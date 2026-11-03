"""HTTP MCP: Google allowlist and the two routes. Google is never contacted."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastmcp")

from fastmcp.server.auth.auth import AccessToken  # noqa: E402

from brain.authz import (  # noqa: E402
    AuthSettings,
    google_scopes_for_upstream,
    rejection_reason,
    resolve_principal,
)
from brain.google_auth import BrainGoogleProvider  # noqa: E402
from brain.http_server import build_http_app  # noqa: E402


def _settings(**overrides) -> AuthSettings:
    base = dict(
        domain="example.com",
        allowed_emails=frozenset({"you@example.com"}),
        email_profiles={"you@example.com": frozenset({"personal", "work"})},
        default_profile="personal",
        default_write=True,
        public_url="https://brain.example",
    )
    base.update(overrides)
    return AuthSettings(**base)


def _claims(email="you@example.com", hd="example.com", verified=True) -> dict:
    return {
        "email": email,
        "email_verified": verified,
        "hd": hd,
        "sub": "sub-1",
        "google_user_data": {"email": email, "hd": hd, "verified_email": verified},
    }


def test_domain_requires_hosted_domain_claim_not_email_suffix():
    settings = _settings(allowed_emails=frozenset())
    assert rejection_reason(
        {"email": "synthetic@example.com", "hd": "", "email_verified": True},
        settings,
    )
    assert rejection_reason(
        {"email": "synthetic@example.com", "hd": "example.com", "email_verified": True},
        settings,
    ) is None
    assert rejection_reason(
        {"email": "synthetic@gmail.com", "hd": "gmail.com", "email_verified": True},
        settings,
    )


def test_allowlist_rejects_other_domains_and_open_servers():
    settings = _settings()
    assert rejection_reason(_claims(), settings) is None
    assert rejection_reason(
        {"email": "someone@gmail.com", "hd": "gmail.com", "email_verified": True},
        settings,
    )
    assert rejection_reason(
        {"email": "you@example.com", "hd": "", "email_verified": False},
        settings,
    )
    open_server = _settings(domain="", allowed_emails=frozenset())
    assert rejection_reason(_claims(), open_server)


def test_scopes_select_profile_and_write_bit():
    settings = _settings()
    personal = resolve_principal(["brain:read", "brain:personal"], _claims(), settings)
    assert personal.profile == "personal" and personal.can_write is False
    write = resolve_principal(["brain:write", "brain:work"], _claims(), settings)
    assert write.profile == "work" and write.can_write is True
    denied = resolve_principal(
        ["brain:work"],
        _claims(),
        _settings(email_profiles={"you@example.com": frozenset({"personal"})}),
    )
    assert denied.error and "profile" in denied.error


def test_upstream_scopes_drop_brain_scopes():
    required = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
    got = google_scopes_for_upstream(["brain:read", "brain:write", "openid"], required)
    assert "brain:read" not in got and "brain:write" not in got
    assert "openid" in got


def _provider() -> BrainGoogleProvider:
    return BrainGoogleProvider(
        _settings(),
        client_id="test.apps.googleusercontent.com",
        client_secret="test-client-secret-not-real",
        base_url="https://brain.example",
        issuer_url="https://brain.example",
        jwt_signing_key="test-signing-key-32chars-minimum",
    )


def test_load_access_token_rejects_outsider(monkeypatch):
    provider = _provider()
    provider.set_mcp_path("/mcp")

    async def ok(_self, _token):
        return AccessToken(
            token="upstream",
            client_id="cid",
            scopes=["openid"],
            claims=_claims(),
        )

    monkeypatch.setattr("brain.google_auth.OAuthProxy.load_access_token", ok)
    jwt = provider.jwt_issuer.issue_access_token(
        client_id="cid",
        scopes=["brain:read", "brain:personal", "openid"],
        jti="jti-1",
        expires_in=600,
    )
    accepted = asyncio.run(provider.load_access_token(jwt))
    assert accepted is not None
    assert "brain:read" in accepted.scopes

    async def outsider(_self, _token):
        return AccessToken(
            token="upstream",
            client_id="cid",
            scopes=["openid"],
            claims=_claims(email="other@gmail.com", hd="gmail.com"),
        )

    monkeypatch.setattr("brain.google_auth.OAuthProxy.load_access_token", outsider)
    assert asyncio.run(provider.load_access_token(jwt)) is None


def test_http_app_advertises_mcp_and_mcp_ro_and_health():
    httpx = pytest.importorskip("httpx")
    app = build_http_app(_provider(), public_url="https://brain.example")

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://brain.example") as client:
            protected = await client.get("/.well-known/oauth-protected-resource/mcp")
            readonly = await client.get("/.well-known/oauth-protected-resource/mcp-ro")
            health = await client.get("/health")
            healthz = await client.get("/healthz")
            assert protected.status_code == 200, protected.text
            assert protected.json()["resource"].rstrip("/").endswith("/mcp")
            assert readonly.status_code == 200, readonly.text
            assert readonly.json()["resource"].rstrip("/").endswith("/mcp-ro")
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert healthz.status_code == 200
            assert healthz.json() == health.json()

    asyncio.run(_run())


def test_oauth_mapping_survives_a_new_instance(pg):
    """A replacement process with the same DATABASE_URL still accepts the token.

    FastMCP's default DiskStore does not. The signature verifies on both; the
    JTI mapping is what the empty store is missing.
    """
    import time

    from fastmcp.server.auth.oauth_proxy import JTIMapping, UpstreamTokenSet
    from key_value.aio.stores.memory import MemoryStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    from brain.oauth_store import PostgresKeyValue, encrypted_client_storage

    signing = "test-signing-key-32chars-minimum"
    secret = "test-client-secret-not-real"

    def _provider(storage):
        provider = BrainGoogleProvider(
            _settings(),
            client_id="test.apps.googleusercontent.com",
            client_secret=secret,
            base_url="https://brain.example",
            issuer_url="https://brain.example",
            jwt_signing_key=signing,
            client_storage=storage,
        )
        provider.set_mcp_path("/mcp")

        async def verify(_token):
            return AccessToken(
                token="upstream",
                client_id="cid",
                scopes=["openid"],
                claims=_claims(),
            )

        provider._token_validator.verify_token = verify
        return provider

    original = _provider(
        encrypted_client_storage(jwt_signing_key=signing, client_secret=secret)
    )
    jti = "jti-persist"
    upstream_id = "up-1"
    now = time.time()

    async def _seed_and_check():
        await original._jti_mapping_store.put(
            key=jti,
            value=JTIMapping(jti=jti, upstream_token_id=upstream_id, created_at=now),
        )
        await original._upstream_token_store.put(
            key=upstream_id,
            value=UpstreamTokenSet(
                upstream_token_id=upstream_id,
                access_token="google-access",
                refresh_token=None,
                refresh_token_expires_at=None,
                expires_at=now + 3600,
                token_type="Bearer",
                scope="openid",
                client_id="cid",
                created_at=now,
            ),
        )
        jwt = original.jwt_issuer.issue_access_token(
            client_id="cid",
            scopes=["brain:read", "openid"],
            jti=jti,
            expires_in=600,
        )
        assert await original.load_access_token(jwt) is not None
        # New object, same table and the same derived Fernet key.
        replacement = _provider(
            encrypted_client_storage(jwt_signing_key=signing, client_secret=secret)
        )
        assert isinstance(replacement._client_storage, FernetEncryptionWrapper)
        assert isinstance(replacement._client_storage.key_value, PostgresKeyValue)
        assert await replacement.load_access_token(jwt) is not None
        forgotten = _provider(MemoryStore())
        assert await forgotten.load_access_token(jwt) is None
        return jwt

    asyncio.run(_seed_and_check())
    # The pg fixture opened the pool; the store used it from a worker thread.
    pg.close_pool()


def test_stdio_dispatch_uses_postgres_when_selected(monkeypatch):
    from brain import server

    monkeypatch.setenv("BRAIN_BACKEND", "postgres")
    seen = {}

    def fake_recall(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return {"hits": [], "fts_only": True, "diag": {"n_hits": 0}}

    monkeypatch.setattr("brain.pgstore.recall", fake_recall)
    out = server.recall("widget quota")
    assert out == {"hits": [], "fts_only": True}
    assert seen["args"][0] == "widget quota"
