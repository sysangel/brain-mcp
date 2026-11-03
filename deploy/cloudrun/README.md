# Cloud Run

The service is the MCP process. Supabase holds the data. Embeddings are
OpenAI `text-embedding-3-small` at 256 dimensions, called from this service.

Env names and placeholders: `env.example`. Secret Manager holds the values.
These are the secret names only:

| Secret | Use |
| --- | --- |
| `brain-database-url` | Runtime `DATABASE_URL`. Supavisor **transaction** pooler, port **6543**. |
| `brain-database-url-ops` | Loads, migrations, and `brain-pg-reembed`. Supavisor **session** pooler, port **5432**. |
| `openai-api-key` | `OPENAI_API_KEY` |
| `brain-jwt-signing-key` | `BRAIN_JWT_SIGNING_KEY` |
| `brain-google-client-secret` | `BRAIN_GOOGLE_CLIENT_SECRET` |
| `brain-app-db-password` | Password for the `brain_app` login. Ops only. |

## Pooler host

Copy the pooler host from the dashboard's Connect panel. The `aws-0` /
`aws-1` prefix differs between projects, and the wrong one returns
`tenant not found`.

Runtime (Cloud Run):

```
postgresql://postgres.<project-ref>:<password>@aws-0-SUPABASE_REGION.pooler.supabase.com:6543/postgres
```

Ops (migrate, load, re-embed):

```
postgresql://postgres.<project-ref>:<password>@aws-0-SUPABASE_REGION.pooler.supabase.com:5432/postgres
```

The app sets `prepare_threshold=None` and uses transaction-scoped advisory
locks, which is what port 6543 requires. Port 5432 is the session pooler.

## IAM

In an org with domain-restricted sharing, `gcloud run deploy --allow-unauthenticated`
fails with `not a permitted customer` because it tries to grant `allUsers`.
Deploy the service without that flag, then turn off the invoker IAM check:

```sh
gcloud run services update brain --region REGION --no-invoker-iam-check
```

Cloud Run then accepts requests that have no Google invoker identity. Google
OAuth inside the app still protects `/mcp` and `/mcp-ro`. `/health` stays open.
`/ingest/episodes`, `/hook/state`, and `/hook/recall` are open at the
ingress too; each handler checks a device token from `brain.device_tokens`
(see docs/supabase.md, section 7) and refuses everything else with 401.

## Probe

Use `GET /health`. Google's frontend 404s paths that end in `z` on `run.app`
URLs, so `/healthz` is unreachable from outside Cloud Run even though the
process still serves it as an alias. The handler does not touch Supabase.

## Nightly job

Backup and git export run as a Cloud Run Job, not in this service. The job
uses secret `brain-database-url-ops` (session pooler, port 5432) and
`brain-github-export-token`. Do not add the GitHub token to the MCP service.
Setup, including the bucket and the 03:00 scheduler,
is in [nightly.md](nightly.md).
