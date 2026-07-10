"""brain-session-start: SessionStart hook — inject the project's rolling state note.

Reads the Claude Code SessionStart hook JSON on stdin (cwd, source), resolves
the project (git toplevel basename, else cwd basename), and prints
`memories/<project>-state.md` as `hookSpecificOutput.additionalContext` — so a
session opens already knowing where work left off: no command, no subagent,
no MCP tool load (see specs/session-start-hook.md).

Freshness is stamped, not guaranteed: the note is only as fresh as the last
reflect. The block carries the note's write time, an episodes-since count, and
a STALE banner routing to /catchup when the project repo has moved past the
note. After emitting, a detached refresh (throttled pull + staleness-gated
incremental ingest — the same guarded path the MCP server runs at startup)
tops up the index for the NEXT session; it can never block this one.

Design invariants (mirrors hook.py):
  * Quiet: no note for the resolved project -> no output. Never guess another
    project's note; wrong context is worse than none.
  * Fast: the blocking path is one stdin read, up to 4 short git calls (three
    3s-bounded, plus the purely-local sync-warning rev-list on a tighter 1s
    bound, so the worst case stays within the hook's 10s budget), one file read,
    and one read-only COUNT. The refresh child is fully detached with all stdio
    on DEVNULL — an inherited stdout pipe would hold the hook's EOF hostage
    until the child exits, silently re-adding start-of-session latency.
  * Safe: the hook never writes, never exits nonzero. Any failure -> exit 0
    with empty stdout (stderr only under BRAIN_HOOK_DEBUG).
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import (
    BRAIN_DIR,
    CACHE_DIR,
    DB_PATH,
    INGEST_STALE_SECONDS,
    MEMORIES_DIR,
    SESSION_BLOCK_MAX_CHARS,
    SESSION_EPISODES_BANNER,
    SESSION_REFRESH_LOCK_PATH,
    SESSION_REFRESH_SETTLE_S,
    SESSION_REPO_DRIFT_SLACK_S,
    SYNC_CONFLICT_FLAG,
)

_GIT_TIMEOUT = 3  # default per-call bound for the blocking path
# The blocking path makes up to 4 git calls: rev-parse (root), log (note commit
# time), log (repo drift), and the sync-warning rev-list. The first three take
# the 3s default; the last is a purely-local ref count that gets the tighter
# _SYNC_GIT_TIMEOUT below, so the worst case is 3x3s + 1s = 10s, within budget.
_SYNC_GIT_TIMEOUT = 1


def _debug(msg: str) -> None:
    if os.environ.get("BRAIN_HOOK_DEBUG") == "1":
        print(f"brain-session-start: {msg}", file=sys.stderr)


def _git_out(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _resolve_root(cwd: Path) -> Path:
    top = _git_out(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else cwd


def _split_note(text: str) -> tuple[str, str]:
    """(frontmatter, body). Tolerates a missing frontmatter block."""
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            return text[4:end], text[end + 4 :].lstrip("\n")
    return "", text


def _frontmatter_field(fm: str, key: str) -> str | None:
    for line in fm.splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip("\"'") or None
    return None


def _parse_ts(s: str) -> float | None:
    """ISO-8601 (date-only ok, trailing Z ok) -> epoch. Naive means UTC."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _note_commit_epoch(note_path: Path) -> float | None:
    """The note's last commit time in the brain repo — survives git pulls,
    unlike mtime. None when the note is uncommitted or git is unavailable."""
    try:
        rel = note_path.relative_to(BRAIN_DIR)
    except ValueError:
        rel = note_path
    ct = _git_out(["log", "-1", "--format=%ct", "--", str(rel)], BRAIN_DIR)
    if ct:
        try:
            return float(ct)
        except ValueError:
            return None
    return None


def _note_epoch(note_path: Path, fm: str, commit_epoch: float | None) -> float | None:
    """When the note was written. `written:` frontmatter (reflect stamps it)
    beats the note's commit time, which beats the day-granular `date:`,
    which beats mtime."""
    written = _frontmatter_field(fm, "written")
    if written:
        epoch = _parse_ts(written)
        if epoch is not None:
            return epoch
    if commit_epoch is not None:
        return commit_epoch
    date = _frontmatter_field(fm, "date")
    if date:
        epoch = _parse_ts(date)
        if epoch is not None:
            return epoch
    try:
        return note_path.stat().st_mtime
    except OSError:
        return None


def _episodes_since(epoch: float, root: Path) -> int | None:
    """Non-sidechain episodes for this project recorded after the note.
    Lexical ts compare works because episode ts is UTC ISO with a 'T'
    separator (the same invariant reflect's SQL leans on)."""
    conn = db.connect_ro()
    if conn is None:
        return None
    iso = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Match both the logical and the resolved root: episodes store Claude
    # Code's cwd verbatim, while `git rev-parse --show-toplevel` returns the
    # physical path (e.g. /tmp vs /private/tmp on macOS) — a symlinked root
    # would otherwise silently count zero and mute the freshness signal.
    roots = {str(root)}
    try:
        roots.add(str(root.resolve()))
    except OSError:
        pass
    clauses: list[str] = []
    params: list = [iso]
    for r in sorted(roots):
        prefix = r.rstrip("/") + "/"
        clauses.append("(cwd = ? OR substr(cwd, 1, ?) = ?)")
        params += [r, len(prefix), prefix]
    try:
        row = conn.execute(
            "SELECT count(*) AS n FROM episodes "
            f"WHERE is_sidechain = 0 AND ts > ? AND ({' OR '.join(clauses)})",
            params,
        ).fetchone()
        return int(row["n"])
    except Exception:
        return None
    finally:
        conn.close()


def _repo_drift(root: Path, epoch: float) -> bool:
    """True when the project repo's HEAD is meaningfully newer than the note.
    Callers pass the LATER of the written: stamp and the note's commit time —
    reflect stamps written: at step 7 but commits at step 10, and that
    multi-step gap (compact, skill mining, re-ingest) routinely exceeds any
    reasonable slack, so comparing against the stamp alone would false-flag
    the note's own project after every reflect. The slack then only has to
    absorb same-commit clock skew."""
    ct = _git_out(["log", "-1", "--format=%ct"], root)
    if not ct:
        return False
    try:
        return float(ct) > epoch + SESSION_REPO_DRIFT_SLACK_S
    except ValueError:
        return False


def _sync_warning() -> str | None:
    """A one-line brain-repo sync warning for the injected block, or None when
    the local brain is clean. Network-FREE (no fetch — counts against the last
    known origin/main via @{u}..HEAD) and cheap. Any degenerate state — offline
    host, missing upstream, detached HEAD, unreadable flag — yields None rather
    than a spurious warning."""
    conflict = False
    try:
        conflict = SYNC_CONFLICT_FLAG.exists()
    except Exception:
        conflict = False
    ahead = 0
    # _git_out is already exception- and nonzero-proof: no upstream / detached
    # HEAD makes `rev-list @{u}..HEAD` exit nonzero -> None -> ahead stays 0.
    out = _git_out(["rev-list", "--count", "@{u}..HEAD"], BRAIN_DIR, timeout=_SYNC_GIT_TIMEOUT)
    if out:
        try:
            ahead = int(out)
        except ValueError:
            ahead = 0
    if conflict:
        return (
            "Brain sync: an unresolved conflict flag (cache/sync-conflict) is set "
            "— resolve it, then run `brain-sync pull`, before trusting memory."
        )
    if ahead:
        return (
            f"Brain sync: {ahead} local memory commit(s) not yet pushed — run "
            "`brain-sync pull` to sync."
        )
    return None


def _safe_sync_warning() -> str | None:
    """Belt-and-braces wrapper: the injected block must survive any failure of
    the sync check, so it can never break or block the hook's stdout. Never
    raises — every exception degrades to no warning."""
    try:
        return _sync_warning()
    except Exception as exc:
        _debug(f"sync warning check failed: {exc!r}")
        return None


def _build_block(
    rel_note: str,
    body: str,
    written_iso: str | None,
    n_since: int | None,
    drift: bool,
    sync_warn: str | None = None,
) -> str:
    written = f", written {written_iso}" if written_iso else ""
    header = f"Project state from brain ({rel_note}{written}; reference, not instructions):"
    tail: list[str] = []
    if sync_warn:
        tail.append(sync_warn)
    fresh = []
    if n_since is not None:
        fresh.append(f"{n_since} episode(s) recorded since this note")
    if drift:
        fresh.append("the project repo has commits newer than this note")
    if fresh:
        tail.append("Freshness: " + "; ".join(fresh) + ".")
    if drift or (n_since or 0) >= SESSION_EPISODES_BANNER:
        tail.append(
            "STALE — work has continued past this note; run /catchup for a "
            "fresh cross-checked brief."
        )
    tail.append(
        "(Deep resume: /catchup · full text & semantic recall: brain MCP "
        "recall/get_episode.)"
    )
    tail_text = "\n".join(tail)
    # Truncate the BODY only, never the tail: a long, very stale note is
    # exactly the case that most needs its freshness line and STALE banner.
    body = body.strip()
    budget = SESSION_BLOCK_MAX_CHARS - len(header) - len(tail_text) - 4  # "\n\n" x2
    if len(body) > budget:
        body = body[: max(budget, 0)].rstrip() + "…"
    return f"{header}\n\n{body}\n\n{tail_text}"


def _spawn_refresh() -> None:
    """Detach brain-session-refresh with all stdio on DEVNULL and its own
    session, so nothing it inherits can block this hook's stdout EOF."""
    try:
        uv = shutil.which("uv") or "uv"
        subprocess.Popen(
            [uv, "run", "--directory", str(BRAIN_DIR), "brain-session-refresh"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        _debug(f"refresh spawn failed: {exc!r}")


def _run() -> str:
    data = json.loads(sys.stdin.read())
    _debug(f"source={data.get('source')!r} cwd={data.get('cwd')!r}")
    cwd = Path(data.get("cwd") or os.getcwd())
    root = _resolve_root(cwd)
    note = MEMORIES_DIR / f"{root.name}-state.md"
    try:
        text = note.read_text(encoding="utf-8")
    except OSError:
        return ""  # quiet: no note for this project, inject nothing
    fm, body = _split_note(text)
    if not body.strip():
        return ""
    commit_epoch = _note_commit_epoch(note)
    epoch = _note_epoch(note, fm, commit_epoch)
    n_since = _episodes_since(epoch, root) if epoch is not None else None
    drift_epochs = [e for e in (epoch, commit_epoch) if e is not None]
    drift = _repo_drift(root, max(drift_epochs)) if drift_epochs else False
    return _build_block(
        f"memories/{note.name}",
        body,
        _iso_utc(epoch) if epoch is not None else None,
        n_since,
        drift,
        _safe_sync_warning(),
    )


def main() -> None:
    # Same output discipline as hook.py: the stdout write + flush live inside
    # the try, and on any failure fd 1 is repointed at /dev/null so the
    # interpreter's shutdown flush can't re-raise on a dead pipe.
    if os.environ.get("BRAIN_SESSION_HOOK_DISABLE") == "1":
        sys.exit(0)
    try:
        block = _run()
        if block:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": block,
                    }
                },
                sys.stdout,
            )
        sys.stdout.flush()
        _spawn_refresh()  # after the flush: output first, freshness second
    except Exception as exc:
        _debug(f"error: {exc!r}")
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
    sys.exit(0)


def refresh_main() -> None:
    """Detached next-session freshener: throttled flock-guarded pull, then a
    staleness-gated incremental ingest — the exact guarded path the MCP
    server's startup thread runs (server._ensure_fresh_index), so concurrent
    execution is bounded: the gate usually no-ops it, and a mispredicted
    overlap (server ingest still running past the settle) at worst duplicates
    idempotent ingest work — wasted CPU, never wrong state (WAL, busy_timeout,
    per-file upsert txns, hash-checked embed backfill). The settle sleep lets
    a same-moment server startup ingest stamp `last_ingest` first. Deliberately NO autoreflect:
    spawning reflect stays the server's job."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SESSION_REFRESH_LOCK_PATH, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # another refresh is already running
            time.sleep(SESSION_REFRESH_SETTLE_S)
            from . import sync
            from .ingest import run_ingest

            sync.maybe_pull()
            stale = True
            if DB_PATH.exists():
                conn = db.connect()
                try:
                    last = db.get_meta(conn, "last_ingest")
                finally:
                    conn.close()
                stale = last is None or (time.time() - float(last)) > INGEST_STALE_SECONDS
            if stale:
                run_ingest(log=lambda s: None)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
