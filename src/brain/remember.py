"""The `remember` MCP tool: persist a durable fact as a memory markdown file,
git-commit it with provenance trailers, and index it so it is recallable in
the same session — without violating the design rules (markdown+git is the
source of truth, cache/brain.db is a rebuildable index, no API keys).

Five phases per call, guarded by two locks held across phases 2-5:
  1. validate + derive slug/description
  2. dedup (before any write) — refuse near-duplicates unless force=true
  3. write memories/<slug>.md with O_EXCL (never clobber)
  4. index the file (reusing the dedup vector — no second embed)
  5. commit (pathspec-limited, provenance trailers)

After a successful commit, a best-effort sync.push() runs outside both locks
(a slow network must never serialize concurrent remembers); push failure is
non-fatal — the local commit is the durable artifact and the next sync cycle
retries.

The module `threading.Lock` serializes concurrent in-process calls (FastMCP
runs sync tools in a threadpool); the `fcntl.flock` on <BRAIN_DIR>/.remember.lock
serializes across processes (each Claude session runs its own brain-server), so
the dedup->write window can't race two files into existence.
"""

from __future__ import annotations

import fcntl
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import db, embed, sync
from .config import (
    BRAIN_DIR,
    DUP_COSINE,
    DUP_SOFT_COSINE,
    FACT_MAX_CHARS,
    MEMORIES_DIR,
)
from .ingest import index_memory_file

_VALID_TYPES = ("user", "feedback", "project", "reference")

# One lock per process (threadpool) + one lock across processes (flock).
_module_lock = threading.Lock()
_LOCK_PATH = BRAIN_DIR / ".remember.lock"


# ---------------------------------------------------------- derivation ---

def slugify(text: str) -> str:
    """Kebab slug matching ^[a-z0-9]+(-[a-z0-9]+)*$, capped at 48 chars.
    Returns "" for input with no alphanumerics (caller supplies a fallback)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > 48:
        s = s[:48].rstrip("-")
    return s


def derive_description(fact: str) -> str:
    """First sentence/line of `fact`, markdown-stripped, whitespace-collapsed,
    capped at 120 chars — the one-liner that becomes the frontmatter
    `description` and leads the dedup embedding text."""
    first = ""
    for line in fact.splitlines():
        if line.strip():
            first = line.strip()
            break
    s = re.sub(r"^\s*#{1,6}\s*", "", first)          # heading marker
    s = re.sub(r"^\s*[-*+>]\s+", "", s)               # list / quote marker
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)    # [text](url) -> text
    s = re.sub(r"[*_`]+", "", s)                       # emphasis / code ticks
    s = re.sub(r"\s+", " ", s).strip()
    m = re.search(r"[.!?](?:\s|$)", s)                 # first sentence terminator
    if m:
        s = s[: m.start() + 1].strip()
    if len(s) > 120:
        s = s[:120].rstrip()
    return s


def format_memory_md(slug: str, description: str, mtype: str, fact: str) -> str:
    """Claude auto-memory format (parseable by ingest.parse_memory). The
    description is double-quoted with inner quotes escaped exactly the way
    ingest._unquote reverses them (\\" -> "); no other escaping, so the parsed
    value is byte-identical to `description`."""
    desc = description.replace('"', '\\"')
    return (
        "---\n"
        f"name: {slug}\n"
        f'description: "{desc}"\n'
        "metadata:\n"
        f"  type: {mtype}\n"
        "---\n\n"
        f"{fact}\n"
    )


def write_memory_file(memories_dir: Path, base_slug: str, content_for) -> tuple[Path, str]:
    """Create memories/<slug>.md with O_CREAT|O_EXCL (never overwrite — git is
    the source of truth). On collision, retry with -2, -3, … suffixes. Returns
    (path, final_slug). `content_for(slug)` renders the file body given the
    *final* slug, so the frontmatter `name` always matches the filename."""
    memories_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        slug = base_slug if n == 1 else f"{base_slug}-{n}"
        path = memories_dir / f"{slug}.md"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            if n > 1000:
                raise
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content_for(slug))
        return path, slug


# --------------------------------------------------------------- dedup ---

def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _cols(conn, mid: int):
    return conn.execute(
        "SELECT id, stable_id, name, description, path, text FROM memories WHERE id = ?",
        (mid,),
    ).fetchone()


def dedup_check(conn, description: str, fact: str, slug: str = ""):
    """Decide whether `fact` duplicates an existing memory, before any write.

    Returns (status, best_hit, similarity, signal, vec):
      status  — "duplicate" | "possible_duplicate" | None
      best_hit — the matched memory Row (or None)
      similarity — cosine / jaccard / 1.0 as applicable
      signal  — "cosine" | "jaccard" | "name" | "exact" | None
      vec     — the candidate document vector (reused for the index insert),
                or None when the embedder/sqlite-vec is unavailable.

    Scope is the `memories` table only (episodes always "contain" the source
    fact and would false-positive). The candidate is embedded once as a
    document over `description + "\\n" + fact` — byte-identical to what ingest
    stores — so the vector can be reused with no drift."""
    vec = None
    if db.has_vec(conn) and embed.available():
        try:
            vec = embed.embed_documents([description + "\n" + fact])[0]
        except Exception:
            vec = None

    norm_fact = _normalize(fact)
    mems = conn.execute(
        "SELECT id, stable_id, name, description, path, text FROM memories"
    ).fetchall()

    # Exact normalized-text match: unambiguous duplicate.
    if norm_fact:
        for m in mems:
            if _normalize(m["text"]) == norm_fact:
                return ("duplicate", m, 1.0, "exact", vec)

    if vec is not None:
        rows = conn.execute(
            "SELECT rowid, distance FROM memories_vec"
            " WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (db.serialize_f32(vec), 5),
        ).fetchall()
        best_cos, best = -1.0, None
        for r in rows:
            # Skip orphan memories_vec rows — a vec rowid whose memories row is
            # gone (a vec-less-era insert, or the hourly ingest pruning a
            # deleted memory file mid-call under .ingest.lock, not our lock).
            # Scoring one would return a refusal with best=None and crash
            # _existing(None) in remember().
            row = _cols(conn, r["rowid"])
            if row is None:
                continue
            # sqlite-vec returns L2 distance over L2-normalized vectors:
            # cos = 1 - d^2/2.
            cos = 1.0 - (r["distance"] * r["distance"]) / 2.0
            if cos > best_cos:
                best_cos, best = cos, row
        if best is not None:
            if best_cos >= DUP_COSINE:
                return ("duplicate", best, best_cos, "cosine", vec)
            if best_cos >= DUP_SOFT_COSINE:
                return ("possible_duplicate", best, best_cos, "cosine", vec)
        if slug:
            for m in mems:
                if (m["name"] or "") == slug:
                    # Identity on the name axis (slug collision), not a measured
                    # cosine — report 1.0 like the FTS fallback's name path.
                    return ("possible_duplicate", m, 1.0, "name", vec)
        return (None, None, max(best_cos, 0.0), None, vec)

    # Fallback: sqlite-vec / fastembed unavailable — FTS + Jaccard word overlap.
    if slug:
        for m in mems:
            if (m["name"] or "") == slug:
                return ("possible_duplicate", m, 1.0, "name", vec)
    fact_words = set(re.findall(r"\w+", fact.lower()))
    best_j, best_m = 0.0, None
    if fact_words:
        for mid in db.fts_search(conn, "memory", fact, limit=5):
            m = _cols(conn, mid)
            if m is None:
                continue
            other = set(
                re.findall(r"\w+", ((m["description"] or "") + " " + (m["text"] or "")).lower())
            )
            if not other:
                continue
            j = len(fact_words & other) / len(fact_words | other)
            if j > best_j:
                best_j, best_m = j, m
    if best_m is not None and best_j >= 0.5:
        return ("possible_duplicate", best_m, best_j, "jaccard", vec)
    return (None, None, best_j, None, vec)


# ---------------------------------------------------------------- commit ---

def _oneline(s: str) -> str:
    return " ".join((s or "").split())[:200]


def commit_memory(path: Path, slug: str, description: str, session: str, project: str):
    """Pathspec-limited add+commit of one memory file with provenance trailers.
    Returns (ok, err). Retries up to 3x with 300ms backoff on index.lock
    contention; on any other git failure returns (False, <stderr>) without
    raising — the markdown on disk is the durable artifact."""
    trailers = []
    # Collapse newlines/whitespace so a value can't forge or break trailers
    # (argv subprocess, so no shell injection — this is provenance integrity).
    if session:
        trailers.append(f"Session: {_oneline(session)}")
    proj = project or os.environ.get("CLAUDE_PROJECT_DIR", "")
    if proj:
        trailers.append(f"Project: {_oneline(proj)}")
    trailers.append(f"Host: {socket.gethostname()}")

    msg = f"remember: {slug}"
    if description:
        msg += f"\n\n{description}"
    msg += "\n\n" + "\n".join(trailers)

    rel = f"memories/{slug}.md"
    last_err = ""
    for attempt in range(3):
        try:
            add = subprocess.run(
                ["git", "-C", str(BRAIN_DIR), "add", "--", rel],
                capture_output=True, text=True, timeout=10,
            )
            if add.returncode != 0:
                last_err = _oneline(add.stderr)
                if "index.lock" in add.stderr and attempt < 2:
                    time.sleep(0.3)
                    continue
                return (False, last_err)
            commit = subprocess.run(
                ["git", "-C", str(BRAIN_DIR), "commit", "-m", msg, "--", rel],
                capture_output=True, text=True, timeout=10,
            )
            if commit.returncode == 0:
                return (True, "")
            last_err = _oneline(commit.stderr or commit.stdout)
            if "index.lock" in (commit.stderr + commit.stdout) and attempt < 2:
                time.sleep(0.3)
                continue
            return (False, last_err)
        except (OSError, subprocess.SubprocessError) as e:
            last_err = _oneline(str(e))
            return (False, last_err)
    return (False, last_err or "git index.lock contention after retries")


# -------------------------------------------------------- orchestration ---

def _relpath(p: str) -> str:
    try:
        return str(Path(p).relative_to(BRAIN_DIR))
    except ValueError:
        return str(p)


def _existing(row) -> dict:
    return {
        "id": row["stable_id"],
        "name": row["name"],
        "path": _relpath(row["path"]),
        "description": row["description"],
    }


def remember(
    conn,
    fact: str,
    type: str = "project",
    name: str = "",
    session: str = "",
    project: str = "",
    force: bool = False,
) -> dict:
    """Persist a new memory. See server.remember for the tool docstring."""
    # Phase 1: validate + derive.
    if type not in _VALID_TYPES:
        return {"error": "type must be one of user|feedback|project|reference"}
    fact = fact.strip()
    if not fact:
        return {"error": "fact must be a non-empty string"}
    if len(fact) > FACT_MAX_CHARS:
        fact = fact[:FACT_MAX_CHARS].strip()  # cap: distilled facts, not transcripts
    description = derive_description(fact)
    base_slug = slugify(name) if name else slugify(" ".join(description.split()[:6]))
    if not base_slug:
        base_slug = "memory"

    with _module_lock:
        with open(_LOCK_PATH, "w") as lock_fh:
            fcntl.flock(lock_fh, fcntl.LOCK_EX)

            # Phase 2: dedup (before any write).
            status, best, sim, signal, vec = dedup_check(conn, description, fact, base_slug)
            if status is not None and not force:
                return {
                    "status": status,
                    "similarity": round(sim, 4),
                    "signal": signal,
                    "existing": _existing(best),
                    "hint": "pass force=true to write anyway",
                }

            # Phase 3: write (O_EXCL, never clobber).
            path, slug = write_memory_file(
                MEMORIES_DIR,
                base_slug,
                lambda s: format_memory_md(s, description, type, fact),
            )

            # Phase 4: index (reuse the candidate vector — no second embed).
            sid = index_memory_file(conn, path)
            vec_pending = True
            if vec is not None and db.has_vec(conn):
                row = conn.execute(
                    "SELECT id, content_hash FROM memories WHERE stable_id = ?", (sid,)
                ).fetchone()
                if row is not None:
                    with conn:
                        conn.execute(
                            "DELETE FROM memories_vec WHERE rowid = ?", (row["id"],)
                        )
                        conn.execute(
                            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                            (row["id"], db.serialize_f32(vec)),
                        )
                        conn.execute(
                            "UPDATE memories SET embedded_hash = ? WHERE id = ?",
                            (row["content_hash"], row["id"]),
                        )
                    vec_pending = False

            # Phase 5: commit (most failure-prone; never rolls back the file).
            ok, err = commit_memory(path, slug, description, session, project)

    result = {
        "status": "created",
        "name": slug,
        "path": f"memories/{slug}.md",
        "id": sid,
        "committed": ok,
    }
    if not ok:
        result["commit_error"] = err
    if vec_pending:
        result["vec_pending"] = True
    if ok and sync.enabled():
        result["pushed"] = sync.push()
    return result
