"""Cloud Run MCP server: streamable HTTP, Google OAuth, Supabase underneath.

``/mcp`` exposes recall, get_episode, and remember, the device-token tools,
and the skill tools in ``hosted_tools`` (state notes, digests, handoffs,
proposals, memory maintenance, the reflect lease and window). ``/mcp-ro``
registers only the two read tools.

The device routes take a device token (``device_tokens``), not OAuth:
``POST /ingest/episodes`` (scope ``ingest``) is where ``brain-push`` sends
parsed sessions, and ``POST /hook/state`` / ``POST /hook/recall`` (scope
``hook-read``) back the SessionStart and UserPromptSubmit hooks.

There is no local SQLite and no startup ingest: the canonical store is
Supabase. FastMCP OAuth client registrations and token
mappings live in ``brain.oauth_kv`` (encrypted), so a replacement Cloud Run
instance — including after scale-to-zero — keeps accepting issued tokens.

Stdio (``brain-server``) can still talk to Supabase when ``DATABASE_URL`` is
set. It does not require OAuth.
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth.middleware import RequireAuthMiddleware
from fastmcp.server.http import StreamableHTTPASGIApp
from mcp.server.auth.routes import (
    build_resource_metadata_url,
    create_protected_resource_routes,
)
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyHttpUrl
from starlette.responses import JSONResponse
from starlette.routing import Route

from .authz import GOOGLE_SCOPES, load_auth_settings
from .backend import backend_name
from .device_api import (
    device_token_create,
    device_token_list,
    device_token_revoke,
    device_token_revoke_hash,
)
from .google_auth import BrainGoogleProvider
from .hook_api import hook_recall_route, hook_state_route
from .hosted_tools import TOOLS as SKILL_TOOLS
from .ingest_api import ingest_episodes_route
from .oauth_store import encrypted_client_storage
from .server import get_episode, recall, remember

_READ_METHODS = ["GET", "POST", "DELETE"]


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def build_google_provider(settings=None) -> BrainGoogleProvider:
    settings = settings or load_auth_settings()
    if not settings.public_url:
        raise SystemExit("BRAIN_PUBLIC_URL is required (the Cloud Run HTTPS origin)")
    if not settings.allowlist_configured:
        raise SystemExit(
            "Set BRAIN_AUTH_DOMAIN and/or BRAIN_AUTH_ALLOWED_EMAILS — "
            "the HTTP server refuses to run open to every Google account"
        )
    client_id = _env("BRAIN_GOOGLE_CLIENT_ID", "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID")
    client_secret = _env(
        "BRAIN_GOOGLE_CLIENT_SECRET", "FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET"
    )
    if not client_id or not client_secret:
        raise SystemExit(
            "Set BRAIN_GOOGLE_CLIENT_ID and BRAIN_GOOGLE_CLIENT_SECRET "
            "(Google Cloud OAuth client; never commit them)"
        )
    signing_key = _env("BRAIN_JWT_SIGNING_KEY") or None
    kwargs = {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": settings.public_url,
        "issuer_url": settings.public_url,
        "jwt_signing_key": signing_key,
        # Shared with every Cloud Run instance that has this DATABASE_URL.
        # Omit it and FastMCP falls back to a local DiskStore that dies on
        # instance replacement.
        "client_storage": encrypted_client_storage(
            jwt_signing_key=signing_key,
            client_secret=client_secret,
        ),
    }
    if not kwargs["jwt_signing_key"]:
        kwargs.pop("jwt_signing_key")
    return BrainGoogleProvider(settings, **kwargs)


def make_servers(auth):
    full = FastMCP("brain", auth=auth)
    full.tool(recall)
    full.tool(get_episode)
    full.tool(remember)
    # Token management and skill writes need a full-access session. Never on /mcp-ro.
    for tool in (
        device_token_create,
        device_token_list,
        device_token_revoke,
        device_token_revoke_hash,
        *SKILL_TOOLS,
    ):
        full.tool(tool)
    readonly = FastMCP("brain", auth=auth)
    readonly.tool(recall)
    readonly.tool(get_episode)
    return full, readonly


def build_http_app(auth, *, public_url: str | None = None):
    """Starlette app: OAuth routes + ``/mcp`` + ``/mcp-ro`` + ``/health`` +
    the device routes (``/ingest/episodes``, ``/hook/state``, ``/hook/recall``).

    ``/healthz`` is the same handler. Google's frontend 404s paths that end
    in ``z`` on ``run.app``, so probes must use ``/health``.
    """
    public_url = (public_url or os.environ.get("BRAIN_PUBLIC_URL") or "").rstrip("/")
    full, readonly = make_servers(auth)
    app = full.http_app(path="/mcp", transport="http")

    ro_manager = StreamableHTTPSessionManager(app=readonly._mcp_server)
    ro_asgi = StreamableHTTPASGIApp(ro_manager)
    required = list(getattr(auth, "required_scopes", None) or [])
    meta_url = None
    if public_url:
        ro_resource = AnyHttpUrl(f"{public_url}/mcp-ro")
        meta_url = build_resource_metadata_url(ro_resource)
        if hasattr(auth, "allow_resource"):
            auth.allow_resource(str(ro_resource))
        options = getattr(auth, "client_registration_options", None)
        scopes = list(getattr(options, "valid_scopes", None) or required or GOOGLE_SCOPES)
        issuer = getattr(auth, "issuer_url", None) or public_url
        issuer = issuer if isinstance(issuer, AnyHttpUrl) else AnyHttpUrl(str(issuer))
        for route in create_protected_resource_routes(
            resource_url=ro_resource,
            authorization_servers=[issuer],
            scopes_supported=scopes,
            resource_name="brain-readonly",
        ):
            app.router.routes.append(route)
    app.router.routes.append(
        Route(
            "/mcp-ro",
            endpoint=RequireAuthMiddleware(ro_asgi, required, meta_url),
            methods=_READ_METHODS,
        )
    )

    async def health(_request):
        return JSONResponse({"ok": True, "backend": backend_name()})

    app.router.routes.append(Route("/health", health, methods=["GET"]))
    app.router.routes.append(Route("/healthz", health, methods=["GET"]))
    # Device-token auth happens inside each handler; OAuth tokens are not accepted.
    app.router.routes.append(Route("/ingest/episodes", ingest_episodes_route, methods=["POST"]))
    app.router.routes.append(Route("/hook/state", hook_state_route, methods=["POST"]))
    app.router.routes.append(Route("/hook/recall", hook_recall_route, methods=["POST"]))

    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(parent):
        async with original(parent):
            async with ro_manager.run():
                yield

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    if backend_name() != "postgres":
        print(
            "[brain] HTTP server expects Supabase. Set DATABASE_URL to the "
            "Supavisor transaction-pooler URI (port 6543).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    settings = load_auth_settings()
    auth = build_google_provider(settings)
    app = build_http_app(auth, public_url=settings.public_url)
    host = os.environ.get("BRAIN_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT") or os.environ.get("BRAIN_HTTP_PORT") or "8080")
    print(
        f"[brain] http MCP on http://{host}:{port}/mcp "
        f"(read-only /mcp-ro), public {settings.public_url}, store supabase",
        file=sys.stderr,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
