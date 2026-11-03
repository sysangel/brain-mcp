"""Logical backup: object name, gzip, and a restore onto a scratch database."""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from brain.backup import backup_date, backup_once, dump_schema, pg_dump_bin


def test_backup_uploads_dated_gzip(monkeypatch):
    monkeypatch.setenv("BRAIN_BACKUP_BUCKET", "my-brain-backups")
    monkeypatch.setenv("BRAIN_BACKUP_DATE", "2026-10-19")
    seen = {}

    def upload(bucket, name, data):
        seen["bucket"] = bucket
        seen["name"] = name
        seen["data"] = data

    name = backup_once(dump=lambda: gzip.compress(b"select 1;\n"), upload=upload, day="2026-10-19")
    assert name == "brain/2026-10-19.sql.gz"
    assert seen == {
        "bucket": "my-brain-backups",
        "name": "brain/2026-10-19.sql.gz",
        "data": gzip.compress(b"select 1;\n"),
    }
    assert backup_date() == "2026-10-19"


def test_backup_requires_a_bucket(monkeypatch):
    monkeypatch.delenv("BRAIN_BACKUP_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="BRAIN_BACKUP_BUCKET"):
        backup_once(dump=lambda: b"", upload=lambda *_a: None)


def _psql_bin() -> str | None:
    try:
        dump = pg_dump_bin()
    except RuntimeError:
        return None
    sibling = Path(dump).with_name("psql")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("psql")


def _major(text: str) -> int | None:
    import re

    match = re.search(r"\b(\d+)\.\d+", text)
    return int(match.group(1)) if match else None


def test_highest_pg_dump_prefers_the_newer_client(tmp_path):
    from brain.backup import highest_pg_dump

    for major in (16, 17):
        path = tmp_path / str(major) / "bin" / "pg_dump"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")
    assert highest_pg_dump(str(tmp_path)).endswith("/17/bin/pg_dump")


def _role_url(url: str, user: str, password: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{user}:{password}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _table_counts(conn) -> dict[str, int]:
    names = [
        row[0]
        for row in conn.execute(
            "select tablename from pg_tables where schemaname = 'brain' order by 1"
        )
    ]
    counts = {}
    for name in names:
        counts[name] = conn.execute(f'select count(*) from brain."{name}"').fetchone()[0]
    return counts


def test_pg_dump_restores_onto_a_scratch_database(pg, tmp_path):
    """Dump as a non-owner member of NOLOGIN brain_server, restore as admin.

    This is the production shape: RLS is forced, the only policy is
    brain_server_all FOR ALL USING (true) WITH CHECK (true), and the login
    cannot turn row security off. CI runs it on pgvector/pgvector:pg17.
    """
    psql = _psql_bin()
    if psql is None:
        pytest.skip("pg_dump/psql not installed")
    version = subprocess.run([pg_dump_bin(), "--version"], capture_output=True, text=True)
    with pg.connection() as conn:
        server = conn.execute("show server_version").fetchone()[0]
        conn.commit()
    client_major = _major(version.stdout)
    server_major = _major(server)
    if client_major is None or server_major is None or client_major < server_major:
        pytest.skip(
            f"pg_dump {version.stdout.strip()} cannot dump server {server}"
        )

    import psycopg

    url = os.environ["DATABASE_URL"]
    dumper = "brain_dump_app"
    dumper_password = "brain"
    # CREATE ROLE cannot run inside a transaction block.
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        if conn.execute(
            "select 1 from pg_roles where rolname = 'brain_dump_app'"
        ).fetchone() is None:
            conn.execute("create role brain_dump_app login password 'brain'")
        conn.execute("grant brain_server to brain_dump_app")
    with pg.connection() as conn:
        nologin = conn.execute(
            "select rolcanlogin from pg_roles where rolname = 'brain_server'"
        ).fetchone()
        assert nologin is not None and nologin[0] is False
        role = conn.execute(
            """
            select rolsuper, rolbypassrls, rolcanlogin,
                   pg_has_role(oid, 'brain_server', 'member')
              from pg_roles where rolname = 'brain_dump_app'
            """
        ).fetchone()
        assert role == (False, False, True, True)
        owned = conn.execute(
            """
            select count(*)
              from pg_class c
              join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'brain'
               and c.relkind = 'r'
               and pg_get_userbyid(c.relowner) = 'brain_dump_app'
            """
        ).fetchone()[0]
        assert owned == 0
        conn.execute(
            """
            insert into brain.documents (stable_id, kind, body, content_hash, relpath)
            values ('mem_backup_probe', 'memory', 'backup probe body', 'h-backup',
                    'memories/backup-probe.md')
            """
        )
        conn.execute(
            """
            insert into brain.clients (client_id, name)
            values ('backup-probe', 'probe')
            """
        )
        counts = _table_counts(conn)
        conn.commit()
    assert counts["documents"] >= 1
    assert counts["clients"] >= 1

    dumper_url = _role_url(url, dumper, dumper_password)
    with psycopg.connect(dumper_url, prepare_threshold=None) as conn:
        conn.execute("set row_security = on")
        assert conn.execute("select count(*) from brain.clients").fetchone()[0] == counts["clients"]
        conn.commit()
    with psycopg.connect(dumper_url, prepare_threshold=None, autocommit=True) as conn:
        conn.execute("set row_security = off")
        with pytest.raises(psycopg.Error, match="row-level security"):
            conn.execute("select count(*) from brain.clients")

    payload = dump_schema(dumper_url)
    sql = gzip.decompress(payload)
    assert b"backup probe body" in sql
    assert b"CREATE POLICY brain_server_all" in sql
    assert b"ENABLE ROW LEVEL SECURITY" in sql
    assert b"FORCE ROW LEVEL SECURITY" in sql
    assert b"GRANT " not in sql

    admin = url.rsplit("/", 1)[0] + "/postgres"
    maintenance = url
    try:
        with psycopg.connect(admin, autocommit=True, prepare_threshold=None) as conn:
            conn.execute("select 1")
        maintenance = admin
    except Exception:
        maintenance = url

    scratch = "brain_nightly_restore"
    with psycopg.connect(maintenance, autocommit=True, prepare_threshold=None) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
            (scratch,),
        )
        conn.execute(f"drop database if exists {scratch}")
        conn.execute(f"create database {scratch}")

    restore_url = url.rsplit("/", 1)[0] + f"/{scratch}"
    path = tmp_path / "brain.sql"
    path.write_bytes(sql)
    # Same cluster already has brain_server (NOLOGIN). A fresh server runs the
    # create role in nightly.md; duplicate_object is only because of that share.
    preamble = (
        "create extension if not exists vector",
        "do $$ begin create role brain_server nologin; "
        "exception when duplicate_object then null; end $$",
    )
    grants = (
        "grant usage on schema brain to brain_server",
        "grant select, insert, update, delete on all tables in schema brain to brain_server",
        "grant usage, select on all sequences in schema brain to brain_server",
    )
    try:
        for statement in preamble:
            proc = subprocess.run(
                [psql, restore_url, "-v", "ON_ERROR_STOP=1", "-c", statement],
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 0, proc.stderr
        proc = subprocess.run(
            [psql, restore_url, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        with psycopg.connect(restore_url, prepare_threshold=None) as conn:
            assert _table_counts(conn) == counts
            owner = conn.execute("select current_user").fetchone()[0]
            tables = conn.execute(
                """
                select c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       pg_get_userbyid(c.relowner)
                  from pg_class c
                  join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'brain' and c.relkind = 'r'
                 order by 1
                """
            ).fetchall()
            assert tables
            assert all(enabled and forced and who == owner for _n, enabled, forced, who in tables)
            assert all(who != dumper for _n, _e, _f, who in tables)
            policies = conn.execute(
                """
                select cls.relname, pg_get_expr(pol.polqual, pol.polrelid),
                       pg_get_expr(pol.polwithcheck, pol.polrelid)
                  from pg_policy pol
                  join pg_class cls on cls.oid = pol.polrelid
                  join pg_namespace nsp on nsp.oid = cls.relnamespace
                  join pg_roles rol on rol.oid = any (pol.polroles)
                 where nsp.nspname = 'brain'
                   and pol.polname = 'brain_server_all'
                   and rol.rolname = 'brain_server'
                """
            ).fetchall()
            assert {name for name, _q, _c in policies} == {name for name, *_ in tables}
            assert all(qual == "true" and check == "true" for _n, qual, check in policies)
            granted = conn.execute(
                """
                select count(*) from information_schema.role_table_grants
                 where table_schema = 'brain' and grantee = 'brain_server'
                """
            ).fetchone()[0]
            assert granted == 0
            for statement in grants:
                conn.execute(statement)
            conn.commit()
            granted = conn.execute(
                """
                select table_name, privilege_type
                  from information_schema.role_table_grants
                 where table_schema = 'brain' and grantee = 'brain_server'
                """
            ).fetchall()
            expected = {
                (name, priv)
                for name, *_rest in tables
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
            assert expected <= set(granted)
            sequences = conn.execute(
                """
                select count(*) from information_schema.usage_privileges
                 where object_schema = 'brain'
                   and grantee = 'brain_server'
                   and object_type = 'SEQUENCE'
                   and privilege_type = 'USAGE'
                """
            ).fetchone()[0]
            assert sequences >= 1
    finally:
        with psycopg.connect(maintenance, autocommit=True, prepare_threshold=None) as conn:
            conn.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (scratch,),
            )
            conn.execute(f"drop database if exists {scratch}")
            if conn.execute(
                "select 1 from pg_roles where rolname = 'brain_dump_app'"
            ).fetchone():
                conn.execute("revoke brain_server from brain_dump_app")
                conn.execute("drop owned by brain_dump_app")
                conn.execute("drop role brain_dump_app")
