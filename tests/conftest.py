"""Keep the existing suite on SQLite unless a test opts into Supabase.

A developer shell that exports DATABASE_URL must not flip ``brain-server``
tool tests onto Postgres. Postgres tests call ``pgstore`` directly and skip
when DATABASE_URL is unset.

Tests that request the ``pg`` or ``migrated_url`` fixture are marked
``postgres`` at collection. CI's sqlite job deselects that mark; the
supabase-sql job runs it against Postgres. A new database test is picked
up without editing the workflow. ``BRAIN_FORBID_DB_SKIP=1`` turns the
missing-URL skip into a failure so an unmarked database test cannot pass
by skipping.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure(config) -> None:
    if os.environ.get("BRAIN_PYTEST_POSTGRES") == "1":
        return
    os.environ.setdefault("BRAIN_BACKEND", "sqlite")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """Mark database tests before ``-m`` filtering runs.

    ``tryfirst`` matters: pytest's mark plugin deselects in this same hook,
    and it does not use ``tryfirst``, so it runs after this one.
    """
    for item in items:
        names = set(getattr(item, "fixturenames", ()))
        if {"pg", "migrated_url"} & names and item.get_closest_marker("postgres") is None:
            item.add_marker(pytest.mark.postgres)


def _database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL") or "").strip()
    if url:
        return url
    if os.environ.get("BRAIN_FORBID_DB_SKIP") == "1":
        pytest.fail(
            "Postgres test ran without DATABASE_URL. Tests that use the pg or "
            "migrated_url fixture are marked postgres and run in the "
            "supabase-sql job; this one was collected in a job with no database."
        )
    pytest.skip("DATABASE_URL not set")


@pytest.fixture(scope="session")
def migrated_url():
    import psycopg

    from brain.pg_admin import apply_migrations

    url = _database_url()
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        apply_migrations(conn)
        apply_migrations(conn)
    return url


@pytest.fixture
def pg(migrated_url, monkeypatch):
    from brain import pgstore

    monkeypatch.setenv("BRAIN_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("BRAIN_EMBED_MODEL", "text-embedding-3-small")
    monkeypatch.setenv("BRAIN_EMBED_DIM", "256")
    monkeypatch.setenv("DATABASE_URL", migrated_url)

    def _no_embed(*_a, **_k):
        raise RuntimeError("embed disabled in test")

    monkeypatch.setattr(pgstore, "embed_texts", _no_embed)
    monkeypatch.setattr(pgstore, "_embed_query", _no_embed)
    pgstore.close_pool()
    with pgstore.connection() as conn:
        conn.execute(
            """
            truncate
              brain.documents,
              brain.reflect_runs,
              brain.sync_events,
              brain.usage_recalls,
              brain.usage_opens,
              brain.users,
              brain.clients,
              brain.oauth_kv,
              brain.device_tokens,
              brain.job_locks,
              brain.git_exports
            restart identity cascade
            """
        )
        conn.execute(
            """
            delete from brain.meta
             where key = 'last_reflect'
                or key like 'last_reflect:%'
                or key in (
                  'last_git_export_sha', 'last_git_export_at',
                  'last_git_resync_sha', 'last_git_resync_at',
                  'ingested_git_head'
                )
            """
        )
        conn.commit()
    yield pgstore
    pgstore.close_pool()
