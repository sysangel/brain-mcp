"""Render a ``brain.documents`` row as the markdown ``brain-pg-load`` ingests.

The bytes are the inverse of the loader's parsers. ``render`` then
``build_doc`` then ``render`` is byte-identical, so an export that is loaded
again does not churn the file. Historical files are not reproduced bit for
bit: ``written:`` and summary ``episodes:`` lines are not stored as columns,
and a memory ``date`` is emitted only when it is the string that reproduces
``content_hash`` (``remember`` hashes with an empty date, so those files stay
dateless and the hash does not move).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import normalize_scope
from .ingest import _render_episode_file
from .pg_loader import summary_content_hash
from .records import timestamp as canonical_timestamp

_ROOTS = frozenset({"memories", "episodes", "summaries", "proposals", "handoffs"})


def check_relpath(relpath: str | None) -> str | None:
    """Return an error message when ``relpath`` must not be written to git."""
    if not relpath or relpath.startswith(("/", "\\")) or "\\" in relpath:
        return "unsafe relpath"
    parts = relpath.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "unsafe relpath"
    if parts[0] not in _ROOTS or not relpath.endswith(".md"):
        return "unsafe relpath"
    return None


def render_document(doc) -> bytes | None:
    """Markdown bytes for ``doc``, or None when the git path should be deleted.

    Delete when ``action`` is ``delete`` or ``status`` is ``forgotten``.
    Archived memories are written, with ``status: archived`` in frontmatter,
    including files under ``memories/archive/`` (the loader would force that
    status on the next import, and a missing line would show up as a diff).
    """
    action = _get(doc, "action") or "upsert"
    status = _get(doc, "status") or "active"
    if action == "delete" or status == "forgotten":
        return None
    kind = _get(doc, "kind")
    if kind == "memory":
        text = _render_memory(doc)
    elif kind == "episode":
        text = _render_episode(doc)
    elif kind == "summary":
        text = _render_summary(doc)
    elif kind in ("proposal", "handoff"):
        text = _render_loose(doc)
    else:
        raise ValueError(f"cannot render kind {kind!r}")
    return text.encode("utf-8")


def _render_memory(doc) -> str:
    relpath = _get(doc, "relpath") or ""
    slug = _get(doc, "title") or Path(relpath).name.removesuffix(".md")
    description = _get(doc, "description") or ""
    fact = (_get(doc, "body") or "").strip()
    attrs = _attrs(doc)
    mtype = attrs.get("type") or None
    scope = normalize_scope(_get(doc, "scope"))
    stored = _get(doc, "content_hash") or ""
    date = _pick_date(
        doc,
        lambda candidate: db.memory_content_hash(
            slug, description, mtype or "", fact, candidate, scope
        )
        == stored,
    )
    lines = [
        "---",
        f"name: {_one_line(slug)}",
        f"description: {_quote(description)}",
    ]
    project_id = _get(doc, "project_id")
    if project_id:
        lines.append(f"project_id: {_one_line(str(project_id))}")
    stamped = status_line(relpath, _get(doc, "status") or "active")
    if stamped:
        lines.append(f"status: {stamped}")
    if _get(doc, "recall_enabled") is False:
        lines.append("recall_enabled: false")
    for key in ("expires_at", "verified_at"):
        stamped = _canonical_ts(_get(doc, key))
        if stamped:
            lines.append(f"{key}: {stamped}")
    if date:
        lines.append(f"date: {date}")
    if mtype or scope:
        lines.append("metadata:")
        if mtype:
            lines.append(f"  type: {_one_line(str(mtype))}")
        if scope:
            lines.append(f"  scope: {scope}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + fact + "\n"


def status_line(relpath: str, status: str) -> str | None:
    """Frontmatter status to emit, or None for the loader's default (active)."""
    if relpath.startswith("memories/archive/") and status == "active":
        return "archived"
    if status != "active":
        return status
    return None


def _render_episode(doc) -> str:
    cwd = _get(doc, "cwd") or ""
    branch = _get(doc, "git_branch") or ""
    side = _sidechain(doc)
    text = _get(doc, "body") or ""
    ts = _episode_ts(
        doc,
        lambda candidate: db.episode_content_hash(candidate, cwd, branch, side, text)
        == (_get(doc, "content_hash") or ""),
    )
    digest = db.episode_content_hash(ts, cwd, branch, side, text)
    scope = normalize_scope(_get(doc, "scope"))
    return _render_episode_file(
        _get(doc, "stable_id") or "",
        digest,
        _get(doc, "session_id") or "",
        _get(doc, "uuid") or "",
        ts,
        cwd,
        branch,
        side,
        text,
        scope,
    )


def _render_summary(doc) -> str:
    session_id = _get(doc, "session_id") or ""
    project = _project_string(doc)
    cwd = _get(doc, "cwd") or ""
    host = _get(doc, "host") or ""
    description = _get(doc, "description") or ""
    text = (_get(doc, "body") or "").strip()

    stored = _get(doc, "content_hash") or ""

    def matches(candidate: str) -> bool:
        return (
            summary_content_hash(
                session_id, candidate, project, cwd, host, description, text
            )
            == stored
        )

    date = _pick_date(doc, matches)
    fields = [
        ("session_id", session_id),
        ("date", date),
        ("project", project),
        ("cwd", cwd),
        ("host", host),
    ]
    lines = ["---"]
    for key, value in fields:
        if value:
            lines.append(f"{key}: {_one_line(value)}")
    if description:
        lines.append(f"description: {_quote(description)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + text + "\n"


def _render_loose(doc) -> str:
    """Proposal/handoff bytes. Their ``content_hash`` is the sha of the file,
    so the date and quoting that reproduce it are picked when one does; a
    ``timestamptz`` keeps neither the original offset nor ``Z``. Otherwise the
    first candidate, unquoted, which is what the nightly drain has written."""
    relpath = _get(doc, "relpath") or ""
    stem = Path(relpath).name.removesuffix(".md")
    title = (_get(doc, "title") or stem).strip()
    description = _one_line(_get(doc, "description") or "")
    project = _one_line(_project_string(doc))
    body = (_get(doc, "body") or "").strip()
    has_heading = any(line.startswith("# ") for line in body.splitlines())
    if title and title != stem and not has_heading:
        body = f"# {title}\n\n{body}".strip()

    def text(date: str, quoted: bool) -> str:
        front = []
        if date:
            front.append(f"date: {date}")
        if project:
            front.append(f"project: {project}")
        if description:
            front.append(
                f'description: "{description}"' if quoted else f"description: {description}"
            )
        if not front:
            return (body + "\n") if body else "\n"
        return "---\n" + "\n".join(front) + "\n---\n\n" + body + "\n"

    stored = _get(doc, "content_hash") or ""
    dates = _loose_dates(doc)
    for date in dates:
        for quoted in (False, True):
            candidate = text(date, quoted)
            if hashlib.sha256(candidate.encode("utf-8")).hexdigest() == stored:
                return candidate
    return text(dates[0] if dates else "", False)


def _loose_dates(doc) -> list[str]:
    """``_date_candidates`` first (the drain's form), then the second-precision
    ``Z`` and whole-hour-offset spellings hand-written frontmatter uses."""
    found = _date_candidates(doc)
    ts = _get(doc, "ts")
    if isinstance(ts, datetime) and ts.microsecond == 0:
        utc = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        extra = [utc.strftime("%Y-%m-%dT%H:%M:%SZ")]
        extra += [
            utc.astimezone(timezone(timedelta(hours=h))).isoformat(timespec="seconds")
            for h in range(-12, 15)
        ]
        found += [value for value in extra if value not in found]
    return found


def _project_string(doc) -> str:
    attrs = _attrs(doc)
    project = attrs.get("project")
    if project:
        return str(project)
    return str(_get(doc, "project_id") or "")


def _pick_date(doc, matches) -> str:
    """The date string that reproduces ``content_hash``, else a stable fallback.

    Empty is tried first. ``remember`` stores that hash, and writing ``ts``
    (the row's insert time) into frontmatter would change the hash on the
    next load. When nothing matches, the first candidate is emitted so a
    second render — whose hash was computed from that same string — agrees.
    """
    candidates = _date_candidates(doc)
    for candidate in ["", *candidates]:
        if matches(candidate):
            return candidate
    return candidates[0] if candidates else ""


def _date_candidates(doc) -> list[str]:
    ts = _get(doc, "ts")
    found: list[str] = []

    def add(value: str) -> None:
        value = (value or "").strip()
        if value and value not in found:
            found.append(value)

    if isinstance(ts, datetime):
        utc = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        utc = utc.astimezone(timezone.utc)
        midnight = (
            utc.hour == utc.minute == utc.second == utc.microsecond == 0
        )
        if midnight:
            add(utc.date().isoformat())
        add(utc.isoformat(timespec="microseconds"))
        if not midnight:
            add(utc.date().isoformat())
        return found
    if isinstance(ts, str):
        add(ts)
        if len(ts) >= 10 and ts[4:5] == "-" and ts[7:8] == "-" and ts[:10] != ts:
            add(ts[:10])
    return found


def _episode_ts(doc, matches=lambda candidate: False) -> str:
    """The ts string ``content_hash`` was computed over, else a stable fallback.

    Every writer hashes the transcript's raw stamp (``…:38.669Z``), but a
    ``timestamptz`` keeps only the instant. The spelling that reproduces the
    stored hash wins, so an export matches both the row and the committed
    file. Otherwise the millisecond ``Z`` form when it is lossless.
    """
    ts = _get(doc, "ts")
    if ts is None:
        return ""
    if not isinstance(ts, datetime):
        return str(ts)
    utc = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    micro = utc.isoformat(timespec="microseconds")
    candidates = [micro, micro.replace("+00:00", "Z")]
    if utc.microsecond % 1000 == 0:
        candidates.insert(0, utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z")
    if utc.microsecond == 0:
        candidates.append(utc.strftime("%Y-%m-%dT%H:%M:%SZ"))
    for candidate in candidates:
        if matches(candidate):
            return candidate
    return candidates[0]


def _sidechain(doc) -> int:
    value = _attrs(doc).get("is_sidechain", 0)
    return 1 if value in (1, True, "1", "true") else 0


def _canonical_ts(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        utc = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return utc.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return canonical_timestamp(str(value))


def _attrs(doc) -> dict:
    raw = _get(doc, "attrs")
    if not raw:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return dict(raw)


def _quote(value: str) -> str:
    """Double-quote the way ``ingest._unquote`` reverses (``\\"`` -> ``"``)."""
    return '"' + _one_line(value).replace('"', '\\"') + '"'


def _one_line(value: str) -> str:
    return value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _get(doc, key: str, default=None):
    if isinstance(doc, dict):
        return doc.get(key, default)
    return getattr(doc, key, default)
