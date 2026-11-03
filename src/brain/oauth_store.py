"""Durable FastMCP OAuth state in Supabase.

FastMCP 2.14.7 stores dynamic client registrations, upstream tokens, and the
JTI-to-upstream mapping in ``client_storage``. The default is an encrypted
disk directory, which disappears when Cloud Run replaces an instance (including
scale-to-zero). This store is a table in the same Postgres database the MCP
already uses, wrapped with the same Fernet key FastMCP would have derived, so
a new instance accepts tokens issued by the one it replaced. Overlapping
revisions share the rows.

Values are encrypted before insert. The table is not a place to read tokens
from SQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, SupportsFloat

from cryptography.fernet import Fernet
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper


def _fernet(jwt_signing_key: str | None, client_secret: str) -> Fernet:
    """Match ``OAuthProxy``'s storage key so two instances encrypt the same way."""
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    if jwt_signing_key:
        signing = derive_jwt_key(
            low_entropy_material=jwt_signing_key,
            salt="fastmcp-jwt-signing-key",
        )
    else:
        signing = derive_jwt_key(
            high_entropy_material=client_secret,
            salt="fastmcp-jwt-signing-key",
        )
    storage_key = derive_jwt_key(
        high_entropy_material=signing.decode(),
        salt="fastmcp-storage-encryption-key",
    )
    return Fernet(key=storage_key)


class PostgresKeyValue:
    """``AsyncKeyValue`` backed by ``brain.oauth_kv``.

    Calls are synchronous psycopg on a worker thread. Each call is its own
    transaction (``prepare_threshold=None`` via the shared pool), so Supavisor
    transaction mode can hand the connection back between them.
    """

    def __init__(self) -> None:
        self._default = "default"

    def _collection(self, collection: str | None) -> str:
        return collection or self._default

    def _conn(self):
        from .pgstore import connection

        return connection()

    def _get_sync(self, key: str, collection: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                select value
                  from brain.oauth_kv
                 where collection = %s and key = %s
                   and (expires_at is null or expires_at > now())
                """,
                (collection, key),
            ).fetchone()
            conn.commit()
        if row is None:
            return None
        value = row[0]
        return dict(value) if isinstance(value, Mapping) else None

    def _ttl_sync(
        self, key: str, collection: str
    ) -> tuple[dict[str, Any] | None, float | None]:
        with self._conn() as conn:
            row = conn.execute(
                """
                select value, extract(epoch from (expires_at - now()))
                  from brain.oauth_kv
                 where collection = %s and key = %s
                   and (expires_at is null or expires_at > now())
                """,
                (collection, key),
            ).fetchone()
            conn.commit()
        if row is None:
            return None, None
        value = dict(row[0]) if isinstance(row[0], Mapping) else None
        ttl = None if row[1] is None else float(row[1])
        return value, ttl

    def _put_sync(
        self,
        key: str,
        value: Mapping[str, Any],
        collection: str,
        ttl: SupportsFloat | None,
    ) -> None:
        from psycopg.types.json import Jsonb

        seconds = None if ttl is None else float(ttl)
        with self._conn() as conn:
            conn.execute(
                """
                insert into brain.oauth_kv (collection, key, value, expires_at)
                values (
                  %s, %s, %s,
                  case
                    when %s::float8 is null then null
                    else now() + make_interval(secs => %s::float8)
                  end
                )
                on conflict (collection, key) do update
                  set value = excluded.value,
                      expires_at = excluded.expires_at
                """,
                (collection, key, Jsonb(dict(value)), seconds, seconds),
            )
            conn.commit()

    def _delete_sync(self, key: str, collection: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "delete from brain.oauth_kv where collection = %s and key = %s",
                (collection, key),
            )
            deleted = cur.rowcount > 0
            conn.commit()
        return deleted

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_sync, key, self._collection(collection))

    async def ttl(
        self, key: str, *, collection: str | None = None
    ) -> tuple[dict[str, Any] | None, float | None]:
        return await asyncio.to_thread(self._ttl_sync, key, self._collection(collection))

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._put_sync, key, value, self._collection(collection), ttl
        )

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        return await asyncio.to_thread(self._delete_sync, key, self._collection(collection))

    async def get_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[dict[str, Any] | None]:
        name = self._collection(collection)
        return [await self.get(key, collection=name) for key in keys]

    async def ttl_many(
        self, keys: Sequence[str], *, collection: str | None = None
    ) -> list[tuple[dict[str, Any] | None, float | None]]:
        name = self._collection(collection)
        return [await self.ttl(key, collection=name) for key in keys]

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        if len(keys) != len(values):
            raise ValueError("keys and values must be the same length")
        name = self._collection(collection)
        for key, value in zip(keys, values, strict=True):
            await self.put(key, value, collection=name, ttl=ttl)

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        name = self._collection(collection)
        deleted = 0
        for key in keys:
            if await self.delete(key, collection=name):
                deleted += 1
        return deleted


def encrypted_client_storage(
    *,
    jwt_signing_key: str | None,
    client_secret: str,
) -> FernetEncryptionWrapper:
    """FastMCP ``client_storage``: Fernet over ``brain.oauth_kv``."""
    return FernetEncryptionWrapper(
        key_value=PostgresKeyValue(),
        fernet=_fernet(jwt_signing_key, client_secret),
    )
