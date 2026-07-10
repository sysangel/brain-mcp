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
import re
import subprocess
import sys
import time
from pathlib import Path

from . import db, embed
from .config import (
    CACHE_DIR,
    CLAUDE_PROJECTS,
    DB_PATH,
    EPISODE_TEXT_CAP,
    MAX_LINE_BYTES,
    MEMORIES_DIR,
    SUMMARIES_DIR,
)


# ------------------------------------------------------------ transcripts ---

def find_transcripts() -> list[Path]:
    files: list[Path] = []
    if not CLAUDE_PROJECTS.is_dir():
        return files
    for proj in sorted(CLAUDE_PROJECTS.iterdir()):
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


# --------------------------------------------------------------- memories ---

def find_memory_files() -> list[Path]:
    files: list[Path] = []
    if CLAUDE_PROJECTS.is_dir():
        for proj in sorted(CLAUDE_PROJECTS.iterdir()):
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


def _drop_db_files() -> None:
    """Delete the DB and its WAL/SHM sidecars (for a full/schema rebuild)."""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DB_PATH) + suffix)
        if p.exists():
            p.unlink()


def run_ingest(full: bool = False, log=None) -> dict:
    """Ingest all sources. Returns a stats dict. `log` is an optional
    callable(str) — never prints to stdout itself (stdout belongs to MCP)."""
    log = log or (lambda s: None)
    t0 = time.time()

    # Pull remote memory updates before scanning: a pull bumps memories/*.md
    # mtimes, which is exactly how the scan below notices new content.
    # Throttled, flock-guarded, exception-proof — never blocks or breaks ingest.
    from . import sync

    sync.maybe_pull(log)

    # Connect and resolve the schema version under an exclusive lock file, so a
    # concurrent run_ingest (each brain-server runs one) can never unlink a DB
    # another process is mid-build on. Connecting *inside* the lock means the
    # version we read is the current on-disk file, not a stale inode a peer just
    # rebuilt out from under us. A v1 (or missing-key) DB is dropped and rebuilt
    # as v2; the peer that waits then reads the freshly stamped v2 and skips it.
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CACHE_DIR / ".ingest.lock", "w") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        if full and DB_PATH.exists():
            _drop_db_files()
            log("dropped existing DB for full rebuild")
        conn = db.connect()
        if db.get_meta(conn, "schema_version") != str(db.SCHEMA_VERSION):
            conn.close()
            _drop_db_files()
            conn = db.connect()  # recreates v2 tables and stamps schema_version
            log("schema changed: full rebuild")

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
    transcripts = find_transcripts()
    memory_files = find_memory_files()
    summary_files = find_summary_files()
    current_paths = (
        {str(p) for p in transcripts}
        | {str(p) for p in memory_files}
        | {str(p) for p in summary_files}
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
            for sid, vals in parsed.items():
                ex = existing.get(sid)
                if ex is not None and (ex["session_id"], ex["uuid"]) != (vals[2], vals[3]):
                    # Same stable_id, different (sessionId, uuid), same source
                    # file: the one signature of a genuine 64-bit hash collision.
                    log(
                        f"stable_id collision: {sid} maps to both "
                        f"({ex['session_id']},{ex['uuid']}) and ({vals[2]},{vals[3]})"
                    )
                row = conn.execute(
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
                    " RETURNING id",
                    vals,
                ).fetchone()
                if row is not None:  # inserted or genuinely updated
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

    # Embeddings (batched): backfill EVERY row whose vector is missing OR stale
    # (embedded_hash != content_hash), not just this run's inserts — self-heals
    # after interrupted or vec-less earlier ingests, and re-embeds a row whose
    # text changed in place during a vec-less run. INSERT OR REPLACE overwrites
    # a stale vector on the surviving rowid; embedded_hash is then stamped to
    # the content_hash the new vector was built from.
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
                    pending = conn.execute(
                        f"SELECT id, content_hash, {text_sql} AS t FROM {base} "
                        f"WHERE id NOT IN (SELECT rowid FROM {table}) "
                        f"   OR embedded_hash IS NULL OR embedded_hash <> content_hash"
                    ).fetchall()
                else:
                    pending = conn.execute(
                        f"SELECT id, {text_sql} AS t FROM {base} "
                        f"WHERE id NOT IN (SELECT rowid FROM {table})"
                    ).fetchall()
                if not pending:
                    continue
                log(f"embedding {len(pending)} rows for {table}…")
                vecs = embed.embed_documents([r["t"] for r in pending])
                if len(vecs) != len(pending):
                    # Not an assert: must survive PYTHONOPTIMIZE. The surrounding
                    # try/except keeps this a soft failure (FTS-only, no stamp).
                    raise RuntimeError(
                        f"embedder returned {len(vecs)} vectors for {len(pending)} rows"
                    )
                with conn:
                    # vec0 rejects INSERT OR REPLACE on an existing rowid
                    # (UNIQUE on its primary key), so delete-then-insert to
                    # overwrite a stale vector in place (no-op delete when fresh).
                    conn.executemany(
                        f"DELETE FROM {table} WHERE rowid = ?",
                        [(r["id"],) for r in pending],
                    )
                    conn.executemany(
                        f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)",
                        [(r["id"], db.serialize_f32(v)) for r, v in zip(pending, vecs)],
                    )
                    if has_hash:
                        conn.executemany(
                            f"UPDATE {base} SET embedded_hash = ? WHERE id = ?",
                            [(r["content_hash"], r["id"]) for r in pending],
                        )
                stats["embedded"] += len(pending)
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


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest transcripts + memory markdown into cache/brain.db")
    ap.add_argument("--full", action="store_true", help="drop the DB and rebuild from scratch")
    args = ap.parse_args()
    stats = run_ingest(full=args.full, log=lambda s: print(f"[brain-ingest] {s}", file=sys.stderr))
    for key, val in stats.items():
        print(f"{key}: {val}")


if __name__ == "__main__":
    main()
