"""Per-device tokens: mint, verify, list, revoke.

Hooks and scheduled catch-up jobs cannot do the browser OAuth flow, so a
signed-in full-access ``/mcp`` session mints one token per device. The token
is ``brn_dev_`` plus 32 random bytes (urlsafe base64). Only its SHA-256 is
stored. A plain hash is enough here: the input has 256 bits of entropy, so
there is nothing to brute-force, and a server-side pepper would tie every
device token to ``BRAIN_JWT_SIGNING_KEY`` rotation.

Scopes are explicit. ``ingest`` pushes episodes; ``hook-read`` reads the
state note and ambient memory snippets for the device's hooks. The tokens
are checked only by the device routes (``/ingest/*``, ``/hook/*``); the MCP
auth provider never sees them, so they cannot recall, read, or remember.
``profiles`` limits which episode scopes the device may write and which
profile its hook reads see.

Every request re-checks the account against the live auth settings, so
taking an email off the allowlist or narrowing its profiles also narrows
tokens minted earlier.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from .authz import AuthSettings, Principal

TOKEN_PREFIX = "brn_dev_"
SCOPE_INGEST = "ingest"
SCOPE_HOOK_READ = "hook-read"
SCOPES = (SCOPE_INGEST, SCOPE_HOOK_READ)
_PROFILES = ("personal", "work")
_TOKEN_RE = re.compile(r"^brn_dev_[A-Za-z0-9_-]{40,64}$")
# Anything shaped like a token, for scrubbing episode text before it is stored.
TOKEN_PATTERN = re.compile(r"brn_dev_[A-Za-z0-9_-]{20,}")
_DEVICE_MAX = 80


@dataclass(frozen=True)
class DeviceIdentity:
    token_id: int
    user_sub: str
    email: str
    device: str
    scopes: tuple[str, ...]
    profiles: tuple[str, ...]

    @property
    def profile(self) -> str | None:
        """The read profile: the single allowed one, else None (all)."""
        return self.profiles[0] if len(self.profiles) == 1 else None


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(token: str) -> bool:
    return bool(_TOKEN_RE.match(token or ""))


def redact_tokens(text: str) -> str:
    return TOKEN_PATTERN.sub("[redacted-device-token]", text or "")


def display_prefix(token: str) -> str:
    """First characters after the fixed prefix. Enough to tell tokens apart."""
    return token[: len(TOKEN_PREFIX) + 6]


def normalize_device(device: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]", "", (device or "").strip())[:_DEVICE_MAX].strip()


def _split(raw: str) -> set[str]:
    return {p.strip().lower() for p in (raw or "").replace("|", ",").split(",") if p.strip()}


def _ceiling(email: str, settings: AuthSettings) -> set[str]:
    mapped = settings.email_profiles.get(email)
    return set(mapped) if mapped is not None else set(_PROFILES)


def allowed_profiles(
    principal: Principal, settings: AuthSettings, requested: str = ""
) -> tuple[tuple[str, ...] | None, str | None]:
    """Profiles a new token may use, or an error.

    Empty ``requested`` means everything this account may use: its
    ``BRAIN_AUTH_EMAIL_PROFILES`` entry, or both profiles when it has none.
    """
    ceiling = _ceiling(principal.email, settings)
    want = _split(requested) or ceiling
    if want - set(_PROFILES):
        return None, "profiles must be personal and/or work"
    if want - ceiling:
        bad = ", ".join(sorted(want - ceiling))
        return None, f"profile not allowed for this account: {bad}"
    return tuple(p for p in _PROFILES if p in want), None


def parse_scopes(requested: str = "") -> tuple[tuple[str, ...] | None, str | None]:
    """Scopes for a new token. Empty means both ``ingest`` and ``hook-read``."""
    want = _split(requested) or set(SCOPES)
    unknown = want - set(SCOPES)
    if unknown:
        return None, "scopes must be ingest and/or hook-read"
    return tuple(s for s in SCOPES if s in want), None


def account_still_allowed(email: str, settings: AuthSettings) -> bool:
    """Defense in depth for a token minted earlier. The mint path already
    checked Google's ``hd`` claim; here only the stored email is known, so
    this re-applies the email allowlist and the domain suffix."""
    if not settings.allowlist_configured or not email or "@" not in email:
        return False
    if settings.domain and not email.endswith("@" + settings.domain):
        return False
    if settings.allowed_emails and email not in settings.allowed_emails:
        return False
    return True


def mint(
    conn,
    principal: Principal,
    device: str,
    profiles: tuple[str, ...],
    scopes: tuple[str, ...] = SCOPES,
) -> dict:
    token = new_token()
    row = conn.execute(
        """
        insert into brain.device_tokens
          (token_hash, token_prefix, user_sub, email, device, scopes, profiles)
        values (%s, %s, %s, %s, %s, %s, %s)
        returning id, created_at
        """,
        (
            hash_token(token),
            display_prefix(token),
            principal.sub or principal.email,
            principal.email,
            device,
            list(scopes),
            list(profiles),
        ),
    ).fetchone()
    return {
        "id": int(row[0]),
        "token": token,
        "prefix": display_prefix(token),
        "device": device,
        "profiles": list(profiles),
        "scopes": list(scopes),
        "created_at": row[1].isoformat(),
    }


def verify(
    conn,
    token: str,
    scope: str,
    settings: AuthSettings,
    *,
    touch: bool = True,
) -> DeviceIdentity | None:
    """The identity behind ``token`` when it is live and carries ``scope``.

    None when the token is malformed, unknown, revoked, lacks the scope, or
    belongs to an account the current settings no longer admit. Lookup is by
    hash, so no plaintext comparison happens; the stored hash is compared
    again with ``hmac.compare_digest`` to keep the check constant-time.
    """
    if scope not in SCOPES or not looks_like_token(token):
        return None
    digest = hash_token(token)
    row = conn.execute(
        """
        select id, token_hash, user_sub, email, device, scopes, profiles
          from brain.device_tokens
         where token_hash = %s and revoked_at is null
        """,
        (digest,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row[1], digest):
        return None
    scopes = tuple(row[5] or ())
    if scope not in scopes:
        return None
    email = (row[3] or "").lower()
    if not account_still_allowed(email, settings):
        return None
    ceiling = _ceiling(email, settings)
    profiles = tuple(p for p in (row[6] or ()) if p in ceiling)
    if not profiles:
        return None
    if touch:
        conn.execute(
            "update brain.device_tokens set last_used_at = now() where id = %s",
            (row[0],),
        )
    return DeviceIdentity(
        token_id=int(row[0]),
        user_sub=row[2],
        email=email,
        device=row[4],
        scopes=scopes,
        profiles=profiles,
    )


def list_tokens(conn, user_sub: str) -> list[dict]:
    rows = conn.execute(
        """
        select id, token_prefix, device, scopes, profiles,
               created_at, last_used_at, revoked_at
          from brain.device_tokens
         where user_sub = %s
         order by id
        """,
        (user_sub,),
    ).fetchall()
    return [
        {
            "id": int(r[0]),
            "prefix": r[1],
            "device": r[2],
            "scopes": list(r[3] or ()),
            "profiles": list(r[4] or ()),
            "created_at": r[5].isoformat(),
            "last_used_at": r[6].isoformat() if r[6] else None,
            "revoked_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def revoke_by_hash(conn, user_sub: str, token_hash: str) -> int | None:
    """Revoke this account's live token by its SHA-256 hex digest.

    None when the hash is malformed or no live token for ``user_sub`` has it.
    The plaintext secret never reaches the server.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", token_hash or ""):
        return None
    row = conn.execute(
        """
        update brain.device_tokens
           set revoked_at = now()
         where token_hash = %s and user_sub = %s and revoked_at is null
        returning id
        """,
        (token_hash, user_sub),
    ).fetchone()
    return int(row[0]) if row else None


def revoke(conn, user_sub: str, token_id: int) -> bool:
    row = conn.execute(
        """
        update brain.device_tokens
           set revoked_at = now()
         where id = %s and user_sub = %s and revoked_at is null
        returning id
        """,
        (token_id, user_sub),
    ).fetchone()
    return row is not None
