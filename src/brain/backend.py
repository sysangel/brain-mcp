"""Which store this process talks to.

Production is Supabase Postgres. ``DATABASE_URL`` is the Supavisor
transaction-pooler URI (port 6543) on Cloud Run, and the direct URI
(port 5432) for ``brain-pg-migrate``, ``brain-pg-load``, and
``brain-pg-reembed``. ``SUPABASE_DB_URL`` is accepted as an alias.

``BRAIN_BACKEND=postgres`` (or ``pg`` / ``supabase``) selects that store.
``BRAIN_BACKEND=sqlite`` forces the local index used by tests and offline
stdio. When the variable is unset, a database URL selects Supabase and
anything else stays on SQLite.
"""

from __future__ import annotations

import os


def database_url() -> str:
    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("SUPABASE_DB_URL")
        or ""
    ).strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is required (Supabase Supavisor transaction URI on "
            "Cloud Run, or the direct db.*.supabase.co URI for migrations)"
        )
    return url


def backend_name() -> str:
    explicit = os.environ.get("BRAIN_BACKEND", "").strip().lower()
    if explicit in ("postgres", "pg", "supabase"):
        return "postgres"
    if explicit in ("sqlite", "local"):
        return "sqlite"
    if os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL"):
        return "postgres"
    return "sqlite"


def postgres_selected() -> bool:
    return backend_name() == "postgres"
