"""brain-push: record Claude Code sessions into the hosted brain.

Parses transcripts with the same parser local ``brain-ingest`` used
(:func:`brain.ingest.parse_transcript` + :func:`brain.ingest.episode_text`,
stable ids and content hashes from :mod:`brain.db`), so a pushed episode is
byte-for-byte the row local ingest would have written. The server embeds and
upserts (see :mod:`brain.ingest_api`); this side needs only a device token
with the ``ingest`` scope.

Sources are local ingest's transcript sources: every configured Claude Code
projects root (``~/.claude/projects`` and ``~/.claude-work/projects``, or
``BRAIN_CLAUDE_PROJECTS``), top-level sessions plus subagent sidechains.
Local ingest never read Codex or pi sessions, so neither does this.

Modes:

  brain-push --hook       Claude Code hook (SessionEnd / Stop). Reads the hook
                          JSON on stdin, starts a detached push of that one
                          transcript, and exits at once. Never blocks the
                          session, never exits nonzero.
  brain-push              catch-up: every transcript that changed since the
                          last successful push (Task Scheduler / cron).
  brain-push --backfill   rescan every transcript, ignoring the file cache.
  brain-push --file PATH  push one transcript (what the hook child runs).

``--since`` (default ``$BRAIN_PUSH_SINCE``, else ``all``) skips older
episodes, for example ones an earlier ``brain-pg-load`` import already holds; ``--since all`` sends everything. Re-sending is harmless:
the server answers ``unchanged`` and embeds nothing.

State lives in ``~/.config/brain/push-state.json`` (``BRAIN_CLIENT_DIR``).
A non-blocking lock file makes concurrent runs safe on Windows and POSIX: a
run that finds the lock held appends its request (its ``--file`` paths, or a
full catch-up) to ``push.pending`` and exits, and the holder runs those
requests before it lets go, so a hook that fires mid-push is never lost. The
state file is replaced atomically.

Episodes the server refuses (a token without that profile, a malformed row)
are remembered apart from acknowledged ones. A catch-up skips them; a
``--backfill`` sends them again, so minting a better token and backfilling
picks them up.

Episode text never reaches the log; the log carries counts and file names.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import _flock as fcntl
from .client import ClientError, client_dir, debug, load_token, post_with_retry

SNAPSHOT_SINCE = os.environ.get("BRAIN_PUSH_SINCE", "").strip() or "all"
BATCH_EPISODES = 100
BATCH_BYTES = 4 * 1024 * 1024
MAX_PASSES = 3
_LOG_MAX = 1024 * 1024
_STATE_VERSION = 1
_REFUSED = ("forbidden", "conflict")  # server outcomes that wrote nothing


# ----------------------------------------------------------------- state ---

def state_path() -> Path:
    return client_dir() / "push-state.json"


def load_state(path: Path | None = None) -> dict:
    path = path or state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": _STATE_VERSION, "files": {}}
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return {"version": _STATE_VERSION, "files": {}}
    return data


def save_state(state: dict, path: Path | None = None) -> None:
    """Write to a sibling temp file, then ``os.replace``: a reader (or a crash)
    sees the old file or the new one, never half of either."""
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: a virus scanner or indexer can hold the target briefly.
            time.sleep(0.2 * (attempt + 1))
    os.replace(tmp, path)


def _log(msg: str) -> None:
    path = client_dir() / "push.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LOG_MAX:
            os.replace(path, path.with_suffix(".log.1"))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {msg}\n")
    except OSError:
        pass
    debug("brain-push", msg)


# --------------------------------------------------------------- parsing ---

def parse_since(raw: str | None) -> datetime | None:
    raw = (raw or SNAPSHOT_SINCE).strip()
    if raw.lower() == "all":
        return None
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _ts(raw) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def episodes_for(path: Path) -> list[dict]:
    """Wire-format episodes for one transcript, keyed exactly as local ingest
    keys them. Last copy of a repeated stable id wins, as there."""
    from . import db
    from .ingest import _episode_excluded, episode_text, parse_transcript, transcript_scope

    scope = transcript_scope(path)
    out: dict[str, dict] = {}
    for ep in parse_transcript(path):
        text = episode_text(ep)
        sid = db.episode_stable_id(
            ep["session_id"],
            ep["uuid"],
            source_path=str(path),
            ts=ep["ts"] or "",
            user_text=ep["user"],
        )
        out[sid] = {
            "stable_id": sid,
            "content_hash": db.episode_content_hash(
                ep["ts"], ep["cwd"], ep["git_branch"], int(ep["is_sidechain"]), text
            ),
            "session_id": ep["session_id"] or "",
            "uuid": ep["uuid"] or "",
            "ts": ep["ts"] or "",
            "cwd": ep["cwd"] or "",
            "git_branch": ep["git_branch"] or "",
            "is_sidechain": bool(ep["is_sidechain"]),
            "scope": scope or "",
            "git_export": not _episode_excluded(ep["cwd"]),
            "text": text,
        }
    return list(out.values())


def transcripts_for_hook(path: Path) -> list[Path]:
    """The hook's transcript plus that session's subagent sidechains."""
    files = [path]
    sub = path.with_suffix("") / "subagents"
    if sub.is_dir():
        files.extend(sorted(sub.glob("*.jsonl")))
    return files


# ------------------------------------------------------------------ push ---

class Pusher:
    """One pass over ``files``. ``send`` posts one batch and returns the
    server's JSON; tests swap it for a fake."""

    def __init__(self, state: dict, *, since: datetime | None, full: bool, send, save):
        self.state = state
        self.since = since
        self.full = full
        self.send = send
        self.save = save
        self.stats = {"files": 0, "changed": 0, "sent": 0, "skipped": 0, "rejected": 0}
        self.results: dict[str, int] = {}

    def _pending(self, path: Path) -> tuple[list[dict], dict] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        key = str(path)
        entry = self.state["files"].get(key) or {}
        meta = {"mtime": st.st_mtime, "size": st.st_size}
        if not self.full and entry.get("mtime") == meta["mtime"] and entry.get("size") == meta["size"]:
            return None
        if self.since is not None and st.st_mtime < self.since.timestamp():
            # Nothing in a file last written before the floor can be newer.
            self.state["files"][key] = {**entry, **meta}
            return None
        sent = entry.get("sent") or {}
        refused = {} if self.full else (entry.get("rejected") or {})
        todo = []
        for ep in episodes_for(path):
            if sent.get(ep["stable_id"]) == ep["content_hash"]:
                continue
            if refused.get(ep["stable_id"]) == ep["content_hash"]:
                continue
            ts = _ts(ep["ts"])
            if self.since is not None and ts is not None and ts < self.since:
                self.stats["skipped"] += 1
                continue
            ep["source_mtime"] = st.st_mtime
            todo.append(ep)
        return todo, meta

    def run(self, files: list[Path]) -> dict:
        queue: list[tuple[str, dict]] = []
        done_meta: dict[str, dict] = {}
        for path in files:
            self.stats["files"] += 1
            got = self._pending(path)
            if got is None:
                continue
            todo, meta = got
            self.stats["changed"] += 1
            key = str(path)
            if not todo:
                self._mark_file(key, meta)
                continue
            done_meta[key] = meta
            queue.extend((key, ep) for ep in todo)
        remaining: dict[str, int] = {}
        for key, _ep in queue:
            remaining[key] = remaining.get(key, 0) + 1
        clean = set(remaining)
        for batch in _batches(queue):
            clean -= self._send_batch(batch)
            for key, _ep in batch:
                remaining[key] -= 1
                if remaining[key] == 0 and key in clean:
                    # Every episode acknowledged: the file is done at this size.
                    self._mark_file(key, done_meta[key])
            self.save(self.state)
        self.save(self.state)
        return {**self.stats, "results": self.results}

    def _mark_file(self, key: str, meta: dict) -> None:
        entry = self.state["files"].get(key) or {}
        self.state["files"][key] = {**entry, **meta}

    def _send_batch(self, batch: list[tuple[str, dict]]) -> set[str]:
        """Post one batch; record what the server acknowledged. Returns the
        files that still have an unacknowledged episode."""
        wire = []
        for _key, ep in batch:
            item = {k: v for k, v in ep.items() if k != "content_hash"}
            wire.append(item)
        out = self.send({"episodes": wire})
        results = out.get("results") or {}
        rejected = {r.get("stable_id") for r in out.get("rejected") or [] if isinstance(r, dict)}
        unacked: set[str] = set()
        for key, ep in batch:
            sid = ep["stable_id"]
            outcome = results.get(sid)
            if outcome is None and sid not in rejected:
                unacked.add(key)  # the next run resends it
                continue
            entry = self.state["files"].setdefault(key, {})
            if sid in rejected or outcome in _REFUSED:
                # Not "sent": a backfill with a better token retries it.
                entry.setdefault("rejected", {})[sid] = ep["content_hash"]
                self.stats["rejected"] += 1
            else:
                entry.setdefault("sent", {})[sid] = ep["content_hash"]
                (entry.get("rejected") or {}).pop(sid, None)
                self.results[outcome] = self.results.get(outcome, 0) + 1
                self.stats["sent"] += 1
        return unacked


def _batches(queue: list[tuple[str, dict]]):
    batch: list[tuple[str, dict]] = []
    size = 0
    for item in queue:
        n = len(item[1]["text"].encode("utf-8")) + 1024
        if batch and (len(batch) >= BATCH_EPISODES or size + n > BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += n
    if batch:
        yield batch


# ----------------------------------------------------------------- locks ---

def _lock_paths() -> tuple[Path, Path]:
    d = client_dir()
    return d / "push.lock", d / "push.pending"


def _leave_request(pending: Path, files: list[str] | None) -> None:
    """Append one waiting request: a list of transcripts, or null for a full
    catch-up. One JSON line per request, so concurrent writers interleave
    whole lines."""
    try:
        with open(pending, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(files) + "\n")
    except OSError:
        pass


def _take_requests(pending: Path) -> tuple[bool, list[str] | None]:
    """Claim everything waiting in the marker. Returns (found, files): files is
    None when any waiter asked for a full catch-up, else the merged paths."""
    taken = pending.with_name(pending.name + ".taken")
    for _ in range(5):
        try:
            os.replace(pending, taken)
            break
        except FileNotFoundError:
            return False, []
        except PermissionError:
            time.sleep(0.05)  # a waiter is mid-append on Windows
    else:
        return False, []
    try:
        lines = taken.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    finally:
        try:
            taken.unlink()
        except OSError:
            pass
    merged: list[str] = []
    for line in lines:
        try:
            req = json.loads(line)
        except ValueError:
            req = None  # unreadable: be safe and catch up everything
        if not isinstance(req, list):
            return True, None
        merged.extend(p for p in req if isinstance(p, str) and p not in merged)
    return True, merged


def run_locked(work, files: list[str] | None = None, *, full: bool = False) -> int | None:
    """Run ``work(files, full)`` under the push lock, then every request other
    runs left while it held the lock. ``files`` None means a catch-up of every
    transcript. Returns None when another process holds the lock (its request
    is queued for the holder)."""
    lock, pending = _lock_paths()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _leave_request(pending, files)
            return None
        try:
            code = work(files, full)
            for _ in range(MAX_PASSES):
                if code != 0:
                    break
                found, waiting = _take_requests(pending)
                if not found:
                    break
                if waiting == []:
                    continue
                code = work(waiting, False)
            return code
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass


# ------------------------------------------------------------------- CLI ---

def _spawn_detached(args: list[str]) -> None:
    """Start ``python -m brain.push <args>`` fully detached, stdio on DEVNULL,
    so the hook's own exit never waits on it."""
    cmd = [sys.executable, "-m", "brain.push", *args]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Leave the hook's job object too, so the child outlives it.
            subprocess.Popen(cmd, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            return
        except OSError:
            subprocess.Popen(cmd, creationflags=base, **kwargs)
            return
    subprocess.Popen(cmd, start_new_session=True, **kwargs)


def _hook_main() -> int:
    """Hook entry. Everything is best-effort; the session is never blocked."""
    try:
        if os.environ.get("BRAIN_PUSH_DISABLE") == "1":
            return 0
        data = json.loads(sys.stdin.read() or "{}")
        transcript = (data.get("transcript_path") or "").strip()
        if not load_token():
            debug("brain-push", "no device token configured; skipping")
            return 0
        _spawn_detached(["--file", transcript] if transcript else [])
    except Exception as exc:
        debug("brain-push", f"hook error: {exc!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-push", description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--hook", action="store_true", help="Claude Code hook mode (detach and exit)")
    mode.add_argument("--backfill", action="store_true", help="rescan every transcript")
    mode.add_argument("--file", action="append", default=[], help="push one transcript (repeatable)")
    parser.add_argument("--since", default=None, help=f"ISO date floor or 'all' (default {SNAPSHOT_SINCE})")
    parser.add_argument("--dry-run", action="store_true", help="count what would be sent; no network")
    args = parser.parse_args(argv)

    if args.hook:
        return _hook_main()
    try:
        since = parse_since(args.since)
    except ValueError:
        print("brain-push: --since must be an ISO date or 'all'", file=sys.stderr)
        return 2
    token = load_token()
    if not token and not args.dry_run:
        print(
            "brain-push: no device token. Mint one with the device_token_create tool on the "
            "hosted /mcp, then save it to ~/.config/brain/device-token or BRAIN_DEVICE_TOKEN.",
            file=sys.stderr,
        )
        return 2

    def resolve(requested: list[str] | None) -> list[Path]:
        if requested is None:
            from .ingest import find_transcripts

            return find_transcripts()
        files: list[Path] = []
        for raw in requested:
            for path in transcripts_for_hook(Path(raw)):
                if path not in files:
                    files.append(path)
        return files

    def send(payload: dict) -> dict:
        if args.dry_run:
            return {"results": {ep["stable_id"]: "dry-run" for ep in payload["episodes"]}}
        return post_with_retry("/ingest/episodes", payload, token=token)

    def work(requested: list[str] | None, full: bool) -> int:
        files = resolve(requested)
        state = load_state()
        saver = (lambda _s: None) if args.dry_run else save_state
        pusher = Pusher(state, since=since, full=full, send=send, save=saver)
        try:
            out = pusher.run(files)
        except ClientError as exc:
            if not args.dry_run:
                save_state(pusher.state)
            _log(f"push stopped: {exc} (files={pusher.stats['files']} sent={pusher.stats['sent']})")
            print(f"brain-push: {exc}", file=sys.stderr)
            return 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(out["results"].items()))
        line = (
            f"files={out['files']} changed={out['changed']} sent={out['sent']} "
            f"rejected={out['rejected']} older={out['skipped']} {summary}".rstrip()
        )
        if not args.dry_run:
            _log(line)
        print(("dry run: " if args.dry_run else "") + line)
        return 0

    code = run_locked(work, args.file or None, full=args.backfill)
    if code is None:
        print("brain-push: another push is running; it will pick this up", file=sys.stderr)
        return 0
    return code


if __name__ == "__main__":
    raise SystemExit(main())
