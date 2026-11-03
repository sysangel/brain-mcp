"""Lifecycle frontmatter from the PR #1 contract, as postgres columns will store it."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from brain.ids import memory_stable_id_v1, memory_stable_id_v2
from brain.records import eligible, parse, project_id


def test_parse_defaults_and_explicit_fields():
    raw = """---
name: project-alpha-state
project_id: Project-Alpha
status: archived
recall_enabled: false
expires_at: 2026-12-01
verified_at: 2026-10-18T16:00:00Z
---
body
"""
    got = parse(raw)
    assert got["project_id"] == "project-alpha"
    assert got["status"] == "archived"
    assert got["recall_enabled"] is False
    assert got["expires_at"].startswith("2026-12-01T00:00:00")
    assert got["verified_at"].startswith("2026-10-18T16:00:00")
    assert eligible(got) is False


def test_active_unexpired_is_eligible():
    got = parse("---\nstatus: active\nrecall_enabled: true\n---\n")
    assert eligible(got, now=datetime(2026, 1, 1, tzinfo=timezone.utc)) is True


def test_expired_is_ineligible_without_reingest():
    got = parse("---\nexpires_at: 2020-01-01T00:00:00Z\n---\n")
    assert eligible(got, now=datetime(2026, 1, 1, tzinfo=timezone.utc)) is False


def test_bad_status_raises():
    with pytest.raises(ValueError):
        parse("---\nstatus: deleted\n---\n")


def test_project_id_shape():
    assert project_id("Project-Alpha") == "project-alpha"
    with pytest.raises(ValueError):
        project_id("not a project")


def test_memory_v2_id_ignores_absolute_prefix():
    rel = "memories/widget.md"
    v2 = memory_stable_id_v2(rel)
    assert v2.startswith("mem_")
    assert len(v2) == 4 + 16
    assert memory_stable_id_v2(rel) != memory_stable_id_v1("/Users/a/src/brain/" + rel)
    assert memory_stable_id_v2(rel) == memory_stable_id_v2("memories\\widget.md")
