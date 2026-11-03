"""Import committed git markdown into the Supabase ``brain.documents`` table.

Git is the import source, not the live store. Paths and bytes come from the
selected commit (``git ls-tree`` / ``git cat-file``), not the working tree or
the index, so a dirty checkout cannot upload uncommitted drafts and
``ingested_git_head`` names the bytes that were stored. Vectors are copied
from a SQLite ``cache/brain.db`` only when that file's ``embed_space`` matches
the requested space; otherwise ``embedding`` stays null for
``brain-pg-reembed``.

``memories/archive/`` is imported with ``status=archived`` (not dropped).
Proposals and handoffs are imported too.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sqlite3
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .backend import database_url
from .config import EMBED_DIM
from .embedconf import LEGACY_EMBED_SPACE, embed_space
from .ids import (
    handoff_stable_id,
    memory_stable_id_v1,
    memory_stable_id_v2,
    proposal_stable_id,
    summary_stable_id,
)
from .ingest import parse_episode_file, parse_memory, parse_summary
from .records import parse as parse_lifecycle

_KINDS = {
    "episodes": "episode",
    "memories": "memory",
    "summaries": "summary",
    "proposals": "proposal",
    "handoffs": "handoff",
}


@dataclass
class Doc:
    stable_id: str
    kind: str
    relpath: str
    title: str | None
    description: str | None
    body: str
    content_hash: str
    scope: str | None = None
    project_id: str | None = None
    ts: str | None = None
    cwd: str | None = None
    session_id: str | None = None
    uuid: str | None = None
    git_branch: str | None = None
    host: str | None = None
    status: str = "active"
    recall_enabled: bool = True
    expires_at: str | None = None
    verified_at: str | None = None
    archived_at: str | None = None
    attrs: dict = field(default_factory=dict)
    embedding: list[float] | None = None
    embed_space: str | None = None
    aliases: list[str] = field(default_factory=list)


def summary_content_hash(session_id, date, project, cwd, host, description, text) -> str:
    material = "\x00".join(
        (
            session_id or "",
            date or "",
            project or "",
            cwd or "",
            host or "",
            description or "",
            text or "",
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def committed_markdown(root: Path, rev: str = "HEAD") -> list[str]:
    """Repo-relative markdown paths in ``rev`` (default HEAD), not the index.

    ``git ls-files`` would include a staged file that has never been committed.
    ``ls-tree`` does not.
    """
    out = subprocess.run(
        [
            "git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", rev, "--",
            "memories", "episodes", "summaries", "proposals", "handoffs",
        ],
        check=True,
        capture_output=True,
    )
    paths = []
    for raw in out.stdout.split(b"\0"):
        if not raw or not raw.endswith(b".md"):
            continue
        paths.append(raw.decode("utf-8"))
    return sorted(paths)


def read_committed_blob(root: Path, relpath: str, rev: str = "HEAD") -> bytes:
    """File bytes at ``rev:relpath``. This is what ``ingested_git_head`` names."""
    out = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", f"{rev}:{relpath}"],
        check=True,
        capture_output=True,
    )
    return out.stdout


def committed_timestamp(root: Path, relpath: str, rev: str = "HEAD") -> str:
    """Committer time of the last commit at ``rev`` that touched ``relpath``."""
    out = subprocess.run(
        ["git", "-C", str(root), "log", "-1", "--format=%cI", rev, "--", relpath],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def _git_head(root: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def _day(ts: str | None):
    if not ts or len(ts) < 10:
        return None
    try:
        datetime.strptime(ts[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return ts[:10]


def _maybe_project(value: str | None) -> str | None:
    if not value:
        return None
    try:
        from .records import project_id

        return project_id(value)
    except ValueError:
        return None


def decode_f32(blob: bytes | None, dim: int = EMBED_DIM) -> list[float] | None:
    if not blob or len(blob) != dim * 4:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


def cache_embed_space(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "select value from meta where key = 'embed_space'"
        ).fetchone()
    except sqlite3.OperationalError:
        return LEGACY_EMBED_SPACE
    if row and row[0]:
        return str(row[0])
    return LEGACY_EMBED_SPACE


def load_vector_cache(path: Path | None, accept_space: str) -> tuple[dict[tuple[str, str], list[float]], str]:
    """Vectors keyed by (kind, content_hash). Empty when the space differs."""
    if path is None or not path.is_file():
        return {}, "no sqlite cache"
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        space = cache_embed_space(conn)
        if space != accept_space:
            return {}, f"embed_space {space} != {accept_space}; leaving embeddings null"
        found: dict[tuple[str, str], list[float]] = {}
        try:
            rows = conn.execute(
                "select kind, content_hash, embedding from embedding_cache"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for kind, content_hash, blob in rows:
            vec = decode_f32(blob)
            if vec is not None and content_hash:
                found[(kind, content_hash)] = vec
        _read_vec0(conn, found)
        return found, f"reused {len(found)} vectors from {space}"
    finally:
        conn.close()


def _read_vec0(conn: sqlite3.Connection, found: dict) -> None:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        return
    pairs = (
        ("episode", "episodes", "episodes_vec"),
        ("memory", "memories", "memories_vec"),
    )
    for kind, base, vec in pairs:
        try:
            rows = conn.execute(
                f"""
                select b.content_hash, v.embedding
                  from {base} b
                  join {vec} v on v.rowid = b.id
                 where b.embedded_hash = b.content_hash
                   and b.content_hash is not null
                """
            )
        except sqlite3.OperationalError:
            continue
        for content_hash, blob in rows:
            decoded = decode_f32(blob)
            if decoded is not None:
                found[(kind, content_hash)] = decoded
    try:
        rows = conn.execute(
            "select description, text, session_id, date, project, cwd, host, "
            "v.embedding from summaries s join summaries_vec v on v.rowid = s.id"
        )
    except sqlite3.OperationalError:
        return
    for description, text, session_id, date, project, cwd, host, blob in rows:
        decoded = decode_f32(blob)
        if decoded is None:
            continue
        digest = summary_content_hash(
            session_id, date, project, cwd, host, description, text
        )
        found[("summary", digest)] = decoded


def parse_alias_roots(raw: str) -> list[str]:
    """Checkout roots from ``BRAIN_MEMORY_ALIAS_ROOTS``.

    Newlines and semicolons always separate entries, so a Windows drive letter
    survives (``C:\\review\\brain``). A colon-separated POSIX list still works
    (``/home/a:/home/b``); a single letter before a colon is kept as a drive
    prefix instead of becoming its own entry.
    """
    roots: list[str] = []
    for line in re.split(r"[\r\n]+", raw or ""):
        line = line.strip()
        if not line:
            continue
        if ";" in line:
            roots.extend(part.strip() for part in line.split(";") if part.strip())
            continue
        roots.extend(_split_drive_safe(line))
    return roots


def _split_drive_safe(line: str) -> list[str]:
    parts = line.split(":")
    out: list[str] = []
    index = 0
    while index < len(parts):
        part = parts[index]
        if len(part) == 1 and part.isalpha() and index + 1 < len(parts):
            joined = f"{part}:{parts[index + 1]}".strip()
            if joined:
                out.append(joined)
            index += 2
            continue
        if part.strip():
            out.append(part.strip())
        index += 1
    return out


def _windows_root(root: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:", root)) or root.startswith("\\\\") or root.startswith("//")


def legacy_memory_path(root: str, relpath: str) -> str:
    """Join ``root`` and a repo-relative path using that root's own separators.

    ``Path`` on the importing host rewrites ``C:\\review\\brain`` and would
    hash a different string than the Windows checkout that minted the v1 id.
    """
    root = root.strip()
    rel = relpath.strip().lstrip("/\\")
    if _windows_root(root):
        rel = rel.replace("/", "\\")
        root = root.rstrip("\\/")
        return f"{root}\\{rel}" if rel else root
    rel = rel.replace("\\", "/")
    root = root.rstrip("/")
    return f"{root}/{rel}" if rel else root


def _alias_roots() -> list[str]:
    return parse_alias_roots(os.environ.get("BRAIN_MEMORY_ALIAS_ROOTS", ""))


def build_doc(
    root: Path,
    relpath: str,
    cache: dict[tuple[str, str], list[float]],
    cache_space: str,
    *,
    raw: bytes,
    commit_ts: str = "",
) -> Doc | None:
    """Build a document from committed bytes. ``root`` is only for the local v1 alias."""
    text = raw.decode("utf-8", errors="replace")
    path = root / relpath
    top = relpath.split("/", 1)[0]
    kind = _KINDS.get(top)
    if kind is None:
        return None
    if kind == "memory":
        return _memory_doc(root, path, relpath, text, cache, cache_space, commit_ts)
    if kind == "episode":
        return _episode_doc(path, relpath, text, cache, cache_space)
    if kind == "summary":
        return _summary_doc(path, relpath, text, cache, cache_space, commit_ts)
    return _loose_doc(kind, relpath, text, raw, cache, cache_space)


def _attach(doc: Doc, cache, cache_space: str) -> Doc:
    vec = cache.get((doc.kind, doc.content_hash))
    if vec is not None:
        doc.embedding = vec
        doc.embed_space = cache_space
    return doc


def _memory_doc(root, path, relpath, text, cache, cache_space, commit_ts: str = "") -> Doc:
    lifecycle = parse_lifecycle(text)
    parsed = parse_memory(path, raw=text, ts_fallback=commit_ts)
    status = lifecycle["status"]
    archived_at = None
    if relpath.startswith("memories/archive/") and status == "active":
        status = "archived"
        archived_at = parsed["ts"]
    doc = Doc(
        stable_id=memory_stable_id_v2(relpath),
        kind="memory",
        relpath=relpath,
        title=parsed["name"],
        description=parsed["description"],
        body=parsed["text"] or "",
        content_hash=db.memory_content_hash(
            parsed["name"],
            parsed["description"],
            parsed["mtype"],
            parsed["text"],
            parsed.get("date") or "",
            parsed.get("scope"),
        ),
        scope=parsed.get("scope"),
        project_id=lifecycle.get("project_id"),
        ts=parsed.get("ts"),
        status=status,
        recall_enabled=bool(lifecycle.get("recall_enabled", True)),
        expires_at=lifecycle.get("expires_at"),
        verified_at=lifecycle.get("verified_at"),
        archived_at=archived_at,
        attrs={"type": parsed.get("mtype")} if parsed.get("mtype") else {},
    )
    v1 = memory_stable_id_v1(str(path.resolve()))
    if v1 != doc.stable_id:
        doc.aliases.append(v1)
    for root_s in _alias_roots():
        alias = memory_stable_id_v1(legacy_memory_path(root_s, relpath))
        if alias != doc.stable_id and alias not in doc.aliases:
            doc.aliases.append(alias)
    return _attach(doc, cache, cache_space)


def _episode_doc(path, relpath, text, cache, cache_space) -> Doc | None:
    parsed = parse_episode_file(path, raw=text)
    if parsed is None:
        raise ValueError("not a well-formed episode file")
    recomputed = db.episode_content_hash(
        parsed["ts"],
        parsed["cwd"],
        parsed["git_branch"],
        parsed["is_sidechain"],
        parsed["text"],
    )
    content_hash = recomputed
    attrs = {"is_sidechain": bool(parsed["is_sidechain"])}
    if parsed["content_hash"] != recomputed:
        attrs["hash_mismatch"] = parsed["content_hash"]
    doc = Doc(
        stable_id=parsed["stable_id"],
        kind="episode",
        relpath=relpath,
        title=None,
        description=None,
        body=parsed["text"] or "",
        content_hash=content_hash,
        scope=parsed.get("scope"),
        ts=parsed.get("ts"),
        cwd=parsed.get("cwd"),
        session_id=parsed.get("session_id"),
        uuid=parsed.get("uuid"),
        git_branch=parsed.get("git_branch"),
        project_id=_maybe_project(Path(parsed.get("cwd") or "").name),
        attrs=attrs,
    )
    return _attach(doc, cache, cache_space)


def _summary_doc(path, relpath, text, cache, cache_space, commit_ts: str = "") -> Doc:
    parsed = parse_summary(path, raw=text, ts_fallback=commit_ts)
    digest = summary_content_hash(
        parsed["session_id"],
        parsed["date"],
        parsed["project"],
        parsed["cwd"],
        parsed["host"],
        parsed["description"],
        parsed["text"],
    )
    project = parsed.get("project") or ""
    doc = Doc(
        stable_id=summary_stable_id(relpath),
        kind="summary",
        relpath=relpath,
        title=project or Path(relpath).stem,
        description=parsed["description"],
        body=parsed["text"] or "",
        content_hash=digest,
        ts=parsed.get("ts"),
        cwd=parsed.get("cwd"),
        session_id=parsed.get("session_id"),
        host=parsed.get("host"),
        project_id=_maybe_project(project),
        attrs={"project": project} if project and not _maybe_project(project) else {},
    )
    return _attach(doc, cache, cache_space)


def _loose_doc(kind, relpath, text, raw, cache, cache_space) -> Doc:
    """Proposals and handoffs: optional frontmatter, body is the file."""
    title = Path(relpath).stem
    description = ""
    body = text.strip()
    project = ""
    ts = ""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() == "---":
                for line in lines[1:end]:
                    if ":" not in line or line[:1].isspace():
                        continue
                    key, _, val = line.partition(":")
                    val = val.strip().strip('"')
                    if key.strip() == "description":
                        description = val
                    elif key.strip() == "project":
                        project = val
                    elif key.strip() == "date":
                        ts = val
                body = "\n".join(lines[end + 1 :]).strip()
                break
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip() or title
            break
    sid = proposal_stable_id(relpath) if kind == "proposal" else handoff_stable_id(relpath)
    doc = Doc(
        stable_id=sid,
        kind=kind,
        relpath=relpath,
        title=title,
        description=description,
        body=body,
        content_hash=_sha_bytes(raw),
        ts=ts or None,
        project_id=_maybe_project(Path(project).name if project else ""),
        attrs={"project": project} if project else {},
    )
    return _attach(doc, cache, cache_space)


_UPSERT = """
insert into brain.documents (
  stable_id, kind, scope, project_id, relpath, title, description, body,
  content_hash, embedded_hash, embedding, embed_space, ts, day, cwd,
  session_id, uuid, git_branch, host, status, recall_enabled, expires_at,
  verified_at, archived_at, attrs
) values (
  %s, %s, %s, %s, %s, %s, %s, %s,
  %s, %s, %s, %s, %s, %s, %s,
  %s, %s, %s, %s, %s, %s, %s,
  %s, %s, %s::jsonb
)
on conflict (stable_id) do update set
  kind = excluded.kind,
  scope = excluded.scope,
  project_id = excluded.project_id,
  relpath = excluded.relpath,
  title = excluded.title,
  description = excluded.description,
  body = excluded.body,
  content_hash = excluded.content_hash,
  embedding = case
    when brain.documents.content_hash = excluded.content_hash
         and brain.documents.embedding is not null
      then brain.documents.embedding
    else excluded.embedding
  end,
  embedded_hash = case
    when brain.documents.content_hash = excluded.content_hash
         and brain.documents.embedding is not null
      then brain.documents.embedded_hash
    else excluded.embedded_hash
  end,
  embed_space = case
    when brain.documents.content_hash = excluded.content_hash
         and brain.documents.embedding is not null
      then brain.documents.embed_space
    else excluded.embed_space
  end,
  ts = excluded.ts,
  day = excluded.day,
  cwd = excluded.cwd,
  session_id = excluded.session_id,
  uuid = excluded.uuid,
  git_branch = excluded.git_branch,
  host = excluded.host,
  status = excluded.status,
  recall_enabled = excluded.recall_enabled,
  expires_at = excluded.expires_at,
  verified_at = excluded.verified_at,
  archived_at = excluded.archived_at,
  attrs = excluded.attrs
"""


def _json(value: dict) -> str:
    import json

    return json.dumps({k: v for k, v in value.items() if v is not None})


def _vector(value):
    if value is None:
        return None
    from pgvector import Vector

    return Vector(value)


def upsert_doc(conn, doc: Doc) -> None:
    embedded_hash = doc.content_hash if doc.embedding is not None else None
    conn.execute(
        _UPSERT,
        (
            doc.stable_id,
            doc.kind,
            doc.scope,
            doc.project_id,
            doc.relpath,
            doc.title,
            doc.description,
            doc.body,
            doc.content_hash,
            embedded_hash,
            _vector(doc.embedding),
            doc.embed_space,
            doc.ts,
            _day(doc.ts),
            doc.cwd,
            doc.session_id,
            doc.uuid,
            doc.git_branch,
            doc.host,
            doc.status,
            doc.recall_enabled,
            doc.expires_at,
            doc.verified_at,
            doc.archived_at,
            _json(doc.attrs),
        ),
    )
    for alias in doc.aliases:
        conn.execute(
            """
            insert into brain.memory_id_aliases (old_id, stable_id)
            values (%s, %s)
            on conflict (old_id) do update set stable_id = excluded.stable_id
            """,
            (alias, doc.stable_id),
        )


def load_repo(
    conn,
    root: Path,
    *,
    sqlite_path: Path | None = None,
    accept_space: str | None = None,
    dry_run: bool = False,
) -> dict:
    accept_space = accept_space or embed_space()
    cache, note = load_vector_cache(sqlite_path, accept_space)
    cache_space = accept_space if cache else ""
    stats = {
        "seen": 0,
        "upserted": 0,
        "skipped": 0,
        "vectors": 0,
        "by_kind": {},
        "cache": note,
        "dry_run": dry_run,
    }
    paths = committed_markdown(root)
    for relpath in paths:
        stats["seen"] += 1
        try:
            raw = read_committed_blob(root, relpath)
            commit_ts = committed_timestamp(root, relpath)
            doc = build_doc(
                root, relpath, cache, cache_space, raw=raw, commit_ts=commit_ts
            )
        except (ValueError, OSError, TypeError) as exc:
            stats["skipped"] += 1
            if not dry_run:
                conn.execute(
                    "insert into brain.sync_events (kind, detail) values (%s, %s)",
                    ("import-skip", f"{relpath}: {exc}"),
                )
            continue
        if doc is None:
            stats["skipped"] += 1
            continue
        stats["by_kind"][doc.kind] = stats["by_kind"].get(doc.kind, 0) + 1
        if doc.embedding is not None:
            stats["vectors"] += 1
        if not dry_run:
            upsert_doc(conn, doc)
            stats["upserted"] += 1
    if not dry_run:
        head = _git_head(root)
        now = datetime.now(timezone.utc).isoformat()
        for key, value in (
            ("ingested_git_head", head),
            ("last_load", now),
            ("schema_version", "pg-1"),
        ):
            conn.execute(
                """
                insert into brain.meta (key, value) values (%s, %s)
                on conflict (key) do update
                  set value = excluded.value, updated_at = now()
                """,
                (key, value),
            )
        if stats["vectors"]:
            conn.execute(
                """
                insert into brain.meta (key, value) values ('embed_space', %s)
                on conflict (key) do update
                  set value = excluded.value, updated_at = now()
                """,
                (accept_space,),
            )
        conn.commit()
    return stats


def main(argv: list[str] | None = None) -> int:
    from .config import BRAIN_DIR, DB_PATH

    parser = argparse.ArgumentParser(description="Load committed git markdown into Supabase")
    parser.add_argument("--repo", type=Path, default=BRAIN_DIR)
    parser.add_argument("--sqlite", type=Path, default=None)
    parser.add_argument(
        "--accept-space",
        default=None,
        help="copy sqlite vectors only when the cache embed_space equals this "
        "(default: the process embed space). A mismatch leaves embedding null.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    sqlite_path = args.sqlite
    if sqlite_path is None and DB_PATH.is_file():
        sqlite_path = DB_PATH
    import psycopg
    from pgvector.psycopg import register_vector

    with psycopg.connect(database_url(), prepare_threshold=None) as conn:
        register_vector(conn)
        conn.execute("select set_config('search_path', 'brain, public, extensions', false)")
        stats = load_repo(
            conn,
            args.repo,
            sqlite_path=sqlite_path,
            accept_space=args.accept_space,
            dry_run=args.dry_run,
        )
    print(
        f"seen={stats['seen']} upserted={stats['upserted']} skipped={stats['skipped']} "
        f"vectors={stats['vectors']} kinds={stats['by_kind']} ({stats['cache']})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"brain-pg-load: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
