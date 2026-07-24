"""Incremental ingest of Claude Code transcripts + memory markdown.

Sources:
  - <CLAUDE_PROJECTS>/<proj>/*.jsonl                     (top-level sessions)
  - <CLAUDE_PROJECTS>/<proj>/<session>/subagents/*.jsonl (sidechains, tagged)
  - <CLAUDE_PROJECTS>/<proj>/memory/*.md                 (except MEMORY.md index)
  - <BRAIN_DIR>/memories/*.md

Incremental: files table stores (path, mtime, size); unchanged files are
skipped. A changed file is diffed row-by-row against its indexed rows, keyed on
a content-derived stable_id: vanished rows are deleted, new/changed rows are
upserted (ON CONFLICT DO UPDATE, FTS kept in sync by triggers), and unchanged
rows are left untouched so their vectors are never re-embedded. Idempotent:
re-running over an unchanged corpus adds zero rows and re-embeds nothing.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import db, embed
from .config import (
    BRAIN_EPISODE_EXCLUDE,
    CACHE_DIR,
    CLAUDE_PROJECTS_DIRS,
    DB_PATH,
    EMBED_BACKFILL_CHUNK,
    EPISODE_TEXT_CAP,
    EPISODES_DIR,
    INGEST_SOURCE,
    MAX_LINE_BYTES,
    MEMORIES_DIR,
    SUMMARIES_DIR,
)

_VALID_INGEST_SOURCES = ("auto", "episodes", "transcripts")


# ------------------------------------------------------------ transcripts ---

def find_transcripts() -> list[Path]:
    """Union of transcripts across every configured scan root. "Absent" (for
    prune) therefore means absent from ALL roots, not just the first."""
    files: list[Path] = []
    for base in CLAUDE_PROJECTS_DIRS:
        if not base.is_dir():
            continue
        for proj in sorted(base.iterdir()):
            if not proj.is_dir():
                continue
            files.extend(sorted(proj.glob("*.jsonl")))  # top-level sessions only
            files.extend(sorted(proj.glob("*/subagents/*.jsonl")))  # sidechains
    return files


# Skill/command plumbing rendered as user prompts, plus interruption stubs —
# not real human prompts; indexing them pollutes recall.
_NOISE_TAGS = re.compile(
    r"<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>"
    r"|<local-command-stderr>.*?</local-command-stderr>",
    re.S,
)


def _is_junk_prompt(text: str) -> bool:
    t = text.strip()
    if t.startswith("[Request interrupted"):
        return True
    return not _NOISE_TAGS.sub("", t).strip()


def parse_transcript(path: Path) -> list[dict]:
    """One episode = one human user prompt + following assistant text blocks."""
    force_sidechain = "subagents" in path.parts
    episodes: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur is not None and cur["user"].strip():
            episodes.append(cur)
        cur = None

    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with fh:
        for raw in fh:
            # Byte cap (UTF-8 is <=4 bytes/char, so short lines skip the encode).
            if len(raw) > MAX_LINE_BYTES // 4 and len(raw.encode("utf-8", "replace")) > MAX_LINE_BYTES:
                continue  # never parse pathological lines (giant tool payloads)
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue  # torn/malformed line of an actively-written session
            try:
                ltype = obj.get("type")
                if ltype == "user":
                    if obj.get("isMeta"):
                        continue
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        parts = [
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        text = "\n".join(p for p in parts if p)
                    else:
                        text = ""
                    if not text.strip():
                        continue  # pure tool_result (or empty) user line
                    if _is_junk_prompt(text):
                        flush()  # end previous episode; don't misattribute what follows
                        continue  # interruption stubs / command plumbing
                    flush()
                    cur = {
                        "user": text.strip(),
                        "assistant": [],
                        "session_id": obj.get("sessionId"),
                        "uuid": obj.get("uuid"),
                        "ts": obj.get("timestamp"),
                        "cwd": obj.get("cwd"),
                        "git_branch": obj.get("gitBranch"),
                        "is_sidechain": bool(obj.get("isSidechain")) or force_sidechain,
                    }
                elif ltype == "assistant":
                    if cur is None:
                        continue
                    msg = obj.get("message") or {}
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if (
                                isinstance(b, dict)
                                and b.get("type") == "text"
                                and b.get("text")
                            ):
                                cur["assistant"].append(b["text"])
                # every other type (system, attachment, file-history-snapshot,
                # queue-operation, mode, permission-mode, ai-title, last-prompt,
                # summary, unknown future types): skip
            except Exception:
                continue
    flush()
    return episodes


def episode_text(ep: dict) -> str:
    text = f"USER: {ep['user']}"
    if ep["assistant"]:
        text += "\n\nASSISTANT: " + "\n\n".join(ep["assistant"])
    return text[:EPISODE_TEXT_CAP]


# ----------------------------------------------------- episodes-in-git ---
# The Mac (which has the Claude transcripts) emits one file per episode into the
# git-tracked episodes/ tree so a transcript-less VM can index episodes over a
# `git pull`. See specs/episodes-in-git.md. Columns pulled from an episodes row
# whenever we emit or re-emit; kept in one place so the SELECTs can't drift.
_EPISODE_ROW_COLS = (
    "stable_id, content_hash, session_id, uuid, ts, cwd, git_branch,"
    " is_sidechain, text"
)


def _episode_scalar(v) -> str:
    """One frontmatter scalar as a JSON string literal (valid YAML flow scalar,
    round-trips exactly through json — newlines/quotes escaped, so a value can
    never contain a physical newline and break the `\\n---\\n` fence scan)."""
    return json.dumps("" if v is None else str(v))


def _episode_shard(ts) -> str:
    """`YYYY/MM` shard from an ISO-8601 ts; a missing/malformed ts falls to
    `unknown/unknown` (still matched by the `*/*/*.md` scan glob)."""
    m = re.match(r"(\d{4})-(\d{2})", (ts or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else "unknown/unknown"


def _episode_excluded(cwd) -> bool:
    """Privacy gate: True when the episode's cwd matches BRAIN_EPISODE_EXCLUDE
    (substring match, mirroring how reflect matches BRAIN_DIGEST_EXCLUDE — the
    project basename is a substring of the cwd). Excluded episodes stay in the
    local index but are never written to git-tracked episodes/."""
    c = cwd or ""
    return any(x in c for x in BRAIN_EPISODE_EXCLUDE)


def _render_episode_file(
    stable_id, content_hash, session_id, uuid, ts, cwd, git_branch, is_sidechain, text
) -> str:
    """The exact bytes of an episode file: YAML frontmatter + a blank line + the
    stored episode_text body + a trailing newline. Deterministic given the row,
    so re-emitting an unchanged episode is byte-identical."""
    fm = [
        "---",
        f"stable_id: {_episode_scalar(stable_id)}",
        f"content_hash: {_episode_scalar(content_hash)}",
        f"session_id: {_episode_scalar(session_id)}",
        f"uuid: {_episode_scalar(uuid)}",
        f"ts: {_episode_scalar(ts)}",
        f"cwd: {_episode_scalar(cwd)}",
        f"git_branch: {_episode_scalar(git_branch)}",
        f"is_sidechain: {int(is_sidechain)}",
        "---",
    ]
    return "\n".join(fm) + "\n\n" + (text or "") + "\n"


def emit_episode_file(row) -> Path | None:
    """Write one episode's git-tracked file at episodes/YYYY/MM/<stable_id>.md
    (filename = stable_id, so a re-emit overwrites in place). `row` is an
    episodes DB row (or any mapping with the _EPISODE_ROW_COLS keys). Returns the
    written path, or None when the episode is excluded by BRAIN_EPISODE_EXCLUDE
    or the on-disk file is already byte-identical (idempotent re-emit leaves it
    untouched — no mtime churn, no spurious git diff)."""
    if _episode_excluded(row["cwd"]):
        return None
    content = _render_episode_file(
        row["stable_id"], row["content_hash"], row["session_id"], row["uuid"],
        row["ts"], row["cwd"], row["git_branch"], row["is_sidechain"], row["text"],
    )
    dest = EPISODES_DIR / _episode_shard(row["ts"]) / f"{row['stable_id']}.md"
    # Bytes I/O throughout: text-mode read/write applies universal-newline
    # translation, which would collapse a body's \r\n / \r on the way back and
    # break the round-trip (the recomputed content_hash would never match, so the
    # VM skips the file and the Mac rewrites it every run).
    data = content.encode("utf-8")
    try:
        if dest.exists() and dest.read_bytes() == data:
            return None  # unchanged — keep it byte-identical
    except OSError:
        pass
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def find_episode_files() -> list[Path]:
    """Episode files emitted into git: episodes/YYYY/MM/<stable_id>.md. The VM
    indexes these when it has no Claude transcripts of its own."""
    return sorted(EPISODES_DIR.glob("*/*/*.md")) if EPISODES_DIR.is_dir() else []


def parse_episode_file(path: Path) -> dict | None:
    """Inverse of _render_episode_file. Returns the episode fields (including the
    frontmatter `content_hash`, for the caller to re-verify) with the body
    reconstructed byte-exactly, or None when the file is not a well-formed
    episode file (no fences / missing required keys). Does NOT verify the hash —
    the caller recomputes it from the fields and logs the path on a mismatch."""
    try:
        # Bytes read + manual decode: text-mode read_text would apply
        # universal-newline translation and collapse a body's \r\n / \r, so the
        # recomputed content_hash would never match the frontmatter.
        raw = path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    if not raw.startswith("---\n"):
        return None
    after_open = raw[len("---\n"):]
    idx = after_open.find("\n---\n")  # first occurrence is always the closing fence
    if idx == -1:
        return None
    fm_block = after_open[:idx]
    body_region = after_open[idx + len("\n---\n"):]
    # body_region == "\n" + text + "\n": strip exactly the one blank line and the
    # one trailing newline _render_episode_file added (the inverse of "\n\n"+text+"\n").
    if body_region.startswith("\n"):
        body_region = body_region[1:]
    if body_region.endswith("\n"):
        body_region = body_region[:-1]
    fields: dict[str, str] = {}
    for line in fm_block.split("\n"):
        if not line.strip():
            continue
        key, sep, val = line.partition(":")
        if sep:
            fields[key.strip()] = val.strip()
    if not all(k in fields for k in ("stable_id", "content_hash", "ts")):
        return None

    def _unscalar(key: str) -> str:
        v = fields.get(key, '""')
        try:
            out = json.loads(v)
            return out if isinstance(out, str) else str(out)
        except Exception:
            return v.strip('"')

    try:
        is_sidechain = int(fields.get("is_sidechain", "0") or 0)
    except ValueError:
        is_sidechain = 0
    return {
        "stable_id": _unscalar("stable_id"),
        "content_hash": _unscalar("content_hash"),
        "session_id": _unscalar("session_id"),
        "uuid": _unscalar("uuid"),
        "ts": _unscalar("ts"),
        "cwd": _unscalar("cwd"),
        "git_branch": _unscalar("git_branch"),
        "is_sidechain": is_sidechain,
        "text": body_region,
    }


def emit_all_episode_files(conn) -> list[Path]:
    """Sweep every indexed episode through emit_episode_file (honoring the
    exclude gate). Paged so peak memory is O(chunk). Returns the paths actually
    (re)written — unchanged/excluded episodes are omitted."""
    written: list[Path] = []
    ids = [r["id"] for r in conn.execute("SELECT id FROM episodes ORDER BY id")]
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT {_EPISODE_ROW_COLS} FROM episodes WHERE id IN ({q})", chunk
        ):
            p = emit_episode_file(row)
            if p is not None:
                written.append(p)
    return written


def emit_episode_files_for_ids(conn, stable_ids) -> list[Path]:
    """emit_all_episode_files restricted to the given stable_ids (the episodes an
    incremental run inserted/updated). Returns the paths actually (re)written."""
    written: list[Path] = []
    ids = list(stable_ids)
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        q = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"SELECT {_EPISODE_ROW_COLS} FROM episodes WHERE stable_id IN ({q})", chunk
        ):
            p = emit_episode_file(row)
            if p is not None:
                written.append(p)
    return written


def commit_episode_files(paths, log=None) -> bool:
    """Stage episodes/ and commit the emitted files under one commit
    ("brain-ingest: emit N episodes", Host trailer). Reuses remember.commit_paths
    for the add+commit+index.lock-retry. Returns True iff a commit was created;
    an idempotent re-emit / already-clean tree is a no-op returning False. Never
    raises — a failed commit is logged and the next run retries. Called INSIDE
    the ingest lock; the push is the caller's job, AFTER the lock releases.

    Always stages the whole episodes/ dir (so a run that emitted nothing new
    still sweeps up files stranded by an earlier failed commit). The one skip is
    when episodes/ does not exist yet — nothing could be stranded, and
    `git add -- episodes` would otherwise error 'pathspec did not match'."""
    log = log or (lambda s: None)
    if not EPISODES_DIR.exists():
        return False
    import socket

    from .remember import commit_paths  # local: remember imports ingest at load

    msg = f"brain-ingest: emit {len(paths)} episodes"
    trailers = [f"Host: {socket.gethostname()}"]
    ok, committed, err = commit_paths([EPISODES_DIR], msg, trailers)
    if not ok:
        log(f"episode commit failed: {err}")
    return committed


# --------------------------------------------------------------- memories ---

def find_memory_files() -> list[Path]:
    files: list[Path] = []
    for base in CLAUDE_PROJECTS_DIRS:
        if not base.is_dir():
            continue
        for proj in sorted(base.iterdir()):
            mem_dir = proj / "memory"
            if not mem_dir.is_dir():
                continue
            for f in sorted(mem_dir.glob("*.md")):
                if f.name == "MEMORY.md":  # index file, not a memory
                    continue
                files.append(f)
    if MEMORIES_DIR.is_dir():
        files.extend(sorted(MEMORIES_DIR.glob("*.md")))
    return files


_FM_KEY = re.compile(r"^(\w[\w-]*):\s*(.*)$")
_FM_SUBKEY = re.compile(r"^\s+(\w[\w-]*):\s*(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].replace('\\"', '"')
    return v


def _parse_iso_date(s: str) -> str:
    """Return the frontmatter `date` string unchanged if it parses as ISO-8601
    (date or datetime), else '' — so an unparseable date falls through."""
    s = (s or "").strip()
    if not s:
        return ""
    try:
        datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return s


def _git_log_date(path: Path) -> str:
    """ISO-8601 date of the last commit that touched `path` (%cI), or '' when
    the file is uncommitted or not under a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(path.parent), "log", "-1", "--format=%cI", "--", str(path)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def parse_memory(path: Path) -> dict:
    """Parse Claude auto-memory format: YAML-ish frontmatter + markdown body.

    Recency (`ts`) resolves as: frontmatter `date` -> git-log date -> file
    mtime. `date` is returned raw so the content hash can include it (editing
    the authored date refreshes recency) while the git/mtime fallbacks stay out
    of the hash (they'd churn the row on every commit or touch)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    name, description, mtype, date = path.stem, "", None, ""
    body = raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() == "---":
                in_meta = False
                for line in lines[1:end]:
                    m = _FM_KEY.match(line)
                    if m:
                        key, val = m.group(1), _unquote(m.group(2))
                        in_meta = key == "metadata"
                        if key == "name" and val:
                            name = val
                        elif key == "description":
                            description = val
                        elif key == "date":
                            date = val
                    elif in_meta:
                        sm = _FM_SUBKEY.match(line)
                        if sm and sm.group(1) == "type":
                            mtype = _unquote(sm.group(2)) or None
                body = "\n".join(lines[end + 1 :]).strip()
                break
    ts = _parse_iso_date(date) or _git_log_date(path)
    if not ts:
        ts = datetime.datetime.fromtimestamp(
            path.stat().st_mtime, tz=datetime.timezone.utc
        ).isoformat()
    return {
        "name": name,
        "description": description,
        "mtype": mtype,
        "path": str(path),
        "date": date,
        "ts": ts,
        "text": body,
    }


# ---------------------------------------------------------------- summaries ---

def find_summary_files() -> list[Path]:
    """Reflect-written session digests under <BRAIN_DIR>/summaries/YYYY-MM/*.md."""
    return sorted(SUMMARIES_DIR.glob("*/*.md")) if SUMMARIES_DIR.is_dir() else []


def parse_summary(path: Path) -> dict:
    """Parse a reflect-written session digest: frontmatter + markdown body.

    Pulls top-level frontmatter keys (session_id, date, project, cwd, host,
    description). `episodes` is deliberately NOT extracted — the summaries table
    has no such column and no hit/get_item shape carries it; reflect reads
    `episodes:` straight from the file for its rewrite-when-doubled rule. `ts`
    resolves as frontmatter `date` -> file mtime (no git-log fallback)."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    fields = {
        "session_id": "",
        "date": "",
        "project": "",
        "cwd": "",
        "host": "",
        "description": "",
    }
    body = raw
    lines = raw.splitlines()
    if lines and lines[0].strip() == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() == "---":
                for line in lines[1:end]:
                    m = _FM_KEY.match(line)
                    if m:
                        key, val = m.group(1), _unquote(m.group(2))
                        if key in fields:
                            fields[key] = val
                body = "\n".join(lines[end + 1 :]).strip()
                break
    ts = _parse_iso_date(fields["date"])
    if not ts:
        ts = datetime.datetime.fromtimestamp(
            path.stat().st_mtime, tz=datetime.timezone.utc
        ).isoformat()
    return {
        "session_id": fields["session_id"],
        "date": fields["date"],
        "project": fields["project"],
        "cwd": fields["cwd"],
        "host": fields["host"],
        "description": fields["description"],
        "path": str(path),
        "ts": ts,
        "text": body,
    }


def index_memory_file(conn, path: Path) -> str:
    """Parse, upsert, and file-track one memory markdown file in a single
    transaction; return its `mem_…` stable id. The FTS mirror is kept in sync
    by the AI/AU triggers; the vector is (re)built later by the embedding
    backfill (or, for the `remember` tool, written directly from an
    already-computed vector). Raises on an unparseable file.

    Shared by `run_ingest`'s memory loop and the `remember` tool so both
    compute stable_id/content_hash/ts identically."""
    # Stat BEFORE parsing so a write landing between the two records the older
    # (pre-write) mtime against the older content, not a newer mtime against
    # stale content — the latter would make every later incremental ingest see
    # the file as unchanged and never re-index it. Erring toward re-ingest is
    # the safe direction.
    st = path.stat()
    mem = parse_memory(path)
    sid = db.memory_stable_id(mem["path"])
    chash = db.memory_content_hash(
        mem["name"], mem["description"], mem["mtype"], mem["text"], mem["date"]
    )
    with conn:  # one row per file; deletion is prune's job (path-scoped)
        row = conn.execute(
            "INSERT INTO memories(stable_id, content_hash, name, description,"
            " mtype, path, ts, text)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(stable_id) DO UPDATE SET"
            "   content_hash = excluded.content_hash,"
            "   name         = excluded.name,"
            "   description  = excluded.description,"
            "   mtype        = excluded.mtype,"
            "   path         = excluded.path,"
            "   ts           = excluded.ts,"
            "   text         = excluded.text"
            " WHERE memories.content_hash <> excluded.content_hash"
            " RETURNING id",
            (
                sid,
                chash,
                mem["name"],
                mem["description"],
                mem["mtype"],
                mem["path"],
                mem["ts"],
                mem["text"],
            ),
        ).fetchone()
        if row is not None:  # inserted or genuinely updated: drop stale vector
            _delete_vec_rows(conn, "memories_vec", [row["id"]])
        conn.execute(
            "INSERT INTO files(path, mtime, size) VALUES (?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
            (str(path), st.st_mtime, st.st_size),
        )
    return sid


# ----------------------------------------------------------------- driver ---

def _file_changed(conn, path: Path) -> tuple[bool, float, int]:
    st = path.stat()
    row = conn.execute("SELECT mtime, size FROM files WHERE path = ?", (str(path),)).fetchone()
    changed = row is None or row["mtime"] != st.st_mtime or row["size"] != st.st_size
    return changed, st.st_mtime, st.st_size


def _delete_vec_rows(conn, table: str, ids: list[int]) -> None:
    if not db.has_vec(conn) or not ids:
        return
    conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(i,) for i in ids])


# Shared episode upsert (transcript loop + episode-file loop): insert or update
# in place keyed on stable_id; a takeover only moves forward in time and only
# on a content_hash/source_file change (see specs/stable-ids.md). `vals` order:
# (stable_id, content_hash, session_id, uuid, ts, cwd, git_branch,
#  is_sidechain, source_file, text).
_EPISODE_UPSERT_SQL = (
    "INSERT INTO episodes(stable_id, content_hash, session_id, uuid, ts, cwd,"
    " git_branch, is_sidechain, source_file, text)"
    " VALUES (?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(stable_id) DO UPDATE SET"
    "   content_hash = excluded.content_hash,"
    "   session_id   = excluded.session_id,"
    "   uuid         = excluded.uuid,"
    "   ts           = excluded.ts,"
    "   cwd          = excluded.cwd,"
    "   git_branch   = excluded.git_branch,"
    "   is_sidechain = excluded.is_sidechain,"
    "   source_file  = excluded.source_file,"
    "   text         = excluded.text"
    " WHERE (episodes.content_hash <> excluded.content_hash"
    "        OR episodes.source_file  <> excluded.source_file)"
    "   AND (excluded.source_file = episodes.source_file"
    "        OR excluded.ts >= episodes.ts)"  # takeover only moves forward in time
    " RETURNING id"
)


def _upsert_episode(conn, vals):
    """Run the shared episode upsert; returns the RETURNING row (None => the
    WHERE guard suppressed a no-op update, i.e. nothing changed)."""
    return conn.execute(_EPISODE_UPSERT_SQL, vals).fetchone()


def _drop_db_files() -> None:
    """Delete the DB and its WAL/SHM sidecars (for a full/schema rebuild)."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def _read_prev_last_ingest() -> str | None:
    """The current on-disk `last_ingest` marker, read read-only before any
    destructive rebuild. Preserved across a --full/schema drop so readers never
    see the marker missing ("empty + stale") mid-rebuild and stampede."""
    if not DB_PATH.exists():
        return None
    try:
        conn = db.connect_ro(DB_PATH)
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'last_ingest'"
            ).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()
    except Exception:
        return None


INGEST_LOCK_NAME = ".ingest.lock"
_LOCK_WAIT_TIMEOUT = 900.0  # blocking (manual-CLI) mode: fail loud after this


def _acquire_ingest_lock(lock_fh, *, block: bool, log) -> bool:
    """Take the exclusive single-writer ingest lock on `lock_fh`.

    Background callers (server startup ingest, session-refresh) pass block=False:
    one non-blocking attempt, and False when another writer holds it — the caller
    then cleanly no-ops. The manual CLI passes block=True: it polls until the
    lock frees, but bounded by _LOCK_WAIT_TIMEOUT so a wedged holder surfaces as
    a loud error instead of hanging a terminal forever."""
    if not block:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    deadline = time.time() + _LOCK_WAIT_TIMEOUT
    announced = False
    while True:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if not announced:
                log("ingest lock held by another writer; waiting…")
                announced = True
            if time.time() >= deadline:
                raise RuntimeError(
                    f"could not acquire ingest lock after {_LOCK_WAIT_TIMEOUT:.0f}s "
                    f"({CACHE_DIR / INGEST_LOCK_NAME}); another ingest is running "
                    f"or wedged"
                )
            time.sleep(0.5)


def run_ingest(full: bool = False, log=None, *, block: bool = True) -> dict:
    """Ingest all sources under the single-writer ingest lock. Returns a stats
    dict. `log` is an optional callable(str) — never prints to stdout itself
    (stdout belongs to MCP).

    The lock covers the ENTIRE run — scan, parse, upsert, prune, the embedding
    backfill, and the episodes-in-git emit+commit — so concurrent ingests can
    never embed simultaneously or prune against each other's mid-build state.
    block=True (the manual CLI) waits for the lock; block=False (server startup
    ingest, session-refresh) no-ops with a log line when the lock is held,
    returning {"skipped": True}.

    The episode-file push is deliberately OUTSIDE the lock (mirror remember.py:
    never push under a lock — a slow network must not serialize writers)."""
    log = log or (lambda s: None)
    # Validate the source knob LOUDLY before touching anything — a silent
    # fallback to auto would invisibly reintroduce the VM's wrong-source bug.
    if INGEST_SOURCE not in _VALID_INGEST_SOURCES:
        raise RuntimeError(
            f"BRAIN_INGEST_SOURCE={INGEST_SOURCE!r} is invalid; expected one of "
            f"{'|'.join(_VALID_INGEST_SOURCES)}"
        )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / INGEST_LOCK_NAME, "w") as lock_fh:
        if not _acquire_ingest_lock(lock_fh, block=block, log=log):
            log("ingest lock held by another writer; skipping this run")
            return {"skipped": True, "reason": "ingest-lock-held"}
        stats = _run_ingest_locked(full=full, log=log)
    # Lock released. Push the episode commit (if any) outside the lock; a clean
    # no-op commit set nothing to push, so this stays silent then.
    if stats.get("episode_files_committed"):
        from . import sync

        if sync.enabled():
            stats["episodes_pushed"] = sync.push(log)
    return stats


def _run_ingest_locked(full: bool, log) -> dict:
    """The ingest body, run with the single-writer lock already held."""
    t0 = time.time()

    # Pull remote memory updates before scanning: a pull bumps memories/*.md
    # mtimes, which is exactly how the scan below notices new content.
    # Throttled, flock-guarded, exception-proof — never blocks or breaks ingest.
    from . import sync

    sync.maybe_pull(log)

    # Preserve the freshness marker across a destructive rebuild (Fix 5): the
    # single-writer lock already serializes writers, but readers (server /
    # session-start staleness gate) still consult last_ingest, and a --full drop
    # that wiped it would flash "empty + stale". Re-stamp the old value onto the
    # fresh DB so the marker stays continuous until this run finishes and
    # overwrites it with the new time.
    prev_last_ingest = _read_prev_last_ingest()

    # A v1 (or missing-key) DB is dropped and rebuilt as v2. Connecting under the
    # lock (now held for the whole run) means the version we read is the current
    # on-disk file, not a stale inode a peer rebuilt out from under us.
    if full and DB_PATH.exists():
        _drop_db_files()
        log("dropped existing DB for full rebuild")
    conn = db.connect()
    if db.get_meta(conn, "schema_version") != str(db.SCHEMA_VERSION):
        conn.close()
        _drop_db_files()
        conn = db.connect()  # recreates v2 tables and stamps schema_version
        log("schema changed: full rebuild")
    if prev_last_ingest is not None and db.get_meta(conn, "last_ingest") is None:
        with conn:
            db.set_meta(conn, "last_ingest", prev_last_ingest)  # keep marker continuous

    vec_ok = db.has_vec(conn)
    stats = {
        "files_seen": 0,
        "files_changed": 0,
        "episodes_added": 0,
        "episodes_updated": 0,
        "episodes_deleted": 0,
        "memories_added": 0,
        "memories_updated": 0,
        "summaries_added": 0,
    }
    # Episode source selection (episodes-in-git transport; see spec).
    #   transcripts : parse + emit Claude transcripts (the Mac).
    #   episode-file: index episodes/**/*.md pulled from git (the VM).
    # BRAIN_INGEST_SOURCE forces one; "auto" infers. In "episodes" mode we do NOT
    # scan transcripts at all — the VM must never index or emit its own `claude -p`
    # reflect transcripts (auto would otherwise flip it into transcript mode).
    episode_files = find_episode_files()
    if INGEST_SOURCE == "transcripts":
        mode, transcripts = "transcript", find_transcripts()
    elif INGEST_SOURCE == "episodes":
        mode, transcripts = "episode-file", []  # never scan the VM's own transcripts
    else:  # auto: infer from what's present (transcripts win; else episode files)
        transcripts = find_transcripts()
        mode = "transcript" if transcripts or not episode_files else "episode-file"
    memory_files = find_memory_files()
    summary_files = find_summary_files()
    emitted_ids: set[str] = set()  # episodes-in-git: stable_ids to (re)emit after the loop

    episode_source_paths = (
        {str(p) for p in transcripts} if mode == "transcript"
        else {str(p) for p in episode_files}
    )
    current_paths = (
        episode_source_paths
        | {str(p) for p in memory_files}
        | {str(p) for p in summary_files}
    )

    # Zero-transcript prune guard (Fix 3): a scan that finds NO transcripts while
    # the index already tracks transcript files is a misconfiguration (empty or
    # wrong scan root), not an all-sessions-vanished reality. Pruning here once
    # silently deleted every episode. Abort loudly, naming the configured roots,
    # before the prune touches anything. Left UNCONDITIONAL on tracked *.jsonl in
    # transcript mode (not gated on episode-file presence): the Mac emits into
    # episodes/ too, so a Mac whose scan root went missing must still abort here
    # rather than silently fall through. SKIPPED in explicit "episodes" mode — it
    # is about transcript roots we deliberately are not scanning; stale *.jsonl
    # rows from a prior transcript/auto run are cleaned up by the normal prune
    # loop below (they are absent from current_paths). find_transcripts unions all
    # existing roots, so it fires only when EVERY root yielded nothing.
    if mode == "transcript" and not transcripts:
        indexed_transcripts = conn.execute(
            "SELECT count(*) FROM files WHERE path LIKE '%.jsonl'"
        ).fetchone()[0]
        if indexed_transcripts > 0:
            roots = ", ".join(str(d) for d in CLAUDE_PROJECTS_DIRS) or "(none configured)"
            conn.close()
            raise RuntimeError(
                f"transcript scan found 0 files but the index tracks "
                f"{indexed_transcripts} transcript file(s); refusing to prune. "
                f"Check BRAIN_CLAUDE_PROJECTS / scan roots: {roots}. An "
                f"all-transcripts-vanished scan is a misconfiguration, not reality."
            )

    # Episode-file prune guard: the VM analog of the guard above. NO transcripts
    # AND NO episode files, while the index already tracks episode-file sources,
    # is a bad checkout (episodes/ wiped or an empty clone), not reality — abort
    # before the prune wipes the index. A fresh machine (empty index) passes.
    if not transcripts and not episode_files:
        like = (
            str(EPISODES_DIR).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + os.sep + "%"
        )
        indexed_episode_files = conn.execute(
            "SELECT count(*) FROM files WHERE path LIKE ? ESCAPE '\\'", (like,)
        ).fetchone()[0]
        if indexed_episode_files > 0:
            conn.close()
            raise RuntimeError(
                f"episode-file scan found 0 files but the index tracks "
                f"{indexed_episode_files} episode file(s) under {EPISODES_DIR}; "
                f"refusing to prune. A wiped/empty episodes/ checkout is not reality."
            )

    # Prune rows from files that no longer exist.
    for row in conn.execute("SELECT path FROM files").fetchall():
        if row["path"] not in current_paths:
            with conn:
                old = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM episodes WHERE source_file = ?", (row["path"],)
                    )
                ]
                conn.execute("DELETE FROM episodes WHERE source_file = ?", (row["path"],))
                _delete_vec_rows(conn, "episodes_vec", old)
                if old:
                    # A vanished transcript that HELD episodes may have won the
                    # cross-file content-hash dedup for a session fork/resume: a
                    # sibling file still on disk holds identical copies that were
                    # skipped at ingest and so never indexed under its own name.
                    # That sibling's files-table row is mtime-current, so it would
                    # never re-parse on its own — the shared episodes would be lost
                    # until a --full rebuild. Invalidate freshness for same-dir
                    # files so the sibling re-parses THIS run (the transcripts loop
                    # follows immediately) and re-inserts them: with the winner's
                    # copies now deleted they no longer dedup away, while already-
                    # indexed rows no-op via the upsert's content_hash guard, so the
                    # cost is parse-only and only on the rare event of a transcript
                    # aging off disk. Gating on `old` keeps this off memory/summary
                    # deletions (they don't participate in the dedup and would only
                    # eat needless summary re-embeds). LIKE wildcards are escaped so
                    # a '_'/'%' in a project dir name can't over-match a sibling dir.
                    parent = str(Path(row["path"]).parent) + "/"
                    like = (
                        parent.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        + "%"
                    )
                    conn.execute(
                        "DELETE FROM files WHERE path <> ? AND path LIKE ? ESCAPE '\\'",
                        (row["path"], like),
                    )
                old = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM memories WHERE path = ?", (row["path"],)
                    )
                ]
                conn.execute("DELETE FROM memories WHERE path = ?", (row["path"],))
                _delete_vec_rows(conn, "memories_vec", old)
                old = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM summaries WHERE path = ?", (row["path"],)
                    )
                ]
                conn.execute("DELETE FROM summaries WHERE path = ?", (row["path"],))
                _delete_vec_rows(conn, "summaries_vec", old)
                conn.execute("DELETE FROM files WHERE path = ?", (row["path"],))

    for path in transcripts:
        stats["files_seen"] += 1
        try:  # live dir: a file can vanish between listing and stat
            changed, mtime, size = _file_changed(conn, path)
        except OSError as e:
            log(f"skipping {path}: {e}")
            continue
        if not changed:
            continue
        stats["files_changed"] += 1
        episodes = parse_transcript(path)
        # Dedupe by stable_id (last wins): the DB's UNIQUE constraint forbids two
        # rows for one (sessionId, uuid); a transcript shouldn't carry a dup, but
        # this keeps the upsert loop from fighting itself if one ever appears.
        parsed: dict[str, tuple] = {}
        for ep in episodes:
            text = episode_text(ep)
            sid = db.episode_stable_id(
                ep["session_id"],
                ep["uuid"],
                source_path=str(path),
                ts=ep["ts"] or "",
                user_text=ep["user"],
            )
            chash = db.episode_content_hash(
                ep["ts"], ep["cwd"], ep["git_branch"], int(ep["is_sidechain"]), text
            )
            parsed[sid] = (
                sid,
                chash,
                ep["session_id"],
                ep["uuid"],
                ep["ts"],
                ep["cwd"],
                ep["git_branch"],
                int(ep["is_sidechain"]),
                str(path),
                text,
            )
        with conn:  # one transaction per file: diff, delete gone, upsert, mark file
            existing = {
                r["stable_id"]: r
                for r in conn.execute(
                    "SELECT id, stable_id, session_id, uuid FROM episodes"
                    " WHERE source_file = ?",
                    (str(path),),
                )
            }
            gone = [existing[s]["id"] for s in existing.keys() - parsed.keys()]
            if gone:
                conn.executemany("DELETE FROM episodes WHERE id = ?", [(i,) for i in gone])
                _delete_vec_rows(conn, "episodes_vec", gone)
                stats["episodes_deleted"] += len(gone)
            # Cross-file content-hash dedup. A session resume/fork rewrites the
            # same messages into a new sessionId file: identical content (and ts)
            # but a new stable_id, so ON CONFLICT(stable_id) can't collapse the
            # pair and the episode double-indexes. content_hash excludes
            # session_id/uuid, so it is identical across the pair — use it as the
            # dedup key. First-ingested copy wins; skip a NEW parsed episode whose
            # content_hash is already indexed under a different source_file. ts is
            # part of content_hash, so a textually-identical but later episode has
            # a different hash and is never collapsed.
            new_hashes = {v[1] for s, v in parsed.items() if s not in existing}
            dup_hashes: set[str] = set()
            if new_hashes:
                q = ",".join("?" * len(new_hashes))
                dup_hashes = {
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT content_hash FROM episodes"
                        f" WHERE source_file <> ? AND content_hash IN ({q})",
                        (str(path), *new_hashes),
                    )
                }
            for sid, vals in parsed.items():
                ex = existing.get(sid)
                if ex is None and vals[1] in dup_hashes:
                    continue  # identical episode already indexed under another session file
                if ex is not None and (ex["session_id"], ex["uuid"]) != (vals[2], vals[3]):
                    # Same stable_id, different (sessionId, uuid), same source
                    # file: the one signature of a genuine 64-bit hash collision.
                    log(
                        f"stable_id collision: {sid} maps to both "
                        f"({ex['session_id']},{ex['uuid']}) and ({vals[2]},{vals[3]})"
                    )
                row = _upsert_episode(conn, vals)
                if row is not None:  # inserted or genuinely updated
                    if ex is not None:
                        stats["episodes_updated"] += 1
                    else:
                        stats["episodes_added"] += 1
                    _delete_vec_rows(conn, "episodes_vec", [row["id"]])
                    emitted_ids.add(sid)  # episodes-in-git: emit after the loop
            conn.execute(
                "INSERT INTO files(path, mtime, size) VALUES (?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                (str(path), mtime, size),
            )

    # Episode-file mode (VM): index episodes/**/*.md as episode rows. Reuses the
    # files-table/_file_changed incremental logic — a `git pull` bumps the .md
    # mtimes, which is exactly how the scan notices new content. Each file holds
    # one episode keyed by its filename stable_id, so there is no cross-file
    # dedup. content_hash is recomputed from the parsed fields and verified
    # against the frontmatter; a mismatch (tamper/corruption) skips the file. This
    # mode NEVER emits or commits — the VM is read-only on episodes/.
    if mode == "episode-file":
        for path in episode_files:
            stats["files_seen"] += 1
            try:  # live dir: a file can vanish between listing and stat
                changed, mtime, size = _file_changed(conn, path)
            except OSError as e:
                log(f"skipping {path}: {e}")
                continue
            if not changed:
                continue
            stats["files_changed"] += 1
            parsed = parse_episode_file(path)
            if parsed is None:
                log(f"skipping malformed episode file {path}")
                continue
            recomputed = db.episode_content_hash(
                parsed["ts"], parsed["cwd"], parsed["git_branch"],
                parsed["is_sidechain"], parsed["text"],
            )
            if recomputed != parsed["content_hash"]:
                log(f"content_hash mismatch in {path}; skipping")
                continue
            with conn:
                ex = conn.execute(
                    "SELECT id FROM episodes WHERE stable_id = ?", (parsed["stable_id"],)
                ).fetchone()
                row = _upsert_episode(conn, (
                    parsed["stable_id"], parsed["content_hash"], parsed["session_id"],
                    parsed["uuid"], parsed["ts"], parsed["cwd"], parsed["git_branch"],
                    parsed["is_sidechain"], str(path), parsed["text"],
                ))
                if row is not None:
                    if ex is not None:
                        stats["episodes_updated"] += 1
                    else:
                        stats["episodes_added"] += 1
                    _delete_vec_rows(conn, "episodes_vec", [row["id"]])
                conn.execute(
                    "INSERT INTO files(path, mtime, size) VALUES (?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                    (str(path), mtime, size),
                )

    # Episodes-in-git emit (transcript mode only): write episode files for the
    # episodes this run inserted/updated — or, on a --full rebuild, re-export
    # every episode (same sweep as the --emit-episodes backfill). The exclude
    # gate is applied inside emit_episode_file. The commit happens here, INSIDE
    # the ingest lock; run_ingest pushes AFTER the lock releases.
    if mode == "transcript":
        emitted_paths = (
            emit_all_episode_files(conn) if full
            else emit_episode_files_for_ids(conn, emitted_ids)
        )
        stats["episode_files_emitted"] = len(emitted_paths)
        # Always sweep + commit — never gated on this run having emitted
        # something. A prior commit that failed past its index.lock retries
        # leaves files stranded uncommitted; a later zero-new-episode run must
        # still re-attempt them. commit_episode_files stages the whole episodes/
        # dir, so the existing `diff --cached` no-op check makes an already-clean
        # tree a cheap two-git-command no-op (committed=False, no push).
        stats["episode_files_committed"] = commit_episode_files(emitted_paths, log)

    for path in memory_files:
        stats["files_seen"] += 1
        try:  # live dir: a file can vanish between listing and stat
            changed, _mtime, _size = _file_changed(conn, path)
        except OSError as e:
            log(f"skipping {path}: {e}")
            continue
        if not changed:
            continue
        stats["files_changed"] += 1
        sid = db.memory_stable_id(str(path))
        before = conn.execute(
            "SELECT content_hash FROM memories WHERE stable_id = ?", (sid,)
        ).fetchone()
        try:
            index_memory_file(conn, path)
        except Exception as e:
            log(f"skipping unparseable memory file {path}: {e}")
            continue
        after = conn.execute(
            "SELECT content_hash FROM memories WHERE stable_id = ?", (sid,)
        ).fetchone()
        if before is None:
            stats["memories_added"] += 1
        elif after is not None and before["content_hash"] != after["content_hash"]:
            stats["memories_updated"] += 1

    for path in summary_files:
        stats["files_seen"] += 1
        try:  # live dir: a file can vanish between listing and stat
            changed, mtime, size = _file_changed(conn, path)
        except OSError as e:
            log(f"skipping {path}: {e}")
            continue
        if not changed:
            continue
        stats["files_changed"] += 1
        try:
            summ = parse_summary(path)
        except Exception as e:
            log(f"skipping unparseable summary file {path}: {e}")
            continue
        # No stable_id/content_hash on summaries: delete-by-path then insert.
        # summaries.id is AUTOINCREMENT, so the reinsert mints a strictly-larger
        # id (never the freed rowid), and the embedding backfill (which keys on
        # "id not in summaries_vec") re-embeds it with no hash tracking.
        with conn:
            old = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM summaries WHERE path = ?", (str(path),)
                )
            ]
            if old:
                conn.execute("DELETE FROM summaries WHERE path = ?", (str(path),))
                _delete_vec_rows(conn, "summaries_vec", old)
            conn.execute(
                "INSERT INTO summaries(session_id, date, project, cwd, host,"
                " description, path, ts, text)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    summ["session_id"],
                    summ["date"],
                    summ["project"],
                    summ["cwd"],
                    summ["host"],
                    summ["description"],
                    summ["path"],
                    summ["ts"],
                    summ["text"],
                ),
            )
            conn.execute(
                "INSERT INTO files(path, mtime, size) VALUES (?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size",
                (str(path), mtime, size),
            )
        stats["summaries_added"] += 1

    # Embeddings (chunked): backfill EVERY row whose vector is missing OR stale
    # (embedded_hash != content_hash), not just this run's inserts — self-heals
    # after interrupted or vec-less earlier ingests, and re-embeds a row whose
    # text changed in place during a vec-less run. embedded_hash is stamped to
    # the content_hash the new vector was built from.
    #
    # Peak RAM is O(chunk), not O(corpus) (Fix 2): first collect only the pending
    # ids (small — ints), then page through them EMBED_BACKFILL_CHUNK at a time,
    # pulling each chunk's text, embedding it, writing vectors + stamping, and
    # committing per chunk. The old fetchall + one-giant-batch pattern held every
    # pending row's full text AND all vectors in memory at once.
    # Degrades gracefully: text/FTS index still works if this fails.
    stats["embedded"] = 0
    if vec_ok and embed.available():
        try:
            for table, base, text_sql in (
                ("episodes_vec", "episodes", "text"),
                ("memories_vec", "memories", "coalesce(description,'') || char(10) || coalesce(text,'')"),
                ("summaries_vec", "summaries", "coalesce(description,'') || char(10) || coalesce(text,'')"),
            ):
                # summaries carry no content_hash/embedded_hash: a changed file
                # is delete-then-reinserted under a fresh AUTOINCREMENT id, so
                # "id not in vec" alone finds every pending summary vector.
                # episodes/memories upsert in place, so they also re-embed on a
                # content_hash change.
                has_hash = base != "summaries"
                if has_hash:
                    pending_ids = [
                        r["id"] for r in conn.execute(
                            f"SELECT id FROM {base} "
                            f"WHERE id NOT IN (SELECT rowid FROM {table}) "
                            f"   OR embedded_hash IS NULL OR embedded_hash <> content_hash"
                        )
                    ]
                else:
                    pending_ids = [
                        r["id"] for r in conn.execute(
                            f"SELECT id FROM {base} "
                            f"WHERE id NOT IN (SELECT rowid FROM {table})"
                        )
                    ]
                if not pending_ids:
                    continue
                log(
                    f"embedding {len(pending_ids)} rows for {table} "
                    f"in chunks of {EMBED_BACKFILL_CHUNK}…"
                )
                for start in range(0, len(pending_ids), EMBED_BACKFILL_CHUNK):
                    chunk = pending_ids[start : start + EMBED_BACKFILL_CHUNK]
                    qmarks = ",".join("?" * len(chunk))
                    cols = f"id, content_hash, {text_sql} AS t" if has_hash else f"id, {text_sql} AS t"
                    rows = conn.execute(
                        f"SELECT {cols} FROM {base} WHERE id IN ({qmarks})", chunk
                    ).fetchall()
                    if not rows:
                        continue
                    vecs = embed.embed_documents([r["t"] for r in rows])
                    if len(vecs) != len(rows):
                        # Not an assert: must survive PYTHONOPTIMIZE. The
                        # surrounding try/except keeps this a soft failure
                        # (FTS-only, no stamp).
                        raise RuntimeError(
                            f"embedder returned {len(vecs)} vectors for {len(rows)} rows"
                        )
                    with conn:
                        # vec0 rejects INSERT OR REPLACE on an existing rowid
                        # (UNIQUE on its primary key), so delete-then-insert to
                        # overwrite a stale vector in place (no-op delete when fresh).
                        conn.executemany(
                            f"DELETE FROM {table} WHERE rowid = ?",
                            [(r["id"],) for r in rows],
                        )
                        conn.executemany(
                            f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)",
                            [(r["id"], db.serialize_f32(v)) for r, v in zip(rows, vecs)],
                        )
                        if has_hash:
                            conn.executemany(
                                f"UPDATE {base} SET embedded_hash = ? WHERE id = ?",
                                [(r["content_hash"], r["id"]) for r in rows],
                            )
                    stats["embedded"] += len(rows)
        except Exception as e:
            log(f"embedding failed ({e}); index remains FTS-searchable")
    elif not vec_ok:
        log("sqlite-vec unavailable: skipping embeddings (FTS-only index)")

    # Heal vec orphans left by any earlier ingest that ran without sqlite-vec
    # or the embedder (those runs can't touch vec tables, so per-file deletes
    # no-op and stale vectors survive forever otherwise).
    if vec_ok:
        with conn:
            for vec_table, base in (
                ("episodes_vec", "episodes"),
                ("memories_vec", "memories"),
                ("summaries_vec", "summaries"),
            ):
                orphans = [
                    r[0]
                    for r in conn.execute(
                        f"SELECT rowid FROM {vec_table} "
                        f"WHERE rowid NOT IN (SELECT id FROM {base})"
                    )
                ]
                if orphans:
                    conn.executemany(
                        f"DELETE FROM {vec_table} WHERE rowid = ?", [(i,) for i in orphans]
                    )
                    log(f"purged {len(orphans)} orphan rows from {vec_table}")

    with conn:
        db.set_meta(conn, "last_ingest", str(time.time()))
        db.set_meta(conn, "schema_version", str(db.SCHEMA_VERSION))

    stats["episodes_total"] = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
    stats["memories_total"] = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
    stats["summaries_total"] = conn.execute("SELECT count(*) FROM summaries").fetchone()[0]
    if vec_ok:
        stats["episodes_vec_total"] = conn.execute(
            "SELECT count(*) FROM episodes_vec"
        ).fetchone()[0]
        stats["memories_vec_total"] = conn.execute(
            "SELECT count(*) FROM memories_vec"
        ).fetchone()[0]
        stats["summaries_vec_total"] = conn.execute(
            "SELECT count(*) FROM summaries_vec"
        ).fetchone()[0]
    conn.close()
    stats["seconds"] = round(time.time() - t0, 2)
    stats["db_bytes"] = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return stats


def run_emit_episodes(log=None) -> dict:
    """Backfill: sweep EVERY indexed episode through the emit path (honoring the
    exclude gate), commit once (`git add episodes/` + one commit), then push.
    Idempotent — a re-run over unchanged files stages nothing and skips the push.
    Reads the DB only; never rebuilds the index."""
    log = log or (lambda s: None)
    conn = db.connect()
    try:
        paths = emit_all_episode_files(conn)
    finally:
        conn.close()
    stats = {"episode_files_emitted": len(paths)}
    stats["episode_files_committed"] = commit_episode_files(paths, log)
    if stats["episode_files_committed"]:
        from . import sync

        if sync.enabled():
            stats["episodes_pushed"] = sync.push(log)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest transcripts + memory markdown into cache/brain.db")
    ap.add_argument("--full", action="store_true", help="drop the DB and rebuild from scratch")
    ap.add_argument(
        "--emit-episodes",
        action="store_true",
        help="backfill: re-export every indexed episode to episodes/ and "
        "commit+push (episodes-in-git; idempotent). Does not rebuild the index.",
    )
    args = ap.parse_args()

    def logf(s):
        print(f"[brain-ingest] {s}", file=sys.stderr)

    if args.emit_episodes:
        stats = run_emit_episodes(log=logf)
    else:
        stats = run_ingest(full=args.full, log=logf)
    for key, val in stats.items():
        print(f"{key}: {val}")


if __name__ == "__main__":
    main()
