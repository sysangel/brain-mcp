"""Validated operational metadata for memories.

PR #1 stored these as git frontmatter projected into SQLite. The postgres
store keeps the same fields as real columns. Parsing stays here so the loader
and `remember` share one definition.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

FIELDS = ("project_id", "status", "recall_enabled", "expires_at", "verified_at")
DEFAULTS = dict(
    project_id=None,
    status="active",
    recall_enabled=True,
    expires_at=None,
    verified_at=None,
)


def project_id(value: str) -> str:
    value = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", value):
        raise ValueError(
            "project_id must be a non-empty identifier (letters, digits, . _ -)"
        )
    return value


def timestamp(value: str) -> str:
    """Canonical UTC timestamps; a date alone means midnight UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("timestamps with a time must include a timezone")
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except OverflowError as exc:
        raise ValueError("timestamp is outside the supported UTC range") from exc


def parse(raw: str) -> dict:
    out = DEFAULTS.copy()
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    seen: set[str] = set()
    closed = False
    for line in lines[1:]:
        if line.strip() == "---":
            closed = True
            break
        match = re.match(r"^([a-z_]+):\s*(.*?)\s*$", line)
        if not match or match[1] not in FIELDS:
            continue
        key, value = match.groups()
        if key in seen:
            raise ValueError(f"duplicate memory field: {key}")
        seen.add(key)
        if value.startswith('"'):
            value = json.loads(value)
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("''", "'")
        if key == "project_id":
            out[key] = project_id(value)
        elif key == "status":
            if value not in ("active", "archived", "forgotten"):
                raise ValueError("status must be active|archived|forgotten")
            out[key] = value
        elif key == "recall_enabled":
            if value not in ("true", "false"):
                raise ValueError("recall_enabled must be true|false")
            out[key] = value == "true"
        else:
            out[key] = timestamp(value)
    if not closed and seen:
        raise ValueError("unterminated memory frontmatter")
    return out


def eligible(record, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    expires = record.get("expires_at")
    if expires:
        exp = expires if isinstance(expires, datetime) else datetime.fromisoformat(
            str(expires).replace("Z", "+00:00")
        )
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= now:
            return False
    recall = record.get("recall_enabled")
    if recall in (0, "0", "false", False):
        return False
    return record.get("status", "active") == "active"
