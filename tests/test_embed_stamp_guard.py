"""Regression test for the embed-stamp length guard in run_ingest.

If the embedder returns fewer vectors than pending rows, `zip(pending, vecs)`
would silently insert only a prefix of vectors while embedded_hash is stamped
for every pending row — marking rows "embedded" that carry no vector. An explicit
RuntimeError (assert-free, survives PYTHONOPTIMIZE) now catches the mismatch.
Because it sits inside the soft-failure try/except,
the run degrades to FTS-only and leaves embedded_hash untouched (NULL), so a
later ingest retries the row rather than skipping it forever.

Network-free: the embedder is monkeypatched to return a short batch and every
path/scan/pull is redirected or stubbed, so nothing touches the real repo,
the network, or git.
"""

from __future__ import annotations

import pytest

from brain import db, embed, ingest, sync


def test_short_vector_batch_does_not_stamp_embedded(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.db"
    # Redirect every path ingest touches into the tmp dir.
    monkeypatch.setattr(db, "DB_PATH", db_path)
    monkeypatch.setattr(ingest, "DB_PATH", db_path)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)
    # No git pull, no filesystem scan.
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    monkeypatch.setattr(ingest, "find_transcripts", lambda: [])
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])

    conn = db.connect(db_path)
    if not db.has_vec(conn):
        conn.close()
        pytest.skip("sqlite-vec unavailable: the embedding path is skipped entirely")
    # Seed one un-embedded memory row (embedded_hash NULL, no vec row) so the
    # backfill loop has exactly one pending vector to produce.
    conn.execute(
        "INSERT INTO memories(stable_id, content_hash, embedded_hash, name, "
        "description, mtype, path, ts, text) VALUES "
        "('mem_test0000000001', 'h1', NULL, 'n', 'd', 'note', 'x.md', "
        "'2026-07-05', 'body')"
    )
    conn.commit()
    conn.close()

    # Embedder available but returns a SHORT batch (0 vectors for 1 pending row).
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda texts: [])

    logs: list[str] = []
    ingest.run_ingest(full=False, log=logs.append)

    # The guard fired inside the soft-failure try/except: logged, not raised.
    assert any("embedding failed" in m for m in logs), logs

    conn = db.connect(db_path)
    hash_after = conn.execute(
        "SELECT embedded_hash FROM memories WHERE stable_id = 'mem_test0000000001'"
    ).fetchone()[0]
    vec_count = conn.execute("SELECT count(*) FROM memories_vec").fetchone()[0]
    conn.close()

    # Row must NOT be stamped embedded, and no partial vector inserted.
    assert hash_after is None
    assert vec_count == 0
