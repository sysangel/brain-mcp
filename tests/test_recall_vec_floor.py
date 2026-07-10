"""Recall's cosine-similarity floor on the vector leg (brain.db.recall).

vec0 KNN always returns its "least-far" rows even for an off-topic query, so
on a small corpus a nonsense query would otherwise inject weak vector mass into
RRF fusion. recall now drops vector hits whose cosine similarity falls below
WEAK_VEC_COS before appending them to the RRF lists (unit vectors:
cos = 1 - d^2/2).

Network-free and deterministic: fastembed is never loaded. `embed.available`
is stubbed True and `embed.embed_query` returns a hand-built unit vector keyed
on the query string, so cosine similarities are exact and controlled.
"""

from __future__ import annotations

import numpy as np
import pytest

from brain import db, embed
from brain.config import EMBED_DIM, WEAK_VEC_COS


def _unit(axis: int) -> np.ndarray:
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


def _seed_memory(conn, mem_id: int, stable_id: str, text: str, axis: int) -> None:
    """One memory row plus its unit-vector row on `axis` in memories_vec."""
    conn.execute(
        "INSERT INTO memories(id, stable_id, content_hash, embedded_hash, name, "
        "description, mtype, path, ts, text) VALUES "
        "(?, ?, 'h', 'h', ?, '', 'note', 'x.md', '2026-07-05', ?)",
        (mem_id, stable_id, stable_id, text),
    )
    conn.execute(
        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
        (mem_id, db.serialize_f32(_unit(axis))),
    )


def test_cosine_floor_filters_offtopic_but_keeps_ontopic(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "brain.db")
    if not db.has_vec(conn):
        conn.close()
        pytest.skip("sqlite-vec unavailable: the vector leg is skipped entirely")

    # Two docs on orthogonal axes. Their text shares NO tokens with the queries
    # below, so FTS contributes nothing — hits can only arrive via the vec leg.
    _seed_memory(conn, 1, "mem_aaaaaaaaaaaaaa01", "quorple fandango", axis=0)
    _seed_memory(conn, 2, "mem_bbbbbbbbbbbbbb02", "zibbet voracious", axis=1)
    conn.commit()

    # Query embeddings: "onquery" aligns with doc 1 (cos 1.0 >= floor); the
    # off-topic query lands on an unused axis (cos 0.0 with both docs).
    def fake_embed_query(text: str):
        return _unit(0) if text == "onquery" else _unit(9)

    monkeypatch.setattr(embed, "available", lambda: True)
    monkeypatch.setattr(embed, "embed_query", fake_embed_query)

    assert WEAK_VEC_COS > 0.0  # floor is engaged; cos 0.0 must fall below it

    # Off-topic: the raw vec leg DOES return rows (proving the floor, not an
    # empty search, is what suppresses them)...
    raw = db.vec_search(conn, "memory", _unit(9), limit=40)
    assert raw, "vec0 should still return least-far rows for the off-topic query"
    # ...yet recall yields nothing, because every vec hit is below the floor and
    # FTS matches no doc.
    off = db.recall(conn, "grelwix", k=6, scope="memories")
    assert off["hits"] == [], off["hits"]
    # Telemetry still reflects the true nearest neighbor (unfiltered best hit),
    # NOT the floor: the off-topic query sits on axis 9, orthogonal to both docs,
    # so the true nearest cosine is ~0.0 — well below the floor that dropped it.
    assert off["diag"]["top_vec_cos"] == pytest.approx(0.0, abs=1e-6)

    # On-topic: doc 1's vector matches exactly, so it clears the floor and lands.
    on = db.recall(conn, "onquery", k=6, scope="memories")
    assert [h["id"] for h in on["hits"]] == ["mem_aaaaaaaaaaaaaa01"], on["hits"]

    conn.close()
