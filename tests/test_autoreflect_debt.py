"""_debt (brain.autoreflect): count distinct non-sidechain sessions whose
episodes are newer than the last-reflect marker.

The bind is the stored last-reflect ISO-Z string, compared lexically against
episode `ts` (both ISO-8601 UTC-Z) — a plain string `>`. A None marker counts
from the epoch (`ts > ''` is always true). Network-free: episodes are seeded
directly into a tmp store.
"""

from __future__ import annotations

from datetime import datetime, timezone

from brain import autoreflect, db

_next_id = 0


def _seed_episode(conn, session_id, ts, *, is_sidechain=0):
    global _next_id
    _next_id += 1
    conn.execute(
        "INSERT INTO episodes(stable_id, content_hash, session_id, uuid, ts, cwd, "
        "git_branch, is_sidechain, source_file, text) "
        "VALUES (?, 'h', ?, ?, ?, '/x', 'main', ?, 'f.jsonl', 'body')",
        (f"ep_{_next_id:016d}", session_id, f"u{_next_id}", ts, is_sidechain),
    )


def _dt(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=timezone.utc)


def test_none_marker_counts_every_nonsidechain_session(tmp_path):
    conn = db.connect(tmp_path / "brain.db")
    _seed_episode(conn, "sess-A", "2026-07-01T00:00:00Z")
    _seed_episode(conn, "sess-A", "2026-07-01T01:00:00Z")  # same session -> counts once
    _seed_episode(conn, "sess-B", "2026-07-02T00:00:00Z")
    conn.commit()

    assert autoreflect._debt(conn, None) == 2  # distinct sessions A, B
    conn.close()


def test_marker_excludes_older_sessions(tmp_path):
    conn = db.connect(tmp_path / "brain.db")
    _seed_episode(conn, "sess-old", "2026-07-01T00:00:00Z")
    _seed_episode(conn, "sess-new", "2026-07-04T00:00:00Z")
    conn.commit()

    # Marker at 2026-07-03: only sess-new is newer.
    assert autoreflect._debt(conn, _dt(3)) == 1
    # Marker after both: no debt.
    assert autoreflect._debt(conn, _dt(5)) == 0
    conn.close()


def test_sidechain_and_null_sessions_are_excluded(tmp_path):
    conn = db.connect(tmp_path / "brain.db")
    _seed_episode(conn, "sess-main", "2026-07-02T00:00:00Z")
    _seed_episode(conn, "sess-side", "2026-07-02T00:00:00Z", is_sidechain=1)
    _seed_episode(conn, None, "2026-07-02T00:00:00Z")  # null session_id
    conn.commit()

    assert autoreflect._debt(conn, None) == 1  # only sess-main
    conn.close()
