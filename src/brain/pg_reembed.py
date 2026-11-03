"""Resumable OpenAI re-embed into Supabase, then a single swap.

Target space is ``openai:text-embedding-3-small:256``. New vectors are written
to ``embedding_next`` and do not affect recall until ``--swap``. A crash
mid-run leaves the live column on the previous space (or null).

A row is pending only when it is eligible and neither of these is true:

* ``embedding_next`` is already staged for this content hash and target space
* the live ``embedding`` is already that space and hash (what ``--swap``
  publishes, then clears the staging columns)

A re-run after a successful swap is a no-op. New rows, edited bodies, and
vectors from another model or dimension are embedded. Inactive rows are not.
Cost is an estimate from ``len/4`` tokens at the published synchronous price,
printed before any API call.
"""

from __future__ import annotations

import argparse
import sys

from .backend import database_url
from .embed_api import embed_material, embed_texts
from .embedconf import (
    OPENAI_REEMBED_DIM,
    OPENAI_REEMBED_MODEL,
    OPENAI_REEMBED_SPACE,
    OPENAI_USD_PER_MTOK,
    approx_tokens,
)

# Two binds, both the target embed space (provider:model:dim).
_NOT_CURRENT = """
brain.is_eligible(status, recall_enabled, expires_at, now())
and not (
  (
    embedding_next is not null
    and embed_space_next = %s
    and embedded_hash_next = content_hash
  )
  or (
    embedding is not null
    and embed_space = %s
    and embedded_hash = content_hash
  )
)
"""

_PENDING = f"""
select id, stable_id, kind, title, description, body, content_hash
  from brain.documents
 where {_NOT_CURRENT}
 order by id
"""


def price_usd(model: str, tokens: int) -> float:
    rate = OPENAI_USD_PER_MTOK.get(model, OPENAI_USD_PER_MTOK[OPENAI_REEMBED_MODEL])
    return tokens * rate / 1_000_000


def describe(rows: list[dict], *, model: str = OPENAI_REEMBED_MODEL) -> str:
    tokens = 0
    for row in rows:
        tokens += approx_tokens(
            embed_material(row["kind"], row["title"], row["description"], row["body"])
        )
    cost = price_usd(model, tokens)
    return (
        f"{len(rows)} eligible rows, ~{tokens} tokens, "
        f"~${cost:.4f} at {model} (${OPENAI_USD_PER_MTOK.get(model, 0.02)}/1M)"
    )


def _space_params() -> tuple[str, str]:
    return (OPENAI_REEMBED_SPACE, OPENAI_REEMBED_SPACE)


def _rows(conn) -> list[dict]:
    from psycopg.rows import dict_row

    conn.row_factory = dict_row
    return list(conn.execute(_PENDING, _space_params()).fetchall())


def stage(conn, rows: list[dict], *, limit: int | None = None) -> int:
    """Fill ``embedding_next``. Returns how many rows were written this call."""
    pending = rows if limit is None else rows[:limit]
    written = 0
    batch = 32
    for start in range(0, len(pending), batch):
        chunk = pending[start : start + batch]
        texts = [
            embed_material(r["kind"], r["title"], r["description"], r["body"])
            for r in chunk
        ]
        from pgvector import Vector

        vectors = embed_texts(texts, model=OPENAI_REEMBED_MODEL, dim=OPENAI_REEMBED_DIM)
        for row, vec in zip(chunk, vectors):
            conn.execute(
                """
                update brain.documents
                   set embedding_next = %s,
                       embed_space_next = %s,
                       embedded_hash_next = %s
                 where id = %s
                   and content_hash = %s
                """,
                (Vector(vec), OPENAI_REEMBED_SPACE, row["content_hash"], row["id"], row["content_hash"]),
            )
            written += 1
        conn.commit()
    return written


def missing_for_swap(conn) -> int:
    row = conn.execute(
        """
        select count(*) as n
          from brain.documents
         where """ + _NOT_CURRENT + """
        """,
        _space_params(),
    ).fetchone()
    return int(row["n"] if isinstance(row, dict) else row[0])


def swap(conn, *, require_complete: bool) -> int:
    """Publish staged vectors in one transaction. Live recall keeps the old
    column until this commit."""
    if require_complete:
        missing = missing_for_swap(conn)
        if missing:
            conn.rollback()
            raise RuntimeError(
                f"{missing} eligible rows are not staged for {OPENAI_REEMBED_SPACE}; "
                "refusing to swap"
            )
    cur = conn.execute(
        """
        update brain.documents
           set embedding = embedding_next,
               embed_space = embed_space_next,
               embedded_hash = embedded_hash_next,
               embedding_next = null,
               embed_space_next = null,
               embedded_hash_next = null
         where embedding_next is not null
           and embed_space_next = %s
           and embedded_hash_next = content_hash
        """,
        (OPENAI_REEMBED_SPACE,),
    )
    conn.execute(
        """
        insert into brain.meta (key, value) values ('embed_space', %s)
        on conflict (key) do update
          set value = excluded.value, updated_at = now()
        """,
        (OPENAI_REEMBED_SPACE,),
    )
    conn.commit()
    return cur.rowcount


def run(conn, *, do_swap: bool, require_complete: bool, limit: int | None, estimate_only: bool) -> dict:
    rows = _rows(conn)
    summary = describe(rows)
    if estimate_only:
        return {"summary": summary, "staged": 0, "swapped": 0}
    staged = stage(conn, rows, limit=limit) if rows else 0
    swapped = 0
    if do_swap:
        swapped = swap(conn, require_complete=require_complete)
    return {"summary": summary, "staged": staged, "swapped": swapped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-embed Supabase documents with OpenAI text-embedding-3-small @ 256"
    )
    parser.add_argument("--estimate", action="store_true", help="print cost and exit")
    parser.add_argument("--swap", action="store_true", help="publish staged vectors in one transaction")
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="refuse --swap if any eligible row is not staged",
    )
    parser.add_argument("--limit", type=int, default=None, help="stage at most N rows (tests, resume)")
    args = parser.parse_args(argv)
    import psycopg
    from pgvector.psycopg import register_vector

    with psycopg.connect(database_url(), prepare_threshold=None) as conn:
        register_vector(conn)
        conn.execute("select set_config('search_path', 'brain, public, extensions', false)")
        result = run(
            conn,
            do_swap=args.swap,
            require_complete=args.require_complete,
            limit=args.limit,
            estimate_only=args.estimate,
        )
    print(result["summary"], file=sys.stderr)
    if not args.estimate:
        print(f"staged={result['staged']} swapped={result['swapped']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"brain-pg-reembed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
