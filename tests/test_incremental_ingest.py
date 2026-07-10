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
