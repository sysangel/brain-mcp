"""In-process usage telemetry writer for recall/get_episode (see
specs/usage-logging.md).

Tool handlers call `log_recall(...)` / `log_open(...)`, which do a
`queue.put_nowait` (sub-microsecond) and return. A single daemon thread per
process owns the only connection to `local/usage.db`, drains the queue in
small batches, and inserts under WAL. Contract:

- Writes are nearly free and NEVER block a recall: producers only enqueue.
- Queue full (cap USAGE_QUEUE_MAX) -> the event is dropped silently. Lossy.
- Any writer exception (locked DB, disk full, missing dir) -> one stderr line,
  keep serving; recall never fails or slows because of telemetry.
- `local/usage.db` is non-rebuildable, machine-local, gitignored. Losing it
  costs only usage history (one degraded nightly report).

Only stable ids are ever stored (episodes `ep_…`, memories `mem_…`). Summary
hits/opens carry ephemeral `sum_…` rowids and are deliberately skipped — a
rowid in a persisted usage log is worthless after the next re-ingest.
"""

from __future__ import annotations

import queue
import sqlite3
import sys
import threading
from datetime import datetime, timedelta, timezone

from .config import (
    USAGE_DB_PATH,
    USAGE_MAX_HITS_LOGGED,
    USAGE_QUEUE_MAX,
    USAGE_RETENTION_DAYS,
)

# `foreign_keys=ON` makes the recalls -> recall_hits delete cascade (both the
# retention prune and any manual cleanup). journal_size_limit bounds the WAL.
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=2000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA journal_size_limit=4194304",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS recalls(
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    source       TEXT NOT NULL CHECK (source IN ('mcp','hook','cli')),
    query        TEXT NOT NULL,
    scope        TEXT NOT NULL,
    k            INTEGER NOT NULL,
    fts_only     INTEGER NOT NULL,
    n_hits       INTEGER NOT NULL,
    top_score    REAL,
    top_vec_cos  REAL
);
CREATE INDEX IF NOT EXISTS recalls_ts ON recalls(ts);
CREATE TABLE IF NOT EXISTS recall_hits(
    recall_id INTEGER NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    pos       INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    score     REAL NOT NULL,
    PRIMARY KEY (recall_id, pos)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS recall_hits_sid ON recall_hits(kind, stable_id);
CREATE TABLE IF NOT EXISTS opens(
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL CHECK (source IN ('mcp','hook','cli')),
    kind      TEXT NOT NULL,
    stable_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opens_sid ON opens(kind, stable_id);
CREATE INDEX IF NOT EXISTS opens_ts  ON opens(ts);
"""

# stable_id kinds we persist; summaries (ephemeral sum_ ids) are skipped.
_STABLE_KINDS = ("episode", "memory")

_queue: queue.Queue | None = None
_writer: threading.Thread | None = None
_lock = threading.Lock()
_warned = False


def _utcnow() -> str:
    """UTC ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' — string-comparable, matches episodes.ts."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        print(f"[brain usagelog] {msg}", file=sys.stderr)


def _connect() -> sqlite3.Connection:
    USAGE_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(USAGE_DB_PATH))
    for p in _PRAGMAS:
        conn.execute(p)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')")
    _prune(conn)
    conn.commit()
    return conn


def _prune(conn: sqlite3.Connection) -> None:
    """Drop rows older than the retention window. recall_hits cascade off
    recalls via the FK (foreign_keys=ON, set in _PRAGMAS)."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=USAGE_RETENTION_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM recalls WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM opens WHERE ts < ?", (cutoff,))


def _ensure_writer() -> None:
    """Lazily start the single daemon writer thread. The queue is created here
    (under the lock) so producers never touch an unset global."""
    global _queue, _writer
    if _writer is not None and _writer.is_alive():
        return
    with _lock:
        if _queue is None:
            _queue = queue.Queue(maxsize=USAGE_QUEUE_MAX)
        if _writer is None or not _writer.is_alive():
            _writer = threading.Thread(
                target=_run, name="brain-usagelog", daemon=True
            )
            _writer.start()


def _run() -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = _connect()  # schema init + startup prune (>retention rows)
    except Exception as e:  # missing dir, disk full, locked — degrade, don't die
        _warn_once(f"init failed, telemetry disabled: {e}")
    assert _queue is not None
    while True:
        batch = [_queue.get()]
        for _ in range(63):  # small batch: one commit amortized over many events
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            if conn is not None:
                _write_batch(conn, batch)
        except Exception as e:
            _warn_once(f"write failed, dropping a batch: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            for item in batch:
                if item[0] == "flush":
                    item[1].set()  # wake any flush() waiter, even if conn is None
                _queue.task_done()


def _write_batch(conn: sqlite3.Connection, batch: list) -> None:
    for kind, payload in batch:
        if kind == "recall":
            _write_recall(conn, payload)
        elif kind == "open":
            _write_open(conn, payload)
    conn.commit()  # one commit per batch


def _write_recall(conn: sqlite3.Connection, ev: dict) -> None:
    cur = conn.execute(
        "INSERT INTO recalls(ts, source, query, scope, k, fts_only, n_hits, "
        "top_score, top_vec_cos) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            ev["ts"], ev["source"], ev["query"], ev["scope"], ev["k"],
            ev["fts_only"], ev["n_hits"], ev["top_score"], ev["top_vec_cos"],
        ),
    )
    rid = cur.lastrowid
    rows = [
        (rid, pos, h["kind"], h["id"], h["score"])
        for pos, h in ev["hits"]  # (pos, hit); summaries already filtered out
    ]
    if rows:
        conn.executemany(
            "INSERT INTO recall_hits(recall_id, pos, kind, stable_id, score) "
            "VALUES(?, ?, ?, ?, ?)",
            rows,
        )


def _write_open(conn: sqlite3.Connection, ev: dict) -> None:
    conn.execute(
        "INSERT INTO opens(ts, source, kind, stable_id) VALUES(?, ?, ?, ?)",
        (ev["ts"], ev["source"], ev["kind"], ev["stable_id"]),
    )


def log_recall(
    *, query: str, scope: str, k: int, source: str, hits: list[dict], diag: dict
) -> None:
    """Enqueue one recall event (put_nowait; never raises). `diag` is the block
    db.recall attaches and server.py pops: top_score / top_vec_cos / n_hits /
    fts_only. Only the first USAGE_MAX_HITS_LOGGED hits are logged, and summary
    hits (ephemeral sum_ ids) are dropped — pos still reflects the true rank."""
    try:
        _ensure_writer()
        kept = [
            (pos, {"kind": h.get("kind"), "id": h.get("id"), "score": h.get("score")})
            for pos, h in enumerate(hits[:USAGE_MAX_HITS_LOGGED])
            if h.get("kind") in _STABLE_KINDS
        ]
        ev = {
            "ts": _utcnow(),
            "source": source,
            "query": query,
            "scope": scope,
            "k": k,
            "fts_only": 1 if diag.get("fts_only") else 0,
            "n_hits": diag.get("n_hits", len(hits)),
            "top_score": diag.get("top_score"),
            "top_vec_cos": diag.get("top_vec_cos"),
            "hits": kept,
        }
        _queue.put_nowait(("recall", ev))  # type: ignore[union-attr]
    except queue.Full:
        pass  # lossy contract: drop under backpressure
    except Exception:
        pass  # telemetry must never turn a recall into a failure


def log_open(*, kind: str, stable_id: str, source: str) -> None:
    """Enqueue one open event (put_nowait; never raises). Summary opens carry
    ephemeral sum_ ids and are skipped — only ep_/mem_ opens are usefulness
    signals worth persisting."""
    try:
        if kind not in _STABLE_KINDS:
            return
        _ensure_writer()
        ev = {"ts": _utcnow(), "source": source, "kind": kind, "stable_id": stable_id}
        _queue.put_nowait(("open", ev))  # type: ignore[union-attr]
    except queue.Full:
        pass
    except Exception:
        pass


def flush(timeout: float = 1.0) -> None:
    """Block until the queue drains or `timeout` elapses. For short-lived CLI
    callers (a future brain-recall hook) that would otherwise exit before the
    daemon writes. No-op if nothing was ever logged."""
    if _queue is None:
        return
    done = threading.Event()
    try:
        _queue.put_nowait(("flush", done))
    except queue.Full:
        return  # backed up; give up rather than block a CLI exit
    done.wait(timeout)
