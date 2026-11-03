# Nightly backup and git export

Cloud Run Job `brain-nightly` runs `brain-backup`, then `brain-git-export`.
One step failing does not skip the other. The process exits non-zero if
either failed.

The backup is a logical `pg_dump` of schema `brain` at
`gs://PROJECT_ID-brain-backups/brain/YYYY-MM-DD.sql.gz`. The date is taken in
`BRAIN_BACKUP_TZ` (an IANA zone name, default `UTC`); set it to the
scheduler's time zone. The export drains `brain.git_exports` into one
commit on `main` of the repo named by `BRAIN_GIT_REPO` (`OWNER/brain`).

Supabase free projects pause after about a week with no connections. This
job connects every night, which keeps the project from auto-pausing. It is
not a substitute for the Pro plan's physical backups: free projects have no
restorable backup of their own, which is why the dump exists.

Do not put `GITHUB_TOKEN` on the MCP service. Only the job has it.

## What the export writes

`remember` inserts a `git_exports` row (`action` defaults to `upsert`,
`committed` stays false). Nothing else drains it until this job.

The markdown is the format `brain-pg-load` parses. Rendering a row, loading
those bytes, and rendering again does not change the file. `action = delete`
removes the path. `status = forgotten` removes it too. `status = archived`
is written as `status: archived`, including under `memories/archive/`.

Deleting a `documents` row cascades the outbox row, so the drain never sees
it. To remove a file from git, set `status = forgotten` (or `action = delete`)
and leave a pending outbox row.

`brain.meta.ingested_git_head` stays the commit `brain-pg-load` imported.
An export commit is previous `main` plus the outbox files, not a dump of the
database, so it is not that key. The sha is stored on
`git_exports.exported_sha` and on `meta.last_git_export_sha` /
`meta.last_git_export_at`.

A retry that finds the blobs already on `main` does not create a second
commit; it stamps the current HEAD. If `main` moves, the ref update is
retried. An empty outbox does not call GitHub.

File contents go inline in chained `create_tree` calls (100 files or about
1 MB per call) rather than one `create_blob` each, because GitHub allows
only about 500 content-creating requests an hour. The new tree is read back
and every path's blob sha is checked before the commit is made. Only a file
that is not valid UTF-8 still uses `create_blob`.

Episodes keep the transcript's `ts` spelling (`…:38.669Z`) and handoffs and
proposals keep their original `date` offset and description quoting. The
renderer picks the spelling that reproduces the row's `content_hash`, since
a `timestamptz` column stores only the instant.

## Full resync (`brain-git-export --resync`)

When git and Supabase have drifted (for example after a local reflect
wrote commits that never reached the hosted store), compare every
renderable document to `main` under the managed prefixes
(`memories/`, `summaries/`, `proposals/`, `handoffs/`, `episodes/`):

```sh
uv run brain-git-export --resync                 # dry run (default)
uv run brain-git-export --resync --apply         # one commit: add + update
uv run brain-git-export --resync --apply --delete  # also remove orphan managed paths
```

Dry run prints counts and up to 50 example paths per bucket (`add`,
`update`, `delete`, `unmanaged`). Unmanaged files (paths under a managed
prefix that `git_render` would never produce) are reported and never
deleted. Rows with `attrs.git_export` false, and episodes whose cwd matches
`BRAIN_EPISODE_EXCLUDE`, are counted under `excluded` (per-root counts only;
their paths are never printed) and are never added or updated; a git file
that already exists for one stays in `delete`. The same `job_locks` row
(`git-export`) is held so a resync and the nightly drain cannot interleave.
A successful apply stamps
`meta.last_git_resync_sha` / `meta.last_git_resync_at`.

## Migration

Apply with the session pooler (port 5432), secret `brain-database-url-ops`.
`brain-pg-migrate` runs every file in order. The new file is
`supabase/migrations/0005_git_export.sql` (`git_exports.action`,
`brain.job_locks`). It is safe to re-run. Do not edit `0001`–`0004`.

```sh
DATABASE_URL="$(gcloud secrets versions access latest --secret=brain-database-url-ops --project=PROJECT_ID)" \
  uv run brain-pg-migrate
```

## GitHub token

Fine-grained personal access token on `OWNER/brain`:

- Repository access: only `OWNER/brain`
- Permissions: **Contents: Read and write**

That covers the Git Data API (blobs, trees, commits, and the `main` ref).
Metadata read is included. No other permission is required.

A classic token needs the **`repo`** scope when the repository is private.

Store it in Secret Manager. Do not commit the value.

```sh
gcloud secrets create brain-github-export-token \
  --project=PROJECT_ID \
  --replication-policy=automatic

printf '%s' "$GITHUB_TOKEN" | gcloud secrets versions add brain-github-export-token \
  --project=PROJECT_ID \
  --data-file=-
```

## Bucket

```sh
gcloud storage buckets create gs://PROJECT_ID-brain-backups \
  --project=PROJECT_ID \
  --location=REGION \
  --uniform-bucket-level-access

cat > /tmp/brain-backup-lifecycle.json <<'EOF'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }
  ]
}
EOF

gcloud storage buckets update gs://PROJECT_ID-brain-backups \
  --lifecycle-file=/tmp/brain-backup-lifecycle.json
```

The job's runtime service account needs to create and overwrite objects.
`roles/storage.objectUser` is the minimum (creator cannot overwrite the same
day's object). The default compute service account is
`PROJECT_NUMBER-compute@developer.gserviceaccount.com`. Use the job's actual
runtime account if you set a different one.

```sh
gcloud storage buckets add-iam-policy-binding gs://PROJECT_ID-brain-backups \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/storage.objectUser
```

That account also needs `roles/secretmanager.secretAccessor` on
`brain-database-url-ops` and `brain-github-export-token`.

## Job

`DATABASE_URL` is the **session** pooler, port **5432**
(your Supavisor host, login `brain_app`), secret
`brain-database-url-ops`. `pg_dump` does not run on the transaction pooler
(port 6543). The image's `pg_dump` is PostgreSQL 17, matching the server.
A 16 client fails with `server version: 17.x; pg_dump version: 16.x`.
`brain_app` is not the table owner, so the dump passes
`--enable-row-security`. Without that flag, forced row security fails the
run with `query would be affected by row-level security policy`.

`--source .` builds `Dockerfile`, which now includes that client. This deploy
does not change the MCP service.

```sh
gcloud run jobs deploy brain-nightly \
  --project=PROJECT_ID \
  --region=REGION \
  --source=. \
  --command=brain-nightly \
  --set-secrets=DATABASE_URL=brain-database-url-ops:latest,GITHUB_TOKEN=brain-github-export-token:latest \
  --set-env-vars=BRAIN_BACKUP_BUCKET=PROJECT_ID-brain-backups,BRAIN_BACKUP_TZ=UTC,BRAIN_GIT_REPO=OWNER/brain,BRAIN_BACKEND=postgres \
  --service-account=PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --max-retries=1 \
  --task-timeout=600 \
  --memory=512Mi \
  --cpu=1 \
  --tasks=1 \
  --parallelism=1
```

## Scheduler

03:00 in your time zone (match `BRAIN_BACKUP_TZ`). The HTTP call returns when the execution is
created; the job itself keeps running.

The caller needs `run.jobs.run`. `roles/run.jobsExecutor` is that permission.

```sh
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/run.jobsExecutor

gcloud scheduler jobs create http brain-nightly \
  --project=PROJECT_ID \
  --location=REGION \
  --schedule="0 3 * * *" \
  --time-zone="UTC" \
  --uri="https://run.googleapis.com/v2/projects/PROJECT_ID/locations/REGION/jobs/brain-nightly:run" \
  --http-method=POST \
  --oauth-service-account-email=PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform
```

Run it once by hand:

```sh
gcloud run jobs execute brain-nightly --project=PROJECT_ID --region=REGION --wait
```

## Restore

The object is `pg_dump --schema=brain --no-owner --no-privileges --enable-row-security`. Load it into an empty Postgres 17 database as an admin (a superuser, or a role with `BYPASSRLS`). `$RESTORE_URL` is a database you can throw away, not the live Supabase URI, until you have read the dump and mean to replace the project.

What that dump restores:

- **Rows.** `--enable-row-security` reads through `brain_server_all` (`FOR ALL TO brain_server USING (true) WITH CHECK (true)`). `brain_app` is a member of `brain_server`, so every row is in the file.
- **Policies and row security.** `CREATE POLICY brain_server_all` is in the dump, and each table is `ENABLE` plus `FORCE ROW LEVEL SECURITY`. `brain_server` has to exist before those policies are created. It is `NOLOGIN`.
- **Ownership.** `--no-owner` does not emit `ALTER ... OWNER`. The admin who runs `psql` owns the restored objects. They are not owned by `brain_app`.
- **Grants.** `--no-privileges` omits `GRANT` and `REVOKE`. After the load, `brain_server` has the policy and no table privileges. The grant below is what puts them back. `vector` is not inside schema `brain`; create the extension first.

```sh
gcloud storage cp gs://PROJECT_ID-brain-backups/brain/YYYY-MM-DD.sql.gz /tmp/brain.sql.gz
gunzip -c /tmp/brain.sql.gz > /tmp/brain.sql

psql "$RESTORE_URL" -v ON_ERROR_STOP=1 -c 'create extension if not exists vector'
psql "$RESTORE_URL" -v ON_ERROR_STOP=1 -c 'create role brain_server nologin'
psql "$RESTORE_URL" -v ON_ERROR_STOP=1 -f /tmp/brain.sql
psql "$RESTORE_URL" -v ON_ERROR_STOP=1 <<'SQL'
grant usage on schema brain to brain_server;
grant select, insert, update, delete on all tables in schema brain to brain_server;
grant usage, select on all sequences in schema brain to brain_server;
SQL
```
