"""FastMCP stdio server exposing three tools: recall + get_episode + remember.

Startup: kicks off an incremental ingest in a background daemon thread if the
DB is missing or stale (>1h since the last ingest) — initialize is answered
immediately. Logs go to stderr — stdout belongs to the MCP protocol.
"""

from __future__ import annotations

import sys
import threading
import time

from fastmcp import FastMCP

from . import autoreflect, db, sync, usagelog
from .config import DB_PATH, INGEST_STALE_SECONDS
from .ingest import run_ingest
from .remember import remember as _remember_impl

mcp = FastMCP("brain")


@mcp.tool
def recall(
    query: str,
    k: int = 6,
    scope: str = "all",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Search past Claude Code sessions (episodes), memory notes (memories), and
    per-session digests (summaries, written by the nightly reflect pass).

    Hybrid full-text + semantic search. scope:
    "all" | "episodes" | "memories" | "summaries" ("all" spans all three).
    Filters: project matches path segments of the session dir (e.g. "mobile"
    => ~/src/mobile); since/until are "YYYY-MM-DD" or "Nd" (N days ago), inclusive.
    Returns compact hits: kind, id, date, project, snippet, session_id
    (episodes and summaries) or name (memories), and a `score` in (0, 1] (recency-
    and kind-weighted, normalized RRF); hits are score-descending. Episode/memory
    ids are stable content-derived strings (`ep_…`/`mem_…`) — safe to persist;
    summary ids (`sum_…`) are ephemeral. Use get_episode(id) for full text.
    """
    conn = db.connect()
    try:
        result = db.recall(conn, query, k=k, scope=scope, project=project, since=since, until=until)
        diag = result.pop("diag", None)  # internal-only; keeps MCP output unchanged
        if diag is not None:
            usagelog.log_recall(
                query=query, scope=scope, k=k, source="mcp",
                hits=result["hits"], diag=diag,
            )
        return result
    finally:
        conn.close()


@mcp.tool
def get_episode(id: str) -> dict:
    """Fetch full stored text + metadata for one recall hit.

    `id` is the string from a recall hit; its `ep_`/`mem_`/`sum_` prefix selects
    the kind (episode → the USER/ASSISTANT exchange; memory → body + path;
    summary → a per-session digest written by reflect). `ep_`/`mem_` ids are
    stable across re-ingests and rebuilds; `sum_` ids are ephemeral (pair
    recall → get_episode within one session). Text capped at 8k chars with a
    truncation marker.
    """
    conn = db.connect()
    try:
        out = db.get_item(conn, id)
        if "error" not in out:  # an open is the strongest usefulness signal
            usagelog.log_open(kind=out["kind"], stable_id=out["id"], source="mcp")
        return out
    finally:
        conn.close()


@mcp.tool
def remember(
    fact: str,
    type: str = "project",
    name: str = "",
    session: str = "",
    project: str = "",
    force: bool = False,
) -> dict:
    """Persist a new memory: write memories/<slug>.md, commit, index — immediately recallable.

    fact: the durable fact (short markdown, first sentence becomes the description).
    type: user|feedback|project|reference. name: optional kebab-slug filename.
    session/project: provenance trailers ($CLAUDE_CODE_SESSION_ID from Bash env /
    workspace dir); omitted when empty. If a near-duplicate memory exists it is
    returned instead of writing; force=true writes anyway. On success returns a
    stable `mem_…` id — pair recall → get_episode(id) to read it back.
    """
    conn = db.connect()
    try:
        return _remember_impl(
            conn, fact, type=type, name=name, session=session, project=project, force=force
        )
    finally:
        conn.close()


def _ensure_fresh_index() -> None:
    """Runs in a daemon thread: MCP initialize must never wait on ingest.
    recall serves whatever is already indexed meanwhile (WAL + busy_timeout).
    First-boot FULL builds are the CLI's job (`brain-ingest --full`); this
    only tops the index up incrementally."""
    try:
        # Pull BEFORE the staleness gate so a session started against a fresh
        # index still heals from the remote. Throttled ⇒ no extra fetch traffic.
        sync.maybe_pull(lambda s: print(f"[brain] {s}", file=sys.stderr))
        stale = True
        if DB_PATH.exists():
            conn = db.connect()
            try:
                last = db.get_meta(conn, "last_ingest")
            finally:
                conn.close()
            stale = last is None or (time.time() - float(last)) > INGEST_STALE_SECONDS
        if stale:
            stats = run_ingest(log=lambda s: print(f"[brain] {s}", file=sys.stderr))
            print(
                f"[brain] ingest done: {stats['episodes_total']} episodes, "
                f"{stats['memories_total']} memories in {stats['seconds']}s",
                file=sys.stderr,
            )
        # Debt-triggered reflect: AFTER ingest so the debt count reads the
        # freshly-topped index. Runs every startup (not gated on `stale`).
        # Fire-and-forget; self-firewalled — never blocks or breaks initialize.
        autoreflect.maybe_trigger(lambda s: print(s, file=sys.stderr))
    except Exception as e:  # never block the server on ingest problems
        print(f"[brain] startup ingest failed: {e}", file=sys.stderr)


def main() -> None:
    threading.Thread(target=_ensure_fresh_index, name="brain-ingest", daemon=True).start()
    mcp.run()  # stdio transport — answers initialize immediately


if __name__ == "__main__":
    main()
