"""Google as the identity provider, FastMCP as the authorization server.

Uses FastMCP's ``GoogleProvider`` (an OAuth proxy): MCP clients see dynamic
client registration and PKCE, while Google sees one pre-registered OAuth
client. Brain-specific scopes stay on the FastMCP token and are stripped
before the upstream redirect. The Workspace domain and email allowlist are
checked on the identity Google returns, before a token is accepted.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.google import GoogleProvider
from mcp.server.auth.provider import AuthorizationParams

from .authz import (
    BRAIN_SCOPES,
    GOOGLE_SCOPES,
    AuthSettings,
    google_scopes_for_upstream,
    identity_from_claims,
    rejection_reason,
)

logger = logging.getLogger(__name__)


class BrainGoogleProvider(GoogleProvider):
    """GoogleProvider plus a Workspace/email allowlist and brain scopes."""

    def __init__(self, settings: AuthSettings, **kwargs):
        self.brain_settings = settings
        self._extra_resources: set[str] = set()
        extra = dict(kwargs.pop("extra_authorize_params", None) or {})
        if settings.domain and "hd" not in extra:
            extra["hd"] = settings.domain
        super().__init__(
            required_scopes=list(GOOGLE_SCOPES),
            extra_authorize_params=extra or None,
            **kwargs,
        )
        advertised = list(GOOGLE_SCOPES) + list(BRAIN_SCOPES)
        if self.client_registration_options is not None:
            self.client_registration_options.valid_scopes = advertised

    def allow_resource(self, url: str) -> None:
        """Accept this resource indicator in addition to the canonical /mcp URL."""
        self._extra_resources.add(url.rstrip("/"))

    def _canonicalize_resource(self, params: AuthorizationParams) -> AuthorizationParams:
        resource = getattr(params, "resource", None)
        if not resource or not getattr(self, "_resource_url", None):
            return params
        canon = str(self._resource_url).rstrip("/")
        allowed = {canon, *(u.rstrip("/") for u in self._extra_resources)}
        got = str(resource).rstrip("/")
        if got in allowed and got != canon:
            return params.model_copy(update={"resource": canon})
        return params

    async def authorize(self, client, params: AuthorizationParams) -> str:
        return await super().authorize(client, self._canonicalize_resource(params))

    def _build_upstream_authorize_url(self, txn_id: str, transaction: dict) -> str:
        txn = dict(transaction)
        txn["scopes"] = google_scopes_for_upstream(
            list(txn.get("scopes") or []),
            list(self.required_scopes or []),
        )
        return super()._build_upstream_authorize_url(txn_id, txn)

    def _prepare_scopes_for_upstream_refresh(self, scopes: list[str]) -> list[str]:
        return google_scopes_for_upstream(scopes, list(self.required_scopes or []))

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            payload = self.jwt_issuer.verify_token(token)
        except Exception:
            logger.debug("brain token rejected: JWT verification failed")
            return None
        validated = await OAuthProxy.load_access_token(self, token)
        if validated is None:
            return None
        claims = dict(validated.claims or {})
        reason = rejection_reason(identity_from_claims(claims), self.brain_settings)
        if reason:
            logger.info("rejected google identity: %s", reason)
            return None
        granted = [s for s in str(payload.get("scope") or "").split() if s]
        scopes: list[str] = []
        for scope in list(validated.scopes or []) + granted:
            if scope not in scopes:
                scopes.append(scope)
        claims["brain_scopes"] = [s for s in granted if s.startswith("brain:")]
        return AccessToken(
            token=validated.token,
            client_id=validated.client_id,
            scopes=scopes,
            expires_at=validated.expires_at,
            claims=claims,
        )
