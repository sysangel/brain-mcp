"""MCP tools the ported skills use on the hosted ``/mcp`` (never ``/mcp-ro``).

Thin wrappers over :mod:`brain.hosted_docs`. Each one resolves the signed-in
principal, refuses read-only credentials for writes, and runs under the
principal's profile, the same pattern ``recall`` and ``remember`` follow.
"""

from __future__ import annotations

from contextlib import nullcontext

from . import hosted_docs
from .device_api import full_access_principal
from .profile_ctx import override_profile


def _reader():
    from .authz import current_principal

    principal = current_principal()
    if principal is None:
        return None, {"error": "this tool needs the hosted /mcp endpoint"}
    if principal.error:
        return None, {"error": principal.error}
    return principal, None


def _as(principal):
    return override_profile(principal.profile) if principal is not None else nullcontext()


def _write(fn, *args):
    principal, err = full_access_principal()
    if err:
        return err
    with _as(principal):
        return fn(*args)


def _read(fn, *args):
    principal, err = _reader()
    if err:
        return err
    with _as(principal):
        return fn(*args)


def state_get(project: str) -> dict:
    """Read a project's rolling state note (``memories/<project>-state.md``):
    body, description, ``written`` stamp, and its id. ``found`` is false when
    the project has no note yet."""
    return _read(hosted_docs.state_get, project, [])


def state_write(project: str, body: str, description: str = "") -> dict:
    """Overwrite a project's state note wholesale (never append). Used by
    /checkpoint and the reflect pass. ``written`` is stamped server-side; the
    SessionStart brief trusts it for freshness. Body up to ~6,000 chars;
    description is the one-line status (150 chars is the house target)."""
    return _write(hosted_docs.state_write, project, body, description)


def summary_write(
    session_id: str,
    body: str,
    description: str,
    project: str = "",
    cwd: str = "",
    episodes: int = 0,
) -> dict:
    """Write or rewrite the digest for one session (``summaries/YYYY-MM/<id>.md``).
    Scope follows the session's episodes: a work session's digest is not
    visible to personal, and an unscoped session stays shared. A rewrite keeps
    the existing path. Sessions under BRAIN_DIGEST_EXCLUDE are refused.
    ``episodes`` is the count the digest covers (defaults to the stored count)."""
    return _write(hosted_docs.summary_write, session_id, body, description, project, cwd, episodes)


def handoff_write(slug: str, body: str, description: str, project: str = "") -> dict:
    """Write a /handoff briefing to ``handoffs/<UTC timestamp>-<slug>.md``."""
    return _write(hosted_docs.handoff_write, slug, body, description, project)


def handoff_latest(topic: str = "", limit: int = 1) -> dict:
    """Newest handoff note(s), full text. ``topic`` filters on path, title, or
    description."""
    return _read(hosted_docs.handoff_latest, topic, limit)


def proposal_write(slug: str, body: str, description: str) -> dict:
    """Write a skill proposal to ``proposals/<UTC date>-<slug>.md``."""
    return _write(hosted_docs.proposal_write, slug, body, description)


def memory_update(id: str, fact: str, description: str = "") -> dict:
    """Overwrite an active memory's text in place (compaction merges,
    corrections). The id, name, type, scope, and lifecycle stay. Archived or
    forgotten memories are refused; memory_set_status changes lifecycle."""
    return _write(hosted_docs.memory_update, id, fact, description)


def memory_set_status(id: str, status: str) -> dict:
    """Retire or restore a memory: ``archived`` (out of recall, kept),
    ``forgotten`` (deleted from the git export), or ``active``."""
    return _write(hosted_docs.memory_set_status, id, status)


def reflect_claim(claimed_by: str = "") -> dict:
    """Claim the reflect lease for this session's profile. A work claim does
    not block a personal one. A neutral claim (no profile) still excludes
    every profile, and any open claim blocks a neutral one. ``ok`` false
    means the lease is held: abort. On success returns the claim ``id`` and
    ``window_start`` (that profile's watermark)."""
    principal, err = full_access_principal()
    if err:
        return err
    with _as(principal):
        return hosted_docs.reflect_claim(claimed_by or principal.email or "mcp")


def reflect_complete(id: int) -> dict:
    """Close the reflect claim ``id`` and advance the watermark to its claim
    time. Run it even on an empty pass."""
    return _write(hosted_docs.reflect_complete, id)


def reflect_window(since: str = "", limit: int = 200, offset: int = 0, chars: int = 600) -> dict:
    """Non-sidechain episodes since ``since`` (default: the reflect
    watermark), oldest first, each cut to ``chars``. Page with ``offset``."""
    return _write(hosted_docs.reflect_window, since, limit, offset, chars)


def reflect_sessions(limit: int = 5) -> dict:
    """Sessions that need a digest (new, or grown to double the episodes the
    digest covered), newest first, excluded projects dropped."""
    return _write(hosted_docs.reflect_sessions, limit)


def session_episodes(session_id: str, chars: int = 2500, limit: int = 200) -> dict:
    """One session's episodes in order, each cut to ``chars``, for digesting."""
    return _write(hosted_docs.session_episodes, session_id, chars, limit)


TOOLS = (
    state_get,
    state_write,
    summary_write,
    handoff_write,
    handoff_latest,
    proposal_write,
    memory_update,
    memory_set_status,
    reflect_claim,
    reflect_complete,
    reflect_window,
    reflect_sessions,
    session_episodes,
)
