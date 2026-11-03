"""Who a hosted request is, and what they may do.

Google identity (email, hosted-domain) is the allowlist. OAuth scopes on the
token select the profile (``brain:personal`` / ``brain:work``, replacing the
per-process ``BRAIN_SCOPE``) and whether ``remember`` is allowed
(``brain:write`` vs ``brain:read``). A token that carries neither brain scope
falls back to ``BRAIN_AUTH_DEFAULT_PROFILE`` / ``BRAIN_AUTH_DEFAULT_WRITE``.

Stdio has no token: ``current_principal()`` returns None and callers keep
``BRAIN_SCOPE``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import normalize_scope

GOOGLE_OPENID = "openid"
GOOGLE_EMAIL = "https://www.googleapis.com/auth/userinfo.email"
GOOGLE_PROFILE = "https://www.googleapis.com/auth/userinfo.profile"
_GOOGLE_SCOPE_ALIASES = frozenset({"openid", "email", "profile"})

BRAIN_READ = "brain:read"
BRAIN_WRITE = "brain:write"
BRAIN_PERSONAL = "brain:personal"
BRAIN_WORK = "brain:work"
BRAIN_SCOPES = (BRAIN_READ, BRAIN_WRITE, BRAIN_PERSONAL, BRAIN_WORK)
GOOGLE_SCOPES = (GOOGLE_OPENID, GOOGLE_EMAIL, GOOGLE_PROFILE)


@dataclass(frozen=True)
class AuthSettings:
    domain: str
    allowed_emails: frozenset[str]
    email_profiles: dict[str, frozenset[str]]
    default_profile: str | None
    default_write: bool
    public_url: str

    @property
    def allowlist_configured(self) -> bool:
        return bool(self.domain or self.allowed_emails)


@dataclass(frozen=True)
class Principal:
    email: str
    profile: str | None
    can_write: bool
    sub: str = ""
    error: str | None = None


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_auth_settings() -> AuthSettings:
    domain = os.environ.get("BRAIN_AUTH_DOMAIN", "").strip().lower().lstrip("@")
    emails = frozenset(
        e.lower() for e in _csv(os.environ.get("BRAIN_AUTH_ALLOWED_EMAILS", ""))
    )
    profiles: dict[str, frozenset[str]] = {}
    for part in _csv(os.environ.get("BRAIN_AUTH_EMAIL_PROFILES", "")):
        email, sep, spec = part.partition("=")
        if not sep or not email.strip():
            continue
        allowed = frozenset(
            p for p in spec.replace("|", " ").split() if p in ("personal", "work")
        )
        if allowed:
            profiles[email.strip().lower()] = allowed
    write_raw = os.environ.get("BRAIN_AUTH_DEFAULT_WRITE", "1").strip().lower()
    return AuthSettings(
        domain=domain,
        allowed_emails=emails,
        email_profiles=profiles,
        default_profile=normalize_scope(os.environ.get("BRAIN_AUTH_DEFAULT_PROFILE")),
        default_write=write_raw not in {"0", "false", "no"},
        public_url=os.environ.get("BRAIN_PUBLIC_URL", "").strip().rstrip("/"),
    )


def identity_from_claims(claims: dict | None) -> dict:
    claims = claims or {}
    user = claims.get("google_user_data") or {}
    if not isinstance(user, dict):
        user = {}
    email = str(claims.get("email") or user.get("email") or "").strip().lower()
    hd = str(claims.get("hd") or user.get("hd") or "").strip().lower()
    if "verified_email" in user:
        verified = user.get("verified_email")
    elif "email_verified" in user:
        verified = user.get("email_verified")
    else:
        verified = claims.get("email_verified")
    sub = str(claims.get("sub") or user.get("sub") or "")
    return {"email": email, "hd": hd, "email_verified": verified, "sub": sub}


def rejection_reason(identity: dict, settings: AuthSettings) -> str | None:
    """None when this Google identity may use the server. Fail closed."""
    if not settings.allowlist_configured:
        return "server auth allowlist is not configured"
    if identity.get("email_verified") is False:
        return "google account email is not verified"
    email = identity.get("email") or ""
    if not email or "@" not in email:
        return "google token has no email"
    hd = identity.get("hd") or ""
    if settings.domain:
        # Google's OpenID Connect docs: email domain does not establish
        # Workspace membership. Absent `hd` means the account is outside a
        # hosted domain, even when the address ends in the same suffix.
        if hd != settings.domain:
            return "google account is outside the allowed workspace domain"
    if settings.allowed_emails and email not in settings.allowed_emails:
        return "google account is not in the allowed email list"
    return None


def resolve_principal(
    scopes: list[str] | None, claims: dict | None, settings: AuthSettings
) -> Principal:
    identity = identity_from_claims(claims)
    email = identity["email"]
    sub = identity.get("sub") or email
    reason = rejection_reason(identity, settings)
    if reason:
        return Principal(email=email, profile=None, can_write=False, sub=sub, error=reason)

    brain = {s for s in (scopes or []) if s.startswith("brain:")}
    mapped = settings.email_profiles.get(email)
    requested: set[str] = set()
    if BRAIN_PERSONAL in brain:
        requested.add("personal")
    if BRAIN_WORK in brain:
        requested.add("work")
    if mapped is not None and requested - mapped:
        bad = ", ".join(sorted(requested - mapped))
        return Principal(
            email=email,
            profile=None,
            can_write=False,
            sub=sub,
            error=f"profile not allowed for this account: {bad}",
        )
    if len(requested) == 1:
        profile: str | None = next(iter(requested))
    elif mapped is not None and len(mapped) == 1:
        profile = next(iter(mapped))
    else:
        profile = settings.default_profile
        if mapped is not None and profile not in mapped:
            profile = next(iter(mapped)) if len(mapped) == 1 else None

    if BRAIN_WRITE in brain:
        can_write = True
    elif BRAIN_READ in brain:
        can_write = False
    else:
        can_write = settings.default_write
    return Principal(email=email, profile=profile, can_write=can_write, sub=sub)


def google_scopes_for_upstream(scopes: list[str] | None, required: list[str]) -> list[str]:
    """Scopes safe to send to Google. Brain scopes stay on the local token."""
    out: list[str] = []
    for scope in scopes or []:
        if scope in _GOOGLE_SCOPE_ALIASES or scope.startswith("https://www.googleapis.com/"):
            if scope not in out:
                out.append(scope)
    for scope in required:
        if scope not in out:
            out.append(scope)
    return out


def current_principal() -> Principal | None:
    """Signed-in principal for this call, or None on stdio / no token."""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    return resolve_principal(list(token.scopes or []), dict(claims), load_auth_settings())
