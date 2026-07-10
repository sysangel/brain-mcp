"""dedup_check (brain.remember): decide whether a candidate fact duplicates an
existing memory before any write.

Two code paths:
  - vec path (sqlite-vec + embedder available): cosine over the reused document
    vector; exercised here with a stubbed, hand-built embedder so it is exact
    and network-free.
  - FTS + Jaccard fallback (no embedder): exact-normalized match, slug/name
    collision, and word-overlap; exercised with the embedder stubbed absent.

Every memory row is seeded directly; fastembed is never loaded.
"""

from __future__ import annotations

import numpy as np
import pytest

from brain import db, embed, remember
from brain.config import EMBED_DIM


def _seed(conn, mem_id, stable_id, name, description, text):
    conn.execute(
        "INSERT INTO memories(id, stable_id, content_hash, embedded_hash, name, "
        "description, mtype, path, ts, text) VALUES "
        "(?, ?, 'h', 'h', ?, ?, 'note', ?, '2026-07-05', ?)",
        (mem_id, stable_id, name, description, f"{name}.md", text),
    )


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


# --------------------------------------------------------- FTS-only fallback ---

def test_fallback_exact_normalized_match_is_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)  # force FTS/Jaccard path
    conn = db.connect(tmp_path / "brain.db")
    _seed(conn, 1, "mem_aaaaaaaaaaaaaa01", "greeting", "a greeting", "Hello   World")
    conn.commit()

    # Whitespace/case differ but normalize identically -> unambiguous duplicate.
    status, hit, sim, signal, vec = remember.dedup_check(conn, "a greeting", "hello world")
    assert status == "duplicate"
    assert signal == "exact"
    assert sim == 1.0
    assert hit["stable_id"] == "mem_aaaaaaaaaaaaaa01"
    assert vec is None  # embedder absent -> no reusable vector
    conn.close()


def test_fallback_slug_collision_is_possible_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    conn = db.connect(tmp_path / "brain.db")
    _seed(conn, 1, "mem_aaaaaaaaaaaaaa01", "personal-finance", "monthly budget", "some unrelated body")
    conn.commit()

    status, hit, sim, signal, _ = remember.dedup_check(
        conn, "different description", "totally different fact", slug="personal-finance"
    )
    assert status == "possible_duplicate"
    assert signal == "name"
    assert hit["name"] == "personal-finance"
    conn.close()


def test_fallback_word_overlap_flags_possible_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    conn = db.connect(tmp_path / "brain.db")
    _seed(
        conn, 1, "mem_aaaaaaaaaaaaaa01", "deploy", "deploy process",
        "the deploy pipeline runs ruff and pytest on every push",
    )
    conn.commit()

    # High Jaccard overlap with the stored text, routed via FTS.
    status, hit, sim, signal, _ = remember.dedup_check(
        conn, "deploy process", "the deploy pipeline runs ruff and pytest on every push now"
    )
    assert status == "possible_duplicate"
    assert signal == "jaccard"
    assert sim >= 0.5
    conn.close()


def test_fallback_no_match_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, "available", lambda: False)
    conn = db.connect(tmp_path / "brain.db")
    _seed(conn, 1, "mem_aaaaaaaaaaaaaa01", "cats", "about cats", "felines purr and nap")
    conn.commit()

    status, hit, sim, signal, _ = remember.dedup_check(
        conn, "quantum widgets", "orthogonal quorple fandango zibbet"
    )
    assert status is None
    assert hit is None
    assert signal is None
    conn.close()


# ------------------------------------------------------------------ vec path ---

def test_vec_path_cosine_flags_duplicate(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "brain.db")
    if not db.has_vec(conn):
        conn.close()
        pytest.skip("sqlite-vec unavailable: the cosine dedup path is skipped")

    _seed(conn, 1, "mem_aaaaaaaaaaaaaa01", "topicA", "desc A", "body on axis zero")
    conn.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
        (1, db.serialize_f32(_unit(0))),
    )
    conn.commit()

    # Embedder present and returns a vector identical to the stored one:
    # cosine 1.0 >= DUP_COSINE -> a measured "cosine" duplicate.
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda texts: [_unit(0)])

    # Fact is phrased differently from the stored text (so the exact-normalized
    # check cannot fire) — only the stubbed matching vector can flag it.
    status, hit, sim, signal, vec = remember.dedup_check(
        conn, "desc A", "a reworded paraphrase describing the axis zero topic"
    )
    assert status == "duplicate"
    assert signal == "cosine"
    assert sim == pytest.approx(1.0, abs=1e-6)
    assert hit["stable_id"] == "mem_aaaaaaaaaaaaaa01"
    assert vec is not None  # candidate vector is returned for index reuse
    conn.close()


def test_vec_path_orthogonal_candidate_is_not_flagged(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "brain.db")
    if not db.has_vec(conn):
        conn.close()
        pytest.skip("sqlite-vec unavailable: the cosine dedup path is skipped")

    _seed(conn, 1, "mem_aaaaaaaaaaaaaa01", "topicA", "desc A", "body on axis zero")
    conn.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
        (1, db.serialize_f32(_unit(0))),
    )
    conn.commit()

    # An orthogonal candidate: cosine 0.0, well below both dup thresholds, and
    # no slug/exact match -> not a duplicate.
    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed_documents", lambda texts: [_unit(1)])

    status, hit, sim, signal, vec = remember.dedup_check(conn, "desc Z", "orthogonal payload")
    assert status is None
    assert signal is None
    assert vec is not None
    conn.close()
