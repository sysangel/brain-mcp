"""Device tokens: hashing, scopes, profiles, revocation, live allowlist re-check."""

from __future__ import annotations

from brain.authz import AuthSettings, Principal
from brain.device_tokens import (
    SCOPE_HOOK_READ,
    SCOPE_INGEST,
    account_still_allowed,
    allowed_profiles,
    display_prefix,
    hash_token,
    list_tokens,
    looks_like_token,
    mint,
    new_token,
    parse_scopes,
    redact_tokens,
    revoke,
    revoke_by_hash,
    verify,
)


def _settings(**kw) -> AuthSettings:
    base = dict(
        domain="example.com",
        allowed_emails=frozenset(),
        email_profiles={},
        default_profile=None,
        default_write=True,
        public_url="https://brain.example",
    )
    base.update(kw)
    return AuthSettings(**base)


ME = Principal(email="you@example.com", profile=None, can_write=True, sub="sub-1")


def test_token_shape_and_hash_only():
    token = new_token()
    assert looks_like_token(token)
    digest = hash_token(token)
    assert len(digest) == 64 and token not in digest
    assert display_prefix(token) == token[:14]
    assert not looks_like_token("brn_dev_short")
    assert not looks_like_token("Bearer " + token)


def test_redact_scrubs_tokens_from_episode_text():
    token = new_token()
    text = f"saved {token} to the file"
    assert token not in redact_tokens(text)
    assert "[redacted-device-token]" in redact_tokens(text)


def test_profiles_and_scopes_respect_the_account_ceiling():
    settings = _settings(email_profiles={"you@example.com": frozenset({"work"})})
    assert allowed_profiles(ME, settings, "") == (("work",), None)
    assert allowed_profiles(ME, settings, "personal")[0] is None
    assert allowed_profiles(ME, _settings(), "")[0] == ("personal", "work")
    assert allowed_profiles(ME, _settings(), "bogus")[0] is None
    assert parse_scopes("") == ((SCOPE_INGEST, SCOPE_HOOK_READ), None)
    assert parse_scopes("hook-read") == ((SCOPE_HOOK_READ,), None)
    assert parse_scopes("recall")[0] is None


def test_account_recheck_uses_live_settings():
    assert account_still_allowed("you@example.com", _settings())
    assert not account_still_allowed("someone@gmail.com", _settings())
    assert not account_still_allowed(
        "you@example.com", _settings(allowed_emails=frozenset({"other@example.com"}))
    )
    assert not account_still_allowed("you@example.com", _settings(domain=""))


def test_mint_verify_scope_revoke(pg):
    settings = _settings()
    with pg.connection() as conn:
        ingest_only = mint(conn, ME, "laptop", ("personal", "work"), (SCOPE_INGEST,))
        both = mint(conn, ME, "desktop", ("work",))
        conn.commit()
    with pg.connection() as conn:
        stored = conn.execute("select token_hash, token_prefix from brain.device_tokens order by id").fetchall()
        conn.commit()
    assert all(row[0] != ingest_only["token"] and row[0] != both["token"] for row in stored)
    assert stored[0][0] == hash_token(ingest_only["token"])

    with pg.connection() as conn:
        ident = verify(conn, ingest_only["token"], SCOPE_INGEST, settings)
        assert ident is not None and ident.device == "laptop"
        assert ident.profile is None
        assert verify(conn, ingest_only["token"], SCOPE_HOOK_READ, settings) is None
        work = verify(conn, both["token"], SCOPE_HOOK_READ, settings)
        assert work is not None and work.profile == "work"
        assert verify(conn, ingest_only["token"] + "x", SCOPE_INGEST, settings) is None
        conn.commit()
    with pg.connection() as conn:
        rows = list_tokens(conn, "sub-1")
        conn.commit()
    assert [r["device"] for r in rows] == ["laptop", "desktop"]
    assert rows[0]["last_used_at"] is not None
    assert "token" not in rows[0]

    with pg.connection() as conn:
        assert revoke(conn, "someone-else", ingest_only["id"]) is False
        assert revoke(conn, "sub-1", ingest_only["id"]) is True
        assert revoke(conn, "sub-1", ingest_only["id"]) is False
        conn.commit()
    with pg.connection() as conn:
        assert verify(conn, ingest_only["token"], SCOPE_INGEST, settings) is None
        conn.commit()


def test_revoke_by_hash_is_limited_to_the_account(pg):
    settings = _settings()
    with pg.connection() as conn:
        minted = mint(conn, ME, "laptop", ("work",))
        conn.commit()
    digest = hash_token(minted["token"])
    with pg.connection() as conn:
        assert revoke_by_hash(conn, "someone-else", digest) is None
        assert revoke_by_hash(conn, "sub-1", "not-a-hash") is None
        assert revoke_by_hash(conn, "sub-1", digest) == minted["id"]
        assert revoke_by_hash(conn, "sub-1", digest) is None
        assert verify(conn, minted["token"], SCOPE_INGEST, settings) is None
        conn.commit()


def test_verify_narrows_profiles_and_drops_removed_accounts(pg):
    with pg.connection() as conn:
        out = mint(conn, ME, "laptop", ("personal", "work"))
        conn.commit()
    narrowed = _settings(email_profiles={"you@example.com": frozenset({"work"})})
    with pg.connection() as conn:
        ident = verify(conn, out["token"], SCOPE_INGEST, narrowed)
        assert ident.profiles == ("work",)
        gone = _settings(allowed_emails=frozenset({"other@example.com"}))
        assert verify(conn, out["token"], SCOPE_INGEST, gone) is None
        conn.commit()
