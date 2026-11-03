"""Shared plumbing for the device routes, plus the token-management MCP tools.

Device routes (``/ingest/*``, ``/hook/*``) authenticate with a device token
from :mod:`brain.device_tokens`, never with an OAuth bearer. Each route names
the one scope it needs. Errors never echo request bodies; logs carry the
device name and counts only.

The ``device_token_*`` MCP tools are registered on the full-access ``/mcp``
only, so minting needs a signed-in Google session with write access.
"""

from __future__ import annotations

import json
import logging

from .authz import load_auth_settings

logger = logging.getLogger(__name__)


class BodyTooLarge(Exception):
    pass


def bearer(request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


def _lookup(token: str, scope: str):
    from .device_tokens import verify
    from .pgstore import connection

    with connection() as conn:
        identity = verify(conn, token, scope, load_auth_settings())
        conn.commit()
    return identity


async def read_body(request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise BodyTooLarge
        except ValueError as exc:
            raise BodyTooLarge from exc
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


async def authed_json(request, scope: str, limit: int):
    """``(identity, payload, None)`` or ``(None, None, error_response)``."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    token = bearer(request)
    if not token:
        return None, None, JSONResponse(
            {"error": "missing bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        identity = await run_in_threadpool(_lookup, token, scope)
    except Exception as exc:
        logger.warning("device auth lookup failed: %s", type(exc).__name__)
        return None, None, JSONResponse(
            {"error": "store unavailable"}, status_code=503, headers={"Retry-After": "30"}
        )
    if identity is None:
        return None, None, JSONResponse(
            {"error": f"invalid, revoked, or out-of-scope device token (needs {scope})"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        body = await read_body(request, limit)
    except BodyTooLarge:
        return None, None, JSONResponse(
            {"error": f"body larger than {limit} bytes"}, status_code=413
        )
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None, JSONResponse({"error": "body is not JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return None, None, JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    return identity, payload, None


# ------------------------------------------------------------ MCP tools ---

def full_access_principal():
    """``(principal, None)`` for a signed-in writer, else ``(None, error)``."""
    from .authz import current_principal

    principal = current_principal()
    if principal is None:
        return None, {"error": "this tool needs the hosted /mcp endpoint"}
    if principal.error:
        return None, {"error": principal.error}
    if not principal.can_write:
        return None, {"error": "read-only credential cannot use this tool"}
    return principal, None


def device_token_create(device: str, profiles: str = "", scopes: str = "") -> dict:
    """Mint a device token for this machine's brain hooks and ``brain-push``.

    device: a short name for this machine, such as "work-laptop".
    profiles: "personal", "work", or both comma-separated; empty means every
    profile this account may use. A token limited to one profile stamps
    scope-less episodes with it and reads only that profile.
    scopes: "ingest" (push sessions), "hook-read" (SessionStart state brief
    and prompt-time memory snippets), or both; empty means both.

    The token is returned once and only its hash is stored. Write it straight
    to ~/.config/brain/device-token (or the BRAIN_DEVICE_TOKEN env var) on that
    device. Do not repeat it in chat. The token cannot call recall,
    get_episode, or remember.
    """
    from .device_tokens import allowed_profiles, mint, normalize_device, parse_scopes
    from .pgstore import connection

    principal, err = full_access_principal()
    if err:
        return err
    name = normalize_device(device)
    if not name:
        return {"error": "device must be a short name (letters, digits, . _ - space)"}
    chosen, perr = allowed_profiles(principal, load_auth_settings(), profiles)
    if perr:
        return {"error": perr}
    chosen_scopes, serr = parse_scopes(scopes)
    if serr:
        return {"error": serr}
    with connection() as conn:
        out = mint(conn, principal, name, chosen, chosen_scopes)
        conn.commit()
    out["note"] = "shown once; store it on the device, not in chat or a repo"
    return out


def device_token_list() -> dict:
    """List this account's device tokens (id, prefix, device, scopes, last use, revoked)."""
    from .device_tokens import list_tokens
    from .pgstore import connection

    principal, err = full_access_principal()
    if err:
        return err
    with connection() as conn:
        rows = list_tokens(conn, principal.sub or principal.email)
        conn.commit()
    return {"tokens": rows}


def device_token_revoke(id: int) -> dict:
    """Revoke one of this account's device tokens by its numeric id."""
    from .device_tokens import revoke
    from .pgstore import connection

    principal, err = full_access_principal()
    if err:
        return err
    with connection() as conn:
        ok = revoke(conn, principal.sub or principal.email, int(id))
        conn.commit()
    if not ok:
        return {"error": f"no active token {id} for this account"}
    return {"revoked": int(id)}


def device_token_revoke_hash(token_hash: str) -> dict:
    """Revoke this account's device token by the SHA-256 hex of its secret.

    ``brain-device-token create --force`` sends the hash of the token file it
    is about to replace, so the old token stops working. The secret itself is
    not an argument and is not returned.
    """
    from .device_tokens import revoke_by_hash
    from .pgstore import connection

    principal, err = full_access_principal()
    if err:
        return err
    with connection() as conn:
        token_id = revoke_by_hash(conn, principal.sub or principal.email, token_hash or "")
        conn.commit()
    if token_id is None:
        return {"error": "no active token for this account"}
    return {"revoked": token_id}
