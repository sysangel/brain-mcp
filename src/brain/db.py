"""SQLite schema + hybrid search (FTS5 BM25 + sqlite-vec KNN, RRF fusion).

The DB at cache/brain.db is a derived index: always rebuildable from the
markdown + transcripts, never committed. FTS5 external-content tables are
kept in sync with the base tables by triggers (same transaction). vec0
tables are maintained explicitly at ingest time (rowid = source row id).

Optional project/date recall filters resolve to a candidate id set that is
pushed into BOTH legs (`rowid IN (json_each(?))`) before RRF fusion, so
filtering happens filter-then-fuse rather than fuse-then-filter.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import embed, ranking
from .config import (
    DB_PATH,
    EMBED_DIM,
    RETURN_TEXT_CAP,
    RRF_K,
    SNIPPET_WIDTH,
    VEC_FLOOR_COS,
    CLAUDE_PROJECTS_DIRS,
)

# RETURNING (upsert re-ingest) needs SQLite >= 3.35 — fail loudly if the
# interpreter's bundled SQLite is ever older.
assert sqlite3.sqlite_version_info >= (3, 35), (
    f"brain needs SQLite >= 3.35 for RETURNING; found {sqlite3.sqlite_version}"
)

# Bumped whenever the derived schema changes shape; recorded in meta as
# schema_version. A mismatch triggers an automatic full rebuild at ingest time.
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS files(
    path  TEXT PRIMARY KEY,
    mtime REAL,
    size  INTEGER
);
CREATE TABLE IF NOT EXISTS episodes(
    id            INTEGER PRIMARY KEY,
    stable_id     TEXT NOT NULL UNIQUE,   -- "ep_" + 16 hex, content-derived
    content_hash  TEXT NOT NULL,          -- change detector, drives re-embedding
    embedded_hash TEXT,                   -- content_hash the stored vector was built from; NULL until embedded
    session_id    TEXT,
    uuid          TEXT,
    ts            TEXT,
    cwd           TEXT,
    git_branch    TEXT,
    is_sidechain  INTEGER,
    source_file   TEXT,
    text          TEXT
);
CREATE INDEX IF NOT EXISTS episodes_source ON episodes(source_file);
CREATE TABLE IF NOT EXISTS memories(
    id            INTEGER PRIMARY KEY,
    stable_id     TEXT NOT NULL UNIQUE,   -- "mem_" + 16 hex, path-derived
    content_hash  TEXT NOT NULL,
    embedded_hash TEXT,                   -- content_hash the stored vector was built from; NULL until embedded
    name          TEXT,
    description   TEXT,
    mtype         TEXT,
    path          TEXT,
    ts            TEXT,                    -- authored recency: frontmatter `date` -> git-log date -> mtime
    text          TEXT
);
CREATE INDEX IF NOT EXISTS memories_path ON memories(path);
CREATE TABLE IF NOT EXISTS summaries(
    -- AUTOINCREMENT so a delete-by-path + reinsert (how a rewritten digest is
    -- re-indexed) never reuses the freed rowid. Plain INTEGER PRIMARY KEY reuses
    -- the max rowid, which would let a stale summaries_vec row (left behind by a
    -- vec-less ingest, when _delete_vec_rows no-ops) survive on the reused id and
    -- defeat the hashless "id NOT IN summaries_vec" re-embed. Monotonic ids keep
    -- that assumption true; the orphaned vec row is later swept by orphan-heal.
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT,
    date        TEXT,
    project     TEXT,
    cwd         TEXT,
    host        TEXT,
    description TEXT,
    path        TEXT,
    ts          TEXT,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS summaries_path ON summaries(path);
CREATE INDEX IF NOT EXISTS summaries_session ON summaries(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    text, content='episodes', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    name, description, text, content='memories', content_rowid='id'
);
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    description, text, content='summaries', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, name, description, text)
    VALUES (new.id, new.name, new.description, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
    VALUES ('delete', old.id, old.name, old.description, old.text);
END;
CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts(rowid, description, text)
    VALUES (new.id, new.description, new.text);
END;
CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, description, text)
    VALUES ('delete', old.id, old.description, old.text);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
    VALUES ('delete', old.id, old.name, old.description, old.text);
    INSERT INTO memories_fts(rowid, name, description, text)
    VALUES (new.id, new.name, new.description, new.text);
END;
"""

VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_vec USING vec0(
    embedding float[{EMBED_DIM}]
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec USING vec0(
    embedding float[{EMBED_DIM}]
);
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_vec USING vec0(
    embedding float[{EMBED_DIM}]
);
"""


def _load_vec(conn: sqlite3.Connection) -> bool:
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()")
        return True
    except Exception:
        return False


def has_vec(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT vec_version()")
        return True
    except Exception:
        return False


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")  # background ingest + tool calls coexist
    vec_ok = _load_vec(conn)
    # A DB with no episodes table is being created here; stamp schema_version
    # in the same connect that builds the tables so a freshly created v2 DB
    # reports v2 immediately (not only at ingest end) and a concurrent ingest
    # never mistakes a mid-build v2 DB for a version mismatch. An existing v1
    # DB already has the table and is left unstamped so ingest can detect it.
    fresh = (
        conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='episodes'"
        ).fetchone()[0]
        == 0
    )
    conn.executescript(SCHEMA)
    if vec_ok:
        conn.executescript(VEC_SCHEMA)
    if fresh:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()
    return conn


def connect_ro(db_path: Path | str | None = None) -> sqlite3.Connection | None:
    """Read-only connect for the ambient hook (see hook.py). URI `mode=ro`,
    200 ms busy timeout, no mkdir, no schema DDL, no schema stamp, no vec load.
    Returns None if the DB file does not exist — a synchronous every-prompt hook
    must never create it. Deliberately NOT connect(): that mkdirs, runs the
    schema (materializing an empty DB), stamps schema_version, and waits 5 s on
    a busy writer — all wrong here. WAL readers don't block on the ingest
    writer; the short timeout just bounds checkpoint-edge contention."""
    path = Path(db_path or DB_PATH)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=200")
    return conn


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def serialize_f32(vec) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------- stable ids + content hashes ---
# Single home so ingest, usage-logging, and the remember tool share one
# definition. Ids are short, prefixed, content-derived hex — opaque to
# consumers. Version-tagged salts keep any future scheme disjoint from these.

def _sha16(material: str) -> str:
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def episode_stable_id(session_id, uuid, *, source_path="", ts="", user_text="") -> str:
    """`ep_` + 16 hex over (sessionId, uuid) of the opening user message —
    the stable transcript identity. Falls back to a source/ts/text key only if
    `uuid` is missing (never triggers on current Claude Code data)."""
    if uuid:
        material = "brain-ep-v1\x00" + (session_id or "") + "\x00" + uuid
    else:
        material = (
            "brain-ep-fallback-v1\x00"
            + (session_id or str(source_path))
            + "\x00"
            + (ts or "")
            + "\x00"
            + user_text
        )
    return "ep_" + _sha16(material)


def memory_stable_id(path) -> str:
    """`mem_` + 16 hex over the memory file's absolute path (its unique key)."""
    return "mem_" + _sha16("brain-mem-v1\x00" + str(path))


def episode_content_hash(ts, cwd, git_branch, is_sidechain, text) -> str:
    """Change detector for an episode; `text` is already the capped stored text."""
    material = "\x00".join(
        (ts or "", cwd or "", git_branch or "", str(int(is_sidechain)), text or "")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def memory_content_hash(name, description, mtype, text, date="") -> str:
    """Change detector for a memory. The frontmatter `date` participates so
    editing the authored date refreshes recency; the git-log/mtime fallbacks
    do not (they'd churn the row on every commit or touch)."""
    material = "\x00".join(
        (name or "", description or "", mtype or "", text or "", date or "")
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- search ---

def _parse_date_bound(s: str) -> str:
    """Resolve a recall date bound to `YYYY-MM-DD`. Two forms only: an ISO
    date (`2026-07-01`, absolute) or `<N>d` = N days before today UTC (`7d`
    = last week, `0d` = today). Anything else raises ValueError (same style
    as the scope validation below)."""
    s = s.strip()
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        try:
            day = datetime.now(timezone.utc).date() - timedelta(days=int(m.group(1)))
        except OverflowError:
            raise ValueError(f"invalid date bound {s!r} (N is out of range)")
        return day.isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        try:
            datetime.strptime(s, "%Y-%m-%d")  # reject e.g. 2026-13-40
        except ValueError:
            raise ValueError(f"invalid date {s!r} (expected YYYY-MM-DD or Nd)")
        return s
    raise ValueError(f"invalid date bound {s!r} (expected YYYY-MM-DD or Nd)")


def _fts_or_query(query: str) -> str | None:
    """Turn arbitrary user text into a safe FTS5 MATCH expression:
    OR of double-quoted tokens. Raw punctuation never reaches FTS5 syntax."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens[:32])


# Per-kind table names. One home so fts/vec/base lookups can't drift as kinds
# are added (episode | memory | summary).
_FTS_TABLE = {"episode": "episodes_fts", "memory": "memories_fts", "summary": "summaries_fts"}
_VEC_TABLE = {"episode": "episodes_vec", "memory": "memories_vec", "summary": "summaries_vec"}
_BASE_TABLE = {"episode": "episodes", "memory": "memories", "summary": "summaries"}


def fts_search(
    conn: sqlite3.Connection, kind: str, query: str, limit: int = 40,
    ids: list[int] | None = None,
) -> list[int]:
    if ids == []:  # filter active, empty candidate set — nothing to match
        return []
    table = _FTS_TABLE[kind]
    expr = _fts_or_query(query)
    if expr is None:
        return []
    # One optional candidate-id clause, built into sql once so the phrase
    # fallback below inherits it. rank order is preserved.
    id_filter = " AND rowid IN (SELECT value FROM json_each(?))" if ids is not None else ""
    sql = f"SELECT rowid FROM {table} WHERE {table} MATCH ?{id_filter} ORDER BY rank LIMIT ?"
    ids_json = json.dumps(ids) if ids is not None else None
    args = (expr, ids_json, limit) if ids is not None else (expr, limit)
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        # Last-resort fallback: whole query as a quoted phrase.
        phrase = '"' + re.sub(r'"', " ", query) + '"'
        pargs = (phrase, ids_json, limit) if ids is not None else (phrase, limit)
        try:
            rows = conn.execute(sql, pargs).fetchall()
        except sqlite3.OperationalError:
            return []
    return [r["rowid"] for r in rows]


def _vec_nonempty(conn: sqlite3.Connection, kind: str) -> bool:
    table = _VEC_TABLE[kind]
    try:
        return conn.execute(f"SELECT EXISTS(SELECT 1 FROM {table})").fetchone()[0] == 1
    except sqlite3.OperationalError:
        return False


def vec_search(
    conn: sqlite3.Connection, kind: str, query_vec, limit: int = 40,
    ids: list[int] | None = None,
) -> list[tuple[int, float]]:
    """(rowid, distance) pairs, nearest first. `distance` is vec0's L2 metric
    (sqlite-vec v0.1.9 default; vectors are L2-normalized in embed.py, so
    d = sqrt(2 - 2*cos)). recall converts the best distance to a cosine
    similarity for its usage-logging diag."""
    if ids == []:  # filter active, empty candidate set — nothing to match
        return []
    table = _VEC_TABLE[kind]
    # `rowid IN (json_each(?))` is a true KNN pre-filter on vec0 v0.1.9: the
    # KNN runs over the filtered rows only, so the top-limit are exact.
    id_filter = " AND rowid IN (SELECT value FROM json_each(?))" if ids is not None else ""
    sql = f"SELECT rowid, distance FROM {table} WHERE embedding MATCH ? AND k = ?{id_filter} ORDER BY distance"
    args = (
        (serialize_f32(query_vec), limit, json.dumps(ids))
        if ids is not None
        else (serialize_f32(query_vec), limit)
    )
    try:
        rows = conn.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["rowid"], r["distance"]) for r in rows]


def _rrf(rank_lists: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    scores: dict[int, float] = {}
    for lst in rank_lists:
        for rank, rid in enumerate(lst):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return scores


def make_snippet(text: str, query: str, width: int = SNIPPET_WIDTH) -> str:
    """~width chars around the best query-token match, whitespace-collapsed."""
    flat = re.sub(r"\s+", " ", text).strip()
    low = flat.lower()
    pos = -1
    for tok in sorted(re.findall(r"[A-Za-z0-9_]+", query.lower()), key=len, reverse=True):
        p = low.find(tok)
        if p >= 0:
            pos = p
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    snip = flat[start : start + width]
    if start > 0:
        snip = "…" + snip
    if start + width < len(flat):
        snip = snip + "…"
    return snip


def _norm_segments(s: str) -> str:
    """Hyphen-bounded, lowercase, hyphen-separated segment string for
    substring segment matching: "/Users/x/src/myrepo" -> "-users-x-src-myrepo-".
    Munged transcript dirs ("-Users-x-src") normalize to the same form."""
    return "-" + s.lower().replace("/", "-").strip("-") + "-"


def _project_matches(filter: str, row_project: str) -> bool:
    """True when `filter` matches a whole path segment of `row_project`
    (segment-bounded, case-insensitive; parent segments match, e.g. "src")."""
    return _norm_segments(filter) in _norm_segments(row_project)


def _memory_project(path: str) -> str:
    """Project label for a memory: the Claude project dir name when the file is
    under any CLAUDE_PROJECTS root, else "brain" for <BRAIN_DIR>/memories/*.md.
    Shared by display (_project_of) and filtering (_filter_ids) so they can't
    drift. Checks every scanned root (the CLAUDE_CONFIG_DIR migration split
    sessions across ~/.claude and ~/.claude-work)."""
    p = Path(path)
    for base in CLAUDE_PROJECTS_DIRS:
        try:
            return p.relative_to(base).parts[0]
        except ValueError:
            continue
    return "brain"


def _project_of(row: sqlite3.Row, kind: str) -> str:
    if kind == "episode":
        cwd = row["cwd"] or ""
        return Path(cwd).name or cwd or "?"
    if kind == "summary":
        return row["project"] or "?"  # summaries carry project (basename of cwd) directly
    return _memory_project(row["path"])


def _filter_ids(
    conn: sqlite3.Connection,
    kind: str,
    project: str | None,
    since: str | None,
    until: str | None,
) -> list[int] | None:
    """Ids of `kind` rows passing the project/date filters, or None when no
    filter is active (fast path — identical to unfiltered recall). An empty
    list means "filter active, nothing matched". `since`/`until` are already
    resolved `YYYY-MM-DD` bounds (inclusive, day granularity); NULL-`ts` rows
    are excluded under a date bound and untouched without one."""
    if project is None and since is None and until is None:
        return None
    if kind == "episode":
        table, proj_col = "episodes", "cwd"
    elif kind == "summary":
        table, proj_col = "summaries", "cwd"  # summaries carry cwd like episodes
    else:
        table, proj_col = "memories", "path"

    where: list[str] = []
    params: list = []
    if project is not None:
        # Fuzzy-match in Python over the (small) distinct project values, then
        # constrain on the exact matches. Mirrors _project_of/_memory_project.
        matched = []
        for r in conn.execute(f"SELECT DISTINCT {proj_col} AS p FROM {table}"):
            raw = r["p"]
            row_project = raw if kind in ("episode", "summary") else _memory_project(raw)
            if _project_matches(project, row_project or ""):
                matched.append(raw)
        if not matched:
            return []
        where.append(f"{proj_col} IN ({','.join('?' * len(matched))})")
        params.extend(matched)
    if since is not None:
        where.append("substr(ts,1,10) >= ?")
        params.append(since)
    if until is not None:
        where.append("substr(ts,1,10) <= ?")
        params.append(until)

    sql = f"SELECT id FROM {table} WHERE " + " AND ".join(where)
    return [r["id"] for r in conn.execute(sql, params).fetchall()]


def recall(
    conn: sqlite3.Connection, query: str, k: int = 6, scope: str = "all",
    project: str | None = None, since: str | None = None, until: str | None = None,
) -> dict:
    """Hybrid recall. Returns {"fts_only": bool, "hits": [compact dicts]}.

    Degrades to FTS-only when sqlite-vec or the embedder is unavailable.
    Optional filters (project, since, until) restrict both search legs before
    RRF fusion; when any is active the result also carries `filters` (resolved
    values) and `filter_candidates` (rows passing the filter, pre-search).
    """
    kinds = {
        "all": ["episode", "memory", "summary"],
        "episodes": ["episode"],
        "memories": ["memory"],
        "summaries": ["summary"],
    }.get(scope)
    if kinds is None:
        raise ValueError(
            f"scope must be one of all|episodes|memories|summaries, got {scope!r}"
        )

    # Resolve + validate filters before the embedding call so a bad date/project
    # beats the (slow) embed, and compute the per-kind candidate id sets.
    if project is not None and not project.strip():
        raise ValueError("project must be a non-empty string")
    since_r = _parse_date_bound(since) if since is not None else None
    until_r = _parse_date_bound(until) if until is not None else None
    ids_by_kind = {kd: _filter_ids(conn, kd, project, since_r, until_r) for kd in kinds}
    filters_echo: dict = {}
    if project is not None:
        filters_echo["project"] = project
    if since is not None:
        filters_echo["since"] = since_r
    if until is not None:
        filters_echo["until"] = until_r

    def _finalize(result: dict) -> dict:
        # Honesty: surface the resolved filters + candidate count so an empty or
        # short hit list reads as "filtered to N", never a silent fallback.
        if filters_echo:
            result["filters"] = filters_echo
            result["filter_candidates"] = sum(
                len(v) for v in ids_by_kind.values() if v is not None
            )
        return result

    if not re.search(r"\w", query):  # Unicode-aware: CJK etc. still searchable
        return _finalize({"fts_only": True, "hits": []})  # empty/punctuation query

    fts_only = True
    qvec = None
    vec_kinds: list[str] = []
    if has_vec(conn) and embed.available():
        vec_kinds = [kd for kd in kinds if _vec_nonempty(conn, kd)]
        if vec_kinds:  # only claim hybrid when vectors actually exist
            try:
                qvec = embed.embed_query(query)
                fts_only = False
            except Exception:
                qvec = None
                vec_kinds = []

    # Build the full RRF candidate pool per kind, then re-rank everything
    # (recency + kind priors) rather than sorting raw RRF sums. The pool is
    # bounded (<=80 per kind), so re-ranking all of it is trivially cheap and
    # has no truncation bias.
    now = datetime.now(timezone.utc)
    candidates: list[ranking.Candidate] = []
    min_vec_dist: float | None = None  # smallest L2 distance seen across searched kinds
    for kind in kinds:
        ids = ids_by_kind[kind]  # None = no filter; [] = filtered to nothing
        lists = [fts_search(conn, kind, query, limit=40, ids=ids)]
        if qvec is not None and kind in vec_kinds:
            vec_hits = vec_search(conn, kind, qvec, limit=40, ids=ids)  # (rowid, dist)
            if vec_hits:  # ordered nearest-first, so [0] is the best (smallest) distance
                best = vec_hits[0][1]
                min_vec_dist = best if min_vec_dist is None else min(min_vec_dist, best)
            # Cosine floor: vec0 always returns its "least-far" rows, so an
            # off-topic query on a small corpus otherwise injects weak vector
            # mass into RRF. Drop hits below VEC_FLOOR_COS (unit vectors:
            # cos = 1 - d^2/2). min_vec_dist above stays on the UNFILTERED best
            # hit so top_vec_cos telemetry still reflects the true nearest neighbor.
            kept = [rid for rid, d in vec_hits if 1.0 - d * d / 2.0 >= VEC_FLOOR_COS]
            # Only append a surviving vec leg: an emptied one would still count in
            # n_lists and halve rrf_norm (ranking.py) for surviving FTS hits.
            if kept:
                lists.append(kept)  # strip distances for RRF
        n_lists = len(lists)
        rrf = _rrf(lists)
        if not rrf:
            continue
        table = _BASE_TABLE[kind]
        ts_by_id = {
            r["id"]: r["ts"]
            for r in conn.execute(
                f"SELECT id, ts FROM {table} WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(rrf.keys())),),
            )
        }
        for rid, rrf_sum in rrf.items():
            candidates.append(
                ranking.Candidate(kind, rid, rrf_sum, n_lists, ts_by_id.get(rid))
            )
    ranked = ranking.rank(candidates, now)

    hits = []
    for score, cand in ranked[: max(k, 0)]:
        kind, rid = cand.kind, cand.rid
        if kind == "episode":
            row = conn.execute("SELECT * FROM episodes WHERE id = ?", (rid,)).fetchone()
            if row is None:
                continue
            hits.append(
                {
                    "kind": "episode",
                    "id": row["stable_id"],
                    "date": (row["ts"] or "")[:10],
                    "project": _project_of(row, "episode"),
                    "snippet": make_snippet(row["text"] or "", query),
                    "session_id": row["session_id"],
                    "score": round(score, 4),
                }
            )
        elif kind == "summary":
            row = conn.execute("SELECT * FROM summaries WHERE id = ?", (rid,)).fetchone()
            if row is None:
                continue
            hits.append(
                {
                    "kind": "summary",
                    "id": f"sum_{row['id']}",  # ephemeral integer id (recall→get_episode within a session)
                    "date": (row["date"] or row["ts"] or "")[:10],
                    "project": _project_of(row, "summary"),
                    "snippet": make_snippet(
                        f"{row['description'] or ''} {row['text'] or ''}", query
                    ),
                    "session_id": row["session_id"],
                    "score": round(score, 4),
                }
            )
        else:
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (rid,)).fetchone()
            if row is None:
                continue
            hits.append(
                {
                    "kind": "memory",
                    "id": row["stable_id"],
                    "date": (row["ts"] or "")[:10],
                    "project": _project_of(row, "memory"),
                    "snippet": make_snippet(
                        f"{row['description'] or ''} {row['text'] or ''}", query
                    ),
                    "name": row["name"],
                    "score": round(score, 4),
                }
            )
    # Usage-logging diag (server.py pops it before returning — MCP output is
    # byte-identical to today). Convert the best L2 distance to cosine
    # similarity (unit vectors: cos = 1 - d^2/2); higher = better match.
    # `fts_only` rides along so the writer records the degraded-mode share
    # without a separate param on the transport-agnostic log_recall interface.
    diag = {
        "top_score": hits[0]["score"] if hits else None,
        "top_vec_cos": (1.0 - min_vec_dist * min_vec_dist / 2.0)
        if min_vec_dist is not None
        else None,
        "n_hits": len(hits),
        "fts_only": fts_only,
    }
    result = _finalize({"fts_only": fts_only, "hits": hits})
    result["diag"] = diag
    return result


def get_item(conn: sqlite3.Connection, item_id: str) -> dict:
    """Full stored text + metadata for one row, keyed by id. The
    `ep_`/`mem_`/`sum_` prefix selects the kind; an unknown prefix is an error.
    `ep_`/`mem_` ids are stable content-derived strings; `sum_` ids are the
    summary row's ephemeral integer id (recall → get_episode within one
    session). Text capped at RETURN_TEXT_CAP."""
    if item_id.startswith("ep_"):
        row = conn.execute(
            "SELECT * FROM episodes WHERE stable_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return {"error": f"no episode with id {item_id}"}
        out = {
            "kind": "episode",
            "id": row["stable_id"],
            "session_id": row["session_id"],
            "uuid": row["uuid"],
            "ts": row["ts"],
            "cwd": row["cwd"],
            "git_branch": row["git_branch"],
            "is_sidechain": bool(row["is_sidechain"]),
            "source_file": row["source_file"],
        }
        text = row["text"] or ""
    elif item_id.startswith("mem_"):
        row = conn.execute(
            "SELECT * FROM memories WHERE stable_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            return {"error": f"no memory with id {item_id}"}
        out = {
            "kind": "memory",
            "id": row["stable_id"],
            "name": row["name"],
            "description": row["description"],
            "type": row["mtype"],
            "path": row["path"],
            "ts": row["ts"],
        }
        text = row["text"] or ""
    elif item_id.startswith("sum_"):
        try:
            # OverflowError (not ValueError) surfaces at the bind, not int(), when
            # the id exceeds 64 bits — keep the execute inside the try so an
            # oversized id returns the same graceful error dict as sum_abc.
            rid = int(item_id[4:])
            row = conn.execute("SELECT * FROM summaries WHERE id = ?", (rid,)).fetchone()
        except (ValueError, OverflowError):
            return {"error": f"invalid summary id {item_id!r}"}
        if row is None:
            return {"error": f"no summary with id {item_id}"}
        out = {
            "kind": "summary",
            "id": f"sum_{row['id']}",
            "session_id": row["session_id"],
            "date": row["date"],
            "project": row["project"],
            "cwd": row["cwd"],
            "description": row["description"],
            "path": row["path"],
            "ts": row["ts"],
        }
        text = row["text"] or ""
    else:
        return {"error": f"unknown id {item_id!r} (expected an ep_/mem_/sum_ prefix)"}

    if len(text) > RETURN_TEXT_CAP:
        text = text[:RETURN_TEXT_CAP] + "\n…[truncated]"
    out["text"] = text
    return out
