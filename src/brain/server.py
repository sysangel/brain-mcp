"""FastMCP stdio server exposing three tools: recall + get_episode + remember.

Startup: kicks off an incremental ingest in a background daemon thread if the
DB is missing or stale (>1h since the last ingest) — initialize is answered
immediately. Logs go to stderr — stdout belongs to the MCP protocol.
"""

from __future__ import annotations

import sys
import threading
import time
from contextlib import nullcontext

from fastmcp import FastMCP

from . import autoreflect, db, sync, usagelog, winstdin
from .authz import current_principal
from .backend import postgres_selected
from .config import DB_PATH, INGEST_STALE_SECONDS
from .ingest import run_ingest
from .profile_ctx import override_profile
from .remember import remember as _remember_impl

mcp = FastMCP("brain")


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
    "all" | "episodes" | "memories" | "summaries", plus "proposals" and
    "handoffs" on the Supabase store ("all" includes those there too).
    (Profile visibility is not a parameter: when $BRAIN_SCOPE names a profile,
    episodes and memories are already limited to shared + that profile.)
    Filters: project matches path segments of the session dir (e.g. "skills"
    => ~/src/skills); since/until are "YYYY-MM-DD" or "Nd" (N days ago), inclusive.
    Returns compact hits: kind, id, date, project, snippet, session_id
    (episodes and summaries) or name (memories), and a `score` in (0, 1] (recency-
    and kind-weighted, normalized RRF); hits are score-descending. Episode/memory
    ids are stable content-derived strings (`ep_…`/`mem_…`) — safe to persist;
    summary ids (`sum_…`) are ephemeral on the sqlite index and stable
    (`sum_` + path hash) on Supabase. The Supabase store also recalls
    proposals and handoffs when scope is "all", "proposals", or "handoffs".
    Use get_episode(id) for full text.
    """
    principal = current_principal()
    if principal is not None and principal.error:
        return {"error": principal.error}
    profile = override_profile(principal.profile) if principal is not None else nullcontext()
    with profile:
        if postgres_selected():
            from . import pgstore

            result = pgstore.recall(
                query, k=k, scope=scope, project=project, since=since, until=until,
                source="mcp", user_sub=(principal.sub if principal else None),
            )
            result.pop("diag", None)
            return result
        return _recall_sqlite(query, k=k, scope=scope, project=project, since=since, until=until)


def _recall_sqlite(query, k, scope, project, since, until) -> dict:
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


def get_episode(id: str) -> dict:
    """Fetch full stored text + metadata for one recall hit.

    `id` is the string from a recall hit; its `ep_`/`mem_`/`sum_` prefix selects
    the kind (episode → the USER/ASSISTANT exchange; memory → body + path;
    summary → a per-session digest written by reflect). `ep_`/`mem_` ids are
    stable across re-ingests and rebuilds; `sum_` ids are ephemeral (pair
    recall → get_episode within one session). Text capped at 8k chars with a
    truncation marker. On Supabase a memory id from the old absolute-path
    scheme still resolves through memory_id_aliases; the returned id is the
    repo-relative one. Ineligible rows are omitted.
    """
    principal = current_principal()
    if principal is not None and principal.error:
        return {"error": principal.error}
    profile = override_profile(principal.profile) if principal is not None else nullcontext()
    with profile:
        if postgres_selected():
            from . import pgstore

            return pgstore.get_item(
                id, source="mcp", user_sub=(principal.sub if principal else None)
            )
        return _get_sqlite(id)


def _get_sqlite(id: str) -> dict:
    conn = db.connect()
    try:
        out = db.get_item(conn, id)
        if "error" not in out:  # an open is the strongest usefulness signal
            usagelog.log_open(kind=out["kind"], stable_id=out["id"], source="mcp")
        return out
    finally:
        conn.close()


def remember(
    fact: str,
    type: str = "project",
    name: str = "",
    session: str = "",
    project: str = "",
    force: bool = False,
    scope: str = "",
    project_id: str = "",
) -> dict:
    """Persist a new memory and return a stable ``mem_…`` id.

    On Supabase this inserts a ``documents`` row. Success does not require a
    git commit; an optional ``git_exports`` outbox row is queued. On the local
    sqlite backend this still writes ``memories/<slug>.md`` and commits.

    fact: the durable fact (short markdown). The description is the first
    sentence, capped at 120 characters on a word boundary with an ellipsis.
    type: user|feedback|project|reference. name: optional kebab-slug filename.
    session/project: provenance. project_id: canonical project identifier
    (Supabase column; ignored by the sqlite writer). If a near-duplicate
    eligible memory exists it is returned instead of writing; force=true writes
    anyway. scope: shared|personal|work — who may recall it; the default follows
    the signed-in profile, or this process's $BRAIN_SCOPE, except project
    memories, which stay shared.
    """
    principal = current_principal()
    if principal is not None and principal.error:
        return {"error": principal.error}
    if principal is not None and not principal.can_write:
        return {"error": "read-only credential cannot remember"}
    profile = override_profile(principal.profile) if principal is not None else nullcontext()
    with profile:
        if postgres_selected():
            from . import pgstore

            return pgstore.remember(
                fact, type=type, name=name, session=session, project=project,
                force=force, scope=scope, project_id=project_id, principal=principal,
            )
        conn = db.connect()
        try:
            return _remember_impl(
                conn, fact, type=type, name=name, session=session, project=project,
                force=force, scope=scope,
            )
        finally:
            conn.close()


# Register the full tool surface. Kept as explicit `mcp.tool(fn)` calls rather than
# `@mcp.tool` decorators so the plain functions stay importable and re-registerable:
# the read-only entry point (server_readonly.py) imports `recall`/`get_episode` and
# registers ONLY those two on its own FastMCP, leaving `remember` unreachable there.
mcp.tool(recall)
mcp.tool(get_episode)
mcp.tool(remember)


def _ensure_fresh_index() -> None:
    """Runs in a daemon thread: MCP initialize must never wait on ingest.
    recall serves whatever is already indexed meanwhile (WAL + busy_timeout).
    First-boot FULL builds are the CLI's job (`brain-ingest --full`); this
    only tops the index up incrementally."""
    try:
        # Load the C extensions here, off the tool path, so the first
        # recall/remember doesn't pay the numpy/OpenBLAS DLL init.
        try:
            import sqlite_vec  # noqa: F401  (pulls in numpy)
        except Exception:
            pass  # recall's own import will surface any real problem
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
            # Background caller: never wait on the single-writer lock. If a
            # manual `brain-ingest`, a session-refresh, or a peer server already
            # holds it, cleanly no-op — recall serves the existing index.
            stats = run_ingest(
                log=lambda s: print(f"[brain] {s}", file=sys.stderr), block=False
            )
            if stats.get("skipped"):
                print(
                    "[brain] startup ingest skipped: another writer holds the lock",
                    file=sys.stderr,
                )
            else:
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
    winstdin.install()  # before mcp.run() parks a CRT read on stdin (see winstdin)
    if postgres_selected():
        print(
            "[brain] Supabase backend; not ingesting a local SQLite index",
            file=sys.stderr,
        )
    else:
        threading.Thread(target=_ensure_fresh_index, name="brain-ingest", daemon=True).start()
    mcp.run()  # stdio transport — answers initialize immediately


if __name__ == "__main__":
    main()
