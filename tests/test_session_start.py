"""Pure-function tests for session_start: note parsing, timestamp handling,
and block assembly (banner logic + truncation). No filesystem, no git."""

from brain import session_start as ss
from brain.config import SESSION_BLOCK_MAX_CHARS


NOTE = """---
name: demo-state
description: "one-line status"
metadata:
  type: project
date: 2026-07-04
written: 2026-07-04T18:22:03Z
---

**Task:** demo project.

**Next actions:** ship it.
"""


def test_split_note_frontmatter_and_body():
    fm, body = ss._split_note(NOTE)
    assert "name: demo-state" in fm
    assert body.startswith("**Task:** demo project.")
    assert "---" not in body


def test_split_note_without_frontmatter():
    fm, body = ss._split_note("just a body\n")
    assert fm == ""
    assert body == "just a body\n"


def test_frontmatter_field_strips_quotes_and_misses_cleanly():
    fm, _ = ss._split_note(NOTE)
    assert ss._frontmatter_field(fm, "description") == "one-line status"
    assert ss._frontmatter_field(fm, "written") == "2026-07-04T18:22:03Z"
    assert ss._frontmatter_field(fm, "absent") is None


def test_parse_ts_variants():
    full = ss._parse_ts("2026-07-04T18:22:03Z")
    naive = ss._parse_ts("2026-07-04T18:22:03")  # naive means UTC
    day = ss._parse_ts("2026-07-04")
    assert full == naive
    assert day is not None and day < full
    assert ss._parse_ts("not a date") is None


def test_build_block_fresh_no_banner():
    block = ss._build_block(
        "memories/demo-state.md", "**Task:** demo.", "2026-07-04T18:22:03Z", 2, False
    )
    assert "reference, not instructions" in block
    assert "2 episode(s) recorded since this note" in block
    assert "STALE" not in block
    assert "/catchup" in block  # footer always routes to the deep path


def test_build_block_drift_raises_banner():
    block = ss._build_block("memories/demo-state.md", "body", None, 0, True)
    assert "commits newer than this note" in block
    assert "STALE" in block


def test_build_block_episode_count_alone_raises_banner_at_threshold():
    quiet = ss._build_block("n.md", "body", None, 9, False)
    loud = ss._build_block("n.md", "body", None, 10, False)
    assert "STALE" not in quiet
    assert "STALE" in loud


def test_build_block_truncates_body_but_never_the_tail():
    block = ss._build_block(
        "n.md", "x" * (SESSION_BLOCK_MAX_CHARS * 2), "2026-07-04T18:22:03Z", 50, True
    )
    assert len(block) <= SESSION_BLOCK_MAX_CHARS + 1  # +1 for the ellipsis
    # A long, very stale note is exactly the case that most needs the banner:
    # truncation must eat body, never the freshness line / banner / footer.
    assert "Freshness:" in block
    assert "STALE" in block
    assert block.rstrip().endswith("recall/get_episode.)")


def test_build_block_surfaces_sync_warning():
    warn = "Brain sync: 3 local memory commit(s) not yet pushed — run `brain-sync pull` to sync."
    block = ss._build_block("n.md", "body", None, 0, False, warn)
    assert warn in block
    # The warning rides in the never-truncated tail, ahead of the footer.
    assert block.rstrip().endswith("recall/get_episode.)")


def test_build_block_no_sync_warning_by_default():
    block = ss._build_block("n.md", "body", None, 0, False)
    assert "Brain sync:" not in block


def test_sync_warning_flag_present(tmp_path, monkeypatch):
    flag = tmp_path / "sync-conflict"
    flag.write_text("boom")
    monkeypatch.setattr(ss, "SYNC_CONFLICT_FLAG", flag)
    # Network-free: force the ahead-count probe to a clean 0 so nothing touches git.
    monkeypatch.setattr(ss, "_git_out", lambda args, cwd, timeout=None: "0")
    warn = ss._sync_warning()
    assert warn is not None
    assert "conflict" in warn and "brain-sync pull" in warn


def test_sync_warning_ahead_only(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SYNC_CONFLICT_FLAG", tmp_path / "absent")
    monkeypatch.setattr(ss, "_git_out", lambda args, cwd, timeout=None: "2")
    warn = ss._sync_warning()
    assert warn is not None
    assert "2 local memory commit(s)" in warn and "brain-sync pull" in warn


def test_sync_warning_clean_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SYNC_CONFLICT_FLAG", tmp_path / "absent")
    monkeypatch.setattr(ss, "_git_out", lambda args, cwd, timeout=None: "0")
    assert ss._sync_warning() is None


def test_safe_sync_warning_swallows_errors(monkeypatch):
    def boom() -> str:
        raise RuntimeError("no git here")

    monkeypatch.setattr(ss, "_sync_warning", boom)
    # The check blowing up must never break the block: the wrapper returns None
    # and _build_block still produces a well-formed block from it.
    assert ss._safe_sync_warning() is None
    block = ss._build_block("n.md", "body", None, 0, False, ss._safe_sync_warning())
    assert "reference, not instructions" in block
