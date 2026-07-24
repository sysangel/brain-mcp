"""Incremental ingest (brain.ingest.run_ingest): diff / delete / upsert against
a tmp store, plus embedding backfill when sqlite-vec is present.

run_ingest is driven end-to-end but sourced from a tmp directory: the source
scanners and the DB path are monkeypatched, the remote pull is stubbed to a
no-op, and (for the FTS-only cases) the embedder is stubbed absent so nothing
touches the network. A second cases stubs a deterministic embedder to assert
the vector backfill stamps embedded_hash.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from brain import db, embed, ingest, sync
from brain.config import EMBED_DIM


def _user_line(session, uuid, ts, text):
    return json.dumps({
        "type": "user",
        "message": {"content": text},
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": "/proj",
        "gitBranch": "main",
        "isSidechain": False,
    })


def _write_transcript(path, episodes):
    """episodes: list of (session, uuid, ts, user_text)."""
    path.write_text("\n".join(_user_line(*e) for e in episodes) + "\n")


def _wire(monkeypatch, tmp_path, transcripts, *, embedder=None):
    """Point run_ingest at a tmp DB + a fixed transcript list; stub the pull."""
    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "find_transcripts", lambda: list(transcripts))
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    # Episodes-in-git: keep these transcript-mode tests off the real repo/git —
    # emit into a tmp episodes/ dir, never scan it, never commit.
    monkeypatch.setattr(ingest, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(ingest, "find_episode_files", lambda: [])
    monkeypatch.setattr(ingest, "commit_episode_files", lambda paths, log=None: False)
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    if embedder is None:
        monkeypatch.setattr(embed, "available", lambda: False)  # FTS-only
    else:
        monkeypatch.setattr(embed, "available", lambda: True)
        monkeypatch.setattr(embed, "embed_documents", embedder)
    return dbp


def test_add_then_idempotent_then_update_and_delete(tmp_path, monkeypatch):
    t = tmp_path / "session.jsonl"
    _write_transcript(t, [
        ("s1", "u1", "2026-07-01T00:00:00Z", "first prompt about widgets"),
        ("s1", "u2", "2026-07-01T00:05:00Z", "second prompt about gadgets"),
    ])
    _wire(monkeypatch, tmp_path, [t])

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 2
    assert stats["episodes_updated"] == 0
    assert stats["episodes_deleted"] == 0

    # Idempotent: an unchanged corpus adds/updates/deletes nothing.
    stats = ingest.run_ingest()
    assert stats["files_changed"] == 0
    assert stats["episodes_added"] == 0
    assert stats["episodes_updated"] == 0
    assert stats["episodes_deleted"] == 0

    # Rewrite: drop u2 entirely, change u1's text. Bump mtime so the file is
    # re-scanned even if size happens to match.
    _write_transcript(t, [
        ("s1", "u1", "2026-07-01T00:00:00Z", "first prompt about widgets, revised heavily"),
    ])
    os.utime(t, (t.stat().st_atime + 10, t.stat().st_mtime + 10))

    stats = ingest.run_ingest()
    assert stats["episodes_deleted"] == 1  # u2 vanished from the source
    assert stats["episodes_updated"] == 1  # u1's content_hash changed
    assert stats["episodes_added"] == 0

    conn = db.connect(tmp_path / "brain.db")
    rows = {r["uuid"]: r["text"] for r in conn.execute("SELECT uuid, text FROM episodes")}
    assert set(rows) == {"u1"}  # only the surviving episode remains
    assert "revised heavily" in rows["u1"]
    # FTS index kept in sync by triggers: the deleted episode is unfindable,
    # the surviving one is findable.
    assert db.fts_search(conn, "episode", "gadgets", limit=5) == []
    assert db.fts_search(conn, "episode", "widgets", limit=5)
    conn.close()


def test_embedding_backfill_stamps_embedded_hash(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "probe.db")
    has_vec = db.has_vec(conn)
    conn.close()
    if not has_vec:
        pytest.skip("sqlite-vec unavailable: embedding backfill is skipped (FTS-only)")

    def fake_embed_documents(texts):
        # Deterministic unit vectors; content is irrelevant to the stamp assertion.
        out = []
        for i, _ in enumerate(texts):
            v = np.zeros(EMBED_DIM, dtype=np.float32)
            v[i % EMBED_DIM] = 1.0
            out.append(v)
        return out

    t = tmp_path / "session.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "a prompt to embed")])
    _wire(monkeypatch, tmp_path, [t], embedder=fake_embed_documents)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1
    assert stats["embedded"] == 1  # the one new episode was backfilled

    conn = db.connect(tmp_path / "brain.db")
    row = conn.execute("SELECT content_hash, embedded_hash FROM episodes").fetchone()
    assert row["embedded_hash"] == row["content_hash"]  # stamped to what was embedded
    # The vector row exists on the episode's rowid.
    ep_id = conn.execute("SELECT id FROM episodes").fetchone()["id"]
    vrow = conn.execute("SELECT rowid FROM episodes_vec WHERE rowid = ?", (ep_id,)).fetchone()
    assert vrow is not None
    conn.close()


def test_dual_session_id_fork_is_deduped(tmp_path, monkeypatch):
    # A session resume/fork rewrites the same messages into a NEW sessionId file:
    # identical ts+text but a different sessionId, so a different stable_id yet an
    # identical content_hash. ON CONFLICT(stable_id) can't collapse the pair; the
    # cross-file content-hash guard does. A's unique episode is kept.
    a = tmp_path / "sessA.jsonl"
    b = tmp_path / "sessB.jsonl"
    _write_transcript(a, [
        ("sA", "u1", "2026-07-01T00:00:00Z", "shared one"),
        ("sA", "u2", "2026-07-01T00:05:00Z", "shared two"),
        ("sA", "u3", "2026-07-01T00:10:00Z", "only in A"),
    ])
    _write_transcript(b, [
        ("sB", "u1", "2026-07-01T00:00:00Z", "shared one"),
        ("sB", "u2", "2026-07-01T00:05:00Z", "shared two"),
    ])
    _wire(monkeypatch, tmp_path, [a, b])

    stats = ingest.run_ingest()
    # A contributes 3; B's two episodes duplicate A's shared pair and are skipped.
    assert stats["episodes_added"] == 3

    conn = db.connect(tmp_path / "brain.db")
    total = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
    distinct = conn.execute("SELECT count(DISTINCT content_hash) FROM episodes").fetchone()[0]
    assert total == distinct == 3  # nothing double-indexed
    # The winning file keeps its shared episodes; the duplicate file keeps none.
    per_file = dict(conn.execute(
        "SELECT source_file, count(*) FROM episodes GROUP BY source_file"
    ))
    assert per_file == {str(a): 3}
    # B is still marked processed, so later runs mtime-skip it.
    assert conn.execute("SELECT count(*) FROM files WHERE path = ?", (str(b),)).fetchone()[0] == 1
    conn.close()

    # Idempotent: an unchanged corpus re-adds nothing (both files mtime-skip).
    stats = ingest.run_ingest()
    assert stats["files_changed"] == 0
    assert stats["episodes_added"] == 0
    assert stats["episodes_deleted"] == 0


def test_winner_file_deletion_reindexes_surviving_fork(tmp_path, monkeypatch):
    # Regression: when the fork copy that WON the cross-file dedup ages off disk,
    # its deduped episodes must not vanish from the index. The surviving fork
    # still holds identical copies on disk; pruning the winner invalidates
    # same-dir freshness so the survivor re-parses and re-indexes the
    # formerly-skipped duplicates in the same run.
    a = tmp_path / "sessA.jsonl"
    b = tmp_path / "sessB.jsonl"
    _write_transcript(a, [
        ("sA", "u1", "2026-07-01T00:00:00Z", "shared one"),
        ("sA", "u2", "2026-07-01T00:05:00Z", "shared two"),
        ("sA", "u3", "2026-07-01T00:10:00Z", "only in A"),
    ])
    _write_transcript(b, [
        ("sB", "u1", "2026-07-01T00:00:00Z", "shared one"),
        ("sB", "u2", "2026-07-01T00:05:00Z", "shared two"),
    ])
    _wire(monkeypatch, tmp_path, [a, b])

    ingest.run_ingest()  # A wins the shared pair; B's copies are skipped

    # A ages out: gone from disk and from the source list. B survives on disk
    # unchanged (its mtime/size are untouched), so only the prune-triggered
    # freshness invalidation can make it re-parse.
    a.unlink()
    monkeypatch.setattr(ingest, "find_transcripts", lambda: [b])

    stats = ingest.run_ingest()
    # B re-parses and re-indexes the two shared episodes it holds; A's unique u3
    # is correctly gone with A (it existed only in the deleted file).
    assert stats["episodes_added"] == 2
    assert stats["episodes_deleted"] == 0  # B never had rows to delete

    conn = db.connect(tmp_path / "brain.db")
    per_file = dict(conn.execute(
        "SELECT source_file, count(*) FROM episodes GROUP BY source_file"
    ))
    assert per_file == {str(b): 2}  # nothing lost: the survivor holds the shared pair
    texts = {r["text"] for r in conn.execute("SELECT text FROM episodes")}
    assert any("shared one" in t for t in texts)
    assert any("shared two" in t for t in texts)
    assert not any("only in A" in t for t in texts)  # unique-to-A episode gone with A
    conn.close()

    # Idempotent afterward: B now owns the episodes and mtime-skips.
    stats = ingest.run_ingest()
    assert stats["files_changed"] == 0
    assert stats["episodes_added"] == 0
    assert stats["episodes_deleted"] == 0


def test_similar_text_different_ts_not_deduped(tmp_path, monkeypatch):
    # The ts-in-hash boundary: identical user text sent under a different session
    # AND a different ts is a genuinely distinct episode (content_hash includes
    # ts), so the guard must NOT collapse it.
    a = tmp_path / "sessA.jsonl"
    b = tmp_path / "sessB.jsonl"
    _write_transcript(a, [("sA", "u1", "2026-07-01T00:00:00Z", "identical text")])
    _write_transcript(b, [("sB", "u1", "2026-07-01T09:00:00Z", "identical text")])
    _wire(monkeypatch, tmp_path, [a, b])

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 2  # both kept — the later-ts copy is distinct

    conn = db.connect(tmp_path / "brain.db")
    total = conn.execute("SELECT count(*) FROM episodes").fetchone()[0]
    distinct = conn.execute("SELECT count(DISTINCT content_hash) FROM episodes").fetchone()[0]
    assert total == distinct == 2
    conn.close()
