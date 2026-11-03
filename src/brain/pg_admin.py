"""Apply ``supabase/migrations`` to the configured Postgres.

Idempotent: the SQL uses ``IF NOT EXISTS`` / ``CREATE OR REPLACE`` and the
RLS loop drops its policy before creating it. Safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .backend import database_url
from .sqlutil import split_sql

MIGRATIONS = Path(__file__).resolve().parents[2] / "supabase" / "migrations"


def apply_sql(conn, script: str, *, label: str = "sql") -> None:
    for i, stmt in enumerate(split_sql(script), start=1):
        try:
            conn.execute(stmt)
        except Exception as exc:
            preview = " ".join(stmt.split())[:180]
            raise RuntimeError(f"{label} statement {i} failed ({preview}): {exc}") from exc


def apply_migrations(conn, directory: Path | None = None) -> list[str]:
    directory = directory or MIGRATIONS
    applied: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        apply_sql(conn, path.read_text(encoding="utf-8"), label=path.name)
        applied.append(path.name)
    return applied


def migrate_main(argv: list[str] | None = None) -> int:
    del argv
    import psycopg

    url = database_url()
    with psycopg.connect(url, autocommit=True, prepare_threshold=None) as conn:
        names = apply_migrations(conn)
    print("applied: " + ", ".join(names), file=sys.stderr)
    return 0


def main() -> None:
    try:
        raise SystemExit(migrate_main())
    except Exception as exc:
        print(f"brain-pg-migrate: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
