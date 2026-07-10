---
name: postgres-nightly-backup
description: How the demo acme-webapp Postgres database is backed up each night.
metadata:
  type: reference
date: 2026-01-12
---
The acme-webapp production Postgres runs a nightly logical backup at 02:00 UTC
via a cron job on the db host. It runs `pg_dump --format=custom` and uploads
the dump to the `acme-backups` object-storage bucket under
`pg/<db>/<date>.dump`, keeping 30 daily snapshots plus 12 monthly ones.
Restore into a scratch database with `pg_restore --clean --no-owner -d
<target> <file>.dump`. Verify a restore quarterly. This is fictional demo
content illustrating the `reference` (runbook/fact) memory kind.
