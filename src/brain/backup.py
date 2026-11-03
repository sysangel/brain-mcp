"""Logical backup of schema ``brain`` to Cloud Storage.

``pg_dump`` writes plain SQL. The hosted server is Postgres 17, so the
client in the image is postgresql-client-17. A 16 client refuses with
``server version: 17.x; pg_dump version: 16.x``. Bookworm's default client
is 15 and refuses either server. The object is gzipped and uploaded with
the Cloud Run metadata-server token. No service-account key file.

``brain_app`` is not the table owner, and row security is forced, so a
plain ``pg_dump`` errors with ``query would be affected by row-level
security policy``. ``--enable-row-security`` reads through the
``brain_server_all`` policy (``USING (true)``), which is every row.

Restore as an admin on Postgres 17 with pgvector. ``--no-owner`` leaves
objects owned by that admin. ``--no-privileges`` omits grants, so grant
``brain_server`` again after the load. Policies are in the dump;
``brain_server`` must exist first. ``vector`` is not in schema ``brain``.
See ``deploy/cloudrun/nightly.md``.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .backend import database_url


def _zone() -> ZoneInfo:
    """The zone that names the backup date. ``BRAIN_BACKUP_TZ`` (an IANA name,
    default ``UTC``) should match the scheduler's time zone so a 03:00 run is
    dated that local day."""
    name = (os.environ.get("BRAIN_BACKUP_TZ") or "UTC").strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"tzdata is missing {name}") from exc


def highest_pg_dump(root: str = "/usr/lib/postgresql") -> str | None:
    """Newest ``pg_dump`` under ``root``. A 16 client cannot dump a 17 server."""
    best: tuple[int, str] | None = None
    if not os.path.isdir(root):
        return None
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        path = os.path.join(root, name, "bin", "pg_dump")
        if os.path.isfile(path) and (best is None or int(name) > best[0]):
            best = (int(name), path)
    return None if best is None else best[1]


def pg_dump_bin() -> str:
    override = os.environ.get("PGDUMP", "").strip()
    if override:
        return override
    found = highest_pg_dump()
    if found:
        return found
    which = shutil.which("pg_dump")
    if not which:
        raise RuntimeError(
            "pg_dump is not installed (need postgresql-client-17; "
            "an older client refuses a Postgres 17 server)"
        )
    return which


def backup_date(now: datetime | None = None) -> str:
    override = os.environ.get("BRAIN_BACKUP_DATE", "").strip()
    if override:
        return override
    moment = now or datetime.now(_zone())
    return moment.strftime("%Y-%m-%d")


def dump_schema(url: str, schema: str = "brain") -> bytes:
    """Plain SQL for ``schema``, gzipped. The URL is not included in errors.

    ``--enable-row-security`` is required for ``brain_app``: RLS is forced,
    the role is not the owner, and it has no ``BYPASSRLS``. The only policy
    is ``USING (true)`` for ``brain_server``, which ``brain_app`` inherits,
    so the dump is the whole table.
    """
    binary = pg_dump_bin()
    proc = subprocess.run(
        [
            binary,
            f"--schema={schema}",
            "--no-owner",
            "--no-privileges",
            "--enable-row-security",
            "--format=plain",
            "--encoding=UTF8",
            "--no-password",
            "--dbname",
            url,
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")
        detail = detail.replace(url, "DATABASE_URL")
        raise RuntimeError(f"pg_dump failed: {detail.strip()[:500]}")
    if not proc.stdout:
        raise RuntimeError("pg_dump produced an empty file")
    return gzip.compress(proc.stdout, mtime=0)


def object_name(day: str | None = None) -> str:
    return f"brain/{day or backup_date()}.sql.gz"


def gcs_token(opener=None) -> str:
    """Runtime service account token from the metadata server."""
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"},
    )
    open_fn = opener or urllib.request.build_opener()
    try:
        with open_fn.open(req, timeout=5) as resp:
            payload = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Cloud Storage upload needs the Cloud Run metadata server "
            "(runtime service account). No key file is read."
        ) from exc
    import json

    return json.loads(payload)["access_token"]


def upload_gcs(bucket: str, name: str, data: bytes, *, token: str, opener=None) -> None:
    query = urllib.parse.urlencode({"uploadType": "media", "name": name})
    url = (
        "https://storage.googleapis.com/upload/storage/v1/b/"
        f"{urllib.parse.quote(bucket, safe='')}/o?{query}"
    )
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/gzip")
    open_fn = opener or urllib.request.build_opener()
    try:
        with open_fn.open(req, timeout=120) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"GCS upload failed ({exc.code}): {detail}") from exc


def backup_once(*, dump=None, upload=None, day: str | None = None) -> str:
    """Dump and upload. Returns the object name (``brain/YYYY-MM-DD.sql.gz``)."""
    bucket = os.environ.get("BRAIN_BACKUP_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("BRAIN_BACKUP_BUCKET is required")
    name = object_name(day or backup_date())
    payload = dump() if dump is not None else dump_schema(database_url())
    if upload is None:
        upload_gcs(bucket, name, payload, token=gcs_token())
    else:
        upload(bucket, name, payload)
    print(f"brain-backup: gs://{bucket}/{name}")
    return name


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        backup_once()
    except Exception as exc:
        print(f"brain-backup: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
