"""Schema shape: private brain schema, RLS, no anon policies, recall function."""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_postgres_marker_follows_the_db_fixture(request):
    """CI selects database tests by fixture, so a new file is not a silent skip."""
    unmarked = [
        item.nodeid
        for item in request.session.items
        if {"pg", "migrated_url"} & set(item.fixturenames)
        and item.get_closest_marker("postgres") is None
    ]
    assert unmarked == []


def test_migration_files_live_under_supabase():
    names = sorted(p.name for p in (ROOT / "supabase" / "migrations").glob("*.sql"))
    assert names == [
        "0001_brain_schema.sql",
        "0002_recall_candidates.sql",
        "0003_oauth_kv.sql",
        "0004_function_search_path.sql",
        "0005_git_export.sql",
        "0006_device_tokens.sql",
        "0007_summary_scope_and_reflect_profile.sql",
    ]
    sql = "\n".join(p.read_text() for p in (ROOT / "supabase" / "migrations").glob("*.sql"))
    assert "enable row level security" in sql
    assert "force row level security" in sql
    assert "to anon" not in sql
    assert "brain_server_all" in sql
    assert "create schema if not exists brain" in sql
    assert "supabase/functions" not in sql
    assert not (ROOT / "supabase" / "functions").exists()


def test_tables_and_policies(migrated_url):
    with psycopg.connect(migrated_url, prepare_threshold=None) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select tablename from pg_tables where schemaname = 'brain'"
            ).fetchall()
        }
        assert {
            "documents",
            "memory_id_aliases",
            "meta",
            "reflect_runs",
            "sync_events",
            "usage_recalls",
            "usage_recall_hits",
            "usage_opens",
            "users",
            "clients",
            "git_exports",
            "oauth_kv",
            "device_tokens",
            "job_locks",
        } <= tables
        policies = conn.execute(
            """
            select pol.polname, rol.rolname
              from pg_policy pol
              join pg_class cls on cls.oid = pol.polrelid
              join pg_namespace nsp on nsp.oid = cls.relnamespace
              join pg_roles rol on rol.oid = any (pol.polroles)
             where nsp.nspname = 'brain'
            """
        ).fetchall()
        assert policies, "expected brain_server policies"
        assert {name for _pol, name in policies} == {"brain_server"}
        rls = conn.execute(
            """
            select relname, relrowsecurity, relforcerowsecurity
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'brain' and c.relkind = 'r'
            """
        ).fetchall()
        assert all(enabled and forced for _name, enabled, forced in rls)
        fn = conn.execute(
            "select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
            "where n.nspname = 'brain' and p.proname = 'recall_candidates'"
        ).fetchone()
        assert fn is not None


def test_advisor_functions_pin_search_path_and_keep_behavior(migrated_url):
    """function_search_path_mutable: empty search_path, same results, immutable kept."""
    with psycopg.connect(migrated_url, prepare_threshold=None) as conn:
        rows = conn.execute(
            """
            select p.proname, p.provolatile, p.proconfig
              from pg_proc p
              join pg_namespace n on n.oid = p.pronamespace
             where n.nspname = 'brain'
               and p.proname in ('touch_updated_at', 'is_eligible', 'norm_segments')
            """
        ).fetchall()
        by_name = {name: (vol, config or []) for name, vol, config in rows}
        assert set(by_name) == {"touch_updated_at", "is_eligible", "norm_segments"}
        for name, (vol, config) in by_name.items():
            # Postgres stores an empty setting as search_path="".
            assert "search_path=\"\"" in config, (name, config)
        assert by_name["is_eligible"][0] == "i"
        assert by_name["norm_segments"][0] == "i"
        assert conn.execute(
            "select brain.is_eligible('active', true, null, now())"
        ).fetchone()[0] is True
        assert conn.execute(
            "select brain.is_eligible('archived', true, null, now())"
        ).fetchone()[0] is False
        assert conn.execute(
            "select brain.is_eligible('active', true, now() - interval '1 day', now())"
        ).fetchone()[0] is False
        assert conn.execute(
            "select brain.norm_segments(%s)", ("Foo/Bar\\Baz",)
        ).fetchone()[0] == "-foo-bar-baz-"
        conn.execute(
            """
            insert into brain.documents (stable_id, kind, body, content_hash)
            values ('mem_touch', 'memory', 'touch me', 'h-touch')
            """
        )
        conn.commit()
        inserted_at = conn.execute(
            "select updated_at from brain.documents where stable_id = 'mem_touch'"
        ).fetchone()[0]
        conn.execute(
            "update brain.documents set body = 'touched' where stable_id = 'mem_touch'"
        )
        conn.commit()
        body, updated_at = conn.execute(
            "select body, updated_at from brain.documents where stable_id = 'mem_touch'"
        ).fetchone()
        conn.execute("delete from brain.documents where stable_id = 'mem_touch'")
        conn.commit()
    assert body == "touched"
    assert updated_at > inserted_at


def test_rls_hides_rows_from_a_role_without_the_policy(migrated_url):
    with psycopg.connect(migrated_url, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(
            """
            insert into brain.documents (stable_id, kind, body, content_hash)
            values ('mem_rls', 'memory', 'visible only to brain_server', 'h')
            on conflict (stable_id) do nothing
            """
        )
        conn.execute(
            """
            do $$
            begin
              if not exists (select 1 from pg_roles where rolname = 'brain_rls_denied') then
                create role brain_rls_denied login password 'denied' nosuperuser nobypassrls;
              end if;
            end $$;
            """
        )
        conn.execute("grant usage on schema brain to brain_rls_denied")
        conn.execute("grant select on brain.documents to brain_rls_denied")
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(migrated_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    denied_url = urlunparse(parsed._replace(netloc=f"brain_rls_denied:denied@{host}:{port}"))
    try:
        with psycopg.connect(denied_url, prepare_threshold=None) as denied:
            count = denied.execute("select count(*) from brain.documents").fetchone()[0]
    except psycopg.OperationalError as exc:
        pytest.skip(f"could not connect as non-superuser: {exc}")
    assert count == 0
