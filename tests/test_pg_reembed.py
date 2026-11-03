"""OpenAI re-embed stages beside the live column and swaps in one transaction."""

from __future__ import annotations

import pytest

from brain.embedconf import OPENAI_REEMBED_SPACE
from brain.pg_reembed import describe, price_usd, run, swap


def test_price_is_the_published_small_model_rate():
    assert price_usd("text-embedding-3-small", 1_000_000) == 0.02
    text = describe(
        [{"kind": "memory", "title": "t", "description": "d", "body": "abcd" * 10}]
    )
    assert "text-embedding-3-small" in text
    assert text.startswith("1 eligible")


def test_stage_is_resumable_and_swap_is_atomic(pg, monkeypatch):
    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (stable_id, kind, title, description, body, content_hash)
            values
              ('mem_a', 'memory', 'a', 'alpha', 'alpha body', 'ha'),
              ('mem_b', 'memory', 'b', 'beta', 'beta body', 'hb')
            """
        )
        conn.commit()

    def fake_embed(texts, **_kwargs):
        return [[0.1] * 256 for _ in texts]

    monkeypatch.setattr("brain.pg_reembed.embed_texts", fake_embed)
    import psycopg
    from pgvector.psycopg import register_vector

    url = __import__("os").environ["DATABASE_URL"]
    with psycopg.connect(url, prepare_threshold=None) as conn:
        register_vector(conn)
        conn.execute("select set_config('search_path', 'brain, public, extensions', false)")
        first = run(conn, do_swap=False, require_complete=False, limit=1, estimate_only=False)
        def _n(row):
            return next(iter(row.values())) if isinstance(row, dict) else row[0]

        staged = _n(conn.execute(
            "select count(*) from brain.documents where embedding_next is not null"
        ).fetchone())
        live = _n(conn.execute(
            "select count(*) from brain.documents where embedding is not null"
        ).fetchone())
        assert first["staged"] == 1
        assert staged == 1
        assert live == 0
        with pytest.raises(RuntimeError, match="not staged"):
            swap(conn, require_complete=True)
        second = run(conn, do_swap=False, require_complete=False, limit=None, estimate_only=False)
        assert second["staged"] == 1  # the remaining row; the first is already staged
        swapped = run(conn, do_swap=True, require_complete=True, limit=None, estimate_only=False)
        assert swapped["swapped"] == 2
        space = _n(conn.execute(
            "select value from brain.meta where key = 'embed_space'"
        ).fetchone())
        pending = _n(conn.execute(
            "select count(*) from brain.documents where embedding_next is not null"
        ).fetchone())
        assert space == OPENAI_REEMBED_SPACE
        assert pending == 0
        calls = {"n": 0}

        def counting_embed(texts, **_kwargs):
            calls["n"] += len(texts)
            return [[0.2] * 256 for _ in texts]

        monkeypatch.setattr("brain.pg_reembed.embed_texts", counting_embed)
        again = run(conn, do_swap=True, require_complete=True, limit=None, estimate_only=False)
        assert again["summary"].startswith("0 eligible")
        assert again["staged"] == 0 and again["swapped"] == 0
        assert calls["n"] == 0

        conn.execute(
            """
            update brain.documents
               set body = 'alpha body edited', content_hash = 'ha2'
             where stable_id = 'mem_a'
            """
        )
        conn.commit()
        edited = run(conn, do_swap=False, require_complete=False, limit=None, estimate_only=False)
        assert edited["staged"] == 1
        assert calls["n"] == 1
        published = run(conn, do_swap=True, require_complete=True, limit=None, estimate_only=False)
        assert published["swapped"] == 1

        conn.execute(
            """
            update brain.documents
               set embed_space = 'local:other-model:256'
             where stable_id = 'mem_b'
            """
        )
        conn.commit()
        calls["n"] = 0
        stale = run(conn, do_swap=False, require_complete=False, limit=None, estimate_only=False)
        assert stale["staged"] == 1
        assert calls["n"] == 1
        only_b = _n(conn.execute(
            """
            select count(*) from brain.documents
             where embedding_next is not null and stable_id = 'mem_b'
            """
        ).fetchone())
        untouched = _n(conn.execute(
            """
            select count(*) from brain.documents
             where stable_id = 'mem_a' and embedding_next is null
               and embed_space = %s and embedded_hash = content_hash
            """,
            (OPENAI_REEMBED_SPACE,),
        ).fetchone())
        assert only_b == 1 and untouched == 1
