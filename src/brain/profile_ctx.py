"""Per-request profile override.

Stdio keeps ``BRAIN_SCOPE``. The HTTP server sets the signed-in profile for
the duration of a tool call. ``None`` inside the override is an explicit
neutral view; it does not fall through to ``BRAIN_SCOPE``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

from .config import PROFILE_SCOPE, normalize_scope


class _Unset:
    pass


UNSET = _Unset()
_profile: contextvars.ContextVar[str | None | _Unset] = contextvars.ContextVar(
    "brain_profile_override", default=UNSET
)


def profile_overridden() -> bool:
    """True while an HTTP tool call has set an explicit profile (including None)."""
    return _profile.get() is not UNSET


def current_profile() -> str | None:
    value = _profile.get()
    if value is UNSET:
        return PROFILE_SCOPE
    return value


@contextmanager
def override_profile(scope: str | None) -> Iterator[None]:
    token = _profile.set(normalize_scope(scope) if scope else None)
    try:
        yield
    finally:
        _profile.reset(token)
