"""Read routes for the device hooks: ``POST /hook/state`` and ``POST /hook/recall``.

Both need a device token with the ``hook-read`` scope, and both read through
the token's profile (one allowed profile means that profile; both means the
neutral view), the same visibility rule the MCP tools apply.

``/hook/state`` backs the SessionStart brief. The client sends the project
name and the repo root spellings it resolved; the server returns the state
note, when it was written, how many episodes landed under that root since,
and the reflect debt so the hook can say a pass is due. The client adds the
repo-drift check itself because only it can see the repo.

``/hook/recall`` backs the UserPromptSubmit nudge. The client sends the
content tokens it already gated on, never the prompt. The server runs the
same quiet, lexical, memories-only search the local hook did (no embedding
call per prompt), applies the distinct-token gate, and returns snippets.
"""

from __future__ import annotations

import logging

from .config import HOOK_FTS_LIMIT, HOOK_MAX_HITS, HOOK_MIN_MATCH

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
_MAX_TOKENS = 12
_MAX_ROOTS = 8


def _str_list(value, cap: int, item_cap: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:cap]:
        if isinstance(item, str) and item.strip() and len(item) <= item_cap:
            out.append(item.strip())
    return out


def state_for(identity, payload: dict) -> dict:
    from psycopg.rows import dict_row

    from .hosted_docs import read_state, reflect_debt
    from .pgstore import connection

    project = payload.get("project")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project must be a non-empty string")
    roots = _str_list(payload.get("roots"), _MAX_ROOTS, 4096)
    with connection() as conn:
        conn.row_factory = dict_row
        out = read_state(conn, project.strip(), roots, identity.profile)
        if out.get("found"):
            out["reflect"] = reflect_debt(conn, identity.profile)
        conn.commit()
    return out


def recall_for(identity, payload: dict) -> dict:
    from psycopg.rows import dict_row

    from . import db
    from .pgstore import connection

    tokens = [t.lower() for t in _str_list(payload.get("tokens"), _MAX_TOKENS, 64)]
    if len(tokens) < 2:
        return {"hits": []}
    with connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            """
            select stable_id, title, description, body, relpath, ts
              from brain.recall_candidates(
                %s::text[], null::vector, array['memory']::text[], null::text,
                null::date, null::date, %s::text, null::text,
                %s, 0, 1.0, 60, now()
              )
             order by rrf desc
            """,
            (tokens, identity.profile, HOOK_FTS_LIMIT),
        ).fetchall()
        conn.commit()
    hits = []
    query = " ".join(tokens)
    for row in rows:
        if len(hits) >= HOOK_MAX_HITS:
            break
        haystack = " ".join(
            (row["title"] or "", row["description"] or "", row["body"] or "")
        ).lower()
        if sum(1 for tok in tokens if tok in haystack) < HOOK_MIN_MATCH:
            continue
        hits.append(
            {
                "key": f"memory:{row['relpath'] or row['stable_id']}",
                "id": row["stable_id"],
                "name": row["title"] or row["relpath"] or "?",
                "date": row["ts"].date().isoformat() if row["ts"] else "",
                "snippet": db.make_snippet(
                    f"{row['description'] or ''} {row['body'] or ''}", query, width=200
                ),
            }
        )
    return {"hits": hits}


async def _route(request, fn):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    from .device_api import authed_json
    from .device_tokens import SCOPE_HOOK_READ

    identity, payload, error = await authed_json(request, SCOPE_HOOK_READ, MAX_BODY_BYTES)
    if error is not None:
        return error
    try:
        out = await run_in_threadpool(fn, identity, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.warning("hook read failed: %s (device %s)", type(exc).__name__, identity.device)
        return JSONResponse({"error": "store unavailable"}, status_code=503)
    return JSONResponse(out)


async def hook_state_route(request):
    return await _route(request, state_for)


async def hook_recall_route(request):
    return await _route(request, recall_for)
