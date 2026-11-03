# Supabase canonical store

Supabase holds the documents and the search index. Cloud Run holds the MCP
process. Git is how an existing checkout is imported once, and how an optional
outbox can export later. It is not the live write path for `remember`.

Do not serve this MCP server from a Supabase Edge Function. Do not embed with
Supabase's built-in model. The Cloud Run service calls the OpenAI API
(`text-embedding-3-small`, 256 dimensions).

SQLite (`cache/brain.db`) stays available for local tests and offline stdio.
Production does not open it.

## 1. Create the Supabase project

1. Create a project in the Supabase dashboard. Put it in the same region you
   will deploy Cloud Run (for example `us-east-1`) so the MCP's round trip
   stays small.
2. The `vector` extension is already installed in the `extensions` schema.
   You do not enable it in the Data API.
3. Project Settings → Data API → Exposed schemas: leave `brain` **off** the
   list. The schema is private. The only client is Cloud Run, using
   `DATABASE_URL`. Do not ship the service-role key or the database password
   to laptops that only need the MCP URL.
4. Prefer the Pro plan if this database is the only copy of the corpus. Free
   projects pause after inactivity, and a pause takes recall down with it.

## 2. Apply migrations

Use the **direct** connection (host `db.<project-ref>.supabase.co`, port
**5432**) or the session pooler (port 5432). Do not use the transaction
pooler (port 6543) for migrations.

```sh
export DATABASE_URL='postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres'
uv sync
uv run brain-pg-migrate
```

That runs `supabase/migrations/*.sql` in order:

- private schema `brain`
- `documents` (kinds `episode|memory|summary|proposal|handoff`) with the
  lifecycle columns from the structured-memory contract: `project_id`,
  `status` (`active|archived|forgotten`), `recall_enabled`, `expires_at`,
  `verified_at`
- `vector(256)`, a generated `tsvector`, GIN, and an HNSW index when pgvector
  provides it
- `memory_id_aliases`, `meta`, `reflect_runs`, `sync_events`,
  `usage_recalls` / `usage_recall_hits` / `usage_opens`, `users`, `clients`,
  `git_exports` (nightly `brain-git-export` drains it; see
  [deploy/cloudrun/nightly.md](../deploy/cloudrun/nightly.md))
- `brain.recall_candidates`: eligibility, scope, project, and date filters
  run **before** the FTS and vector legs; RRF fusion; cosine floor parameter
  (OpenAI prior 0.40)

RLS is enabled and forced on every table. The only policy is
`brain_server_all`, granted to the `brain_server` role. There is no policy
for `anon` or `authenticated`. The Supabase `postgres` role is a superuser
and bypasses RLS, which is what the dashboard connection uses. For a tighter
runtime login, create a user in the SQL editor and grant it the role:

```sql
create role brain_app login password 'choose-one';
grant brain_server to brain_app;
```

Then use `brain_app` in the Cloud Run URL. The migrations themselves can keep
using `postgres`.

Re-running `brain-pg-migrate` is safe.

## 3. Load committed git markdown

Only paths `git ls-files` returns are read. Untracked episodes (the privacy
exclude that never got committed) are not imported. `proposals/` and
`handoffs/` are imported. `memories/archive/` is imported as
`status=archived`, so it is stored and not recallable.

```sh
# Same direct URI as the migration.
export DATABASE_URL='postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres'
# openai != the nomic space stamped on cache/brain.db, so vectors stay null.
BRAIN_EMBED_PROVIDER=openai uv run brain-pg-load --repo . --sqlite cache/brain.db
```

If `cache/brain.db` has `meta.embed_space` equal to the process embed space
(or, when that key is absent, the legacy
`local:nomic-ai/nomic-embed-text-v1.5:256` space), matching `content_hash`
rows copy the float32 vectors and set `embedded_hash`. Any other space leaves
`embedding` null. Pass `--accept-space` to name the space you are willing to
copy. A second load is an upsert on `stable_id`. A metadata-only change keeps
an existing vector when `content_hash` is unchanged.

Memory ids in Supabase are repo-relative (`brain-mem-v2`). The absolute-path
v1 id for the file's path at import time is written to `memory_id_aliases`.
Extra checkout roots go in `BRAIN_MEMORY_ALIAS_ROOTS`, separated by semicolons
or newlines (`C:\review\brain;D:\other\brain`). A colon-separated POSIX list
still works. Windows roots keep their original spelling, including backslashes,
so the legacy id matches the machine that minted it. `get_episode` resolves
aliases.

## 4. Re-embed with OpenAI

`brain-pg-reembed` calls `text-embedding-3-small` with `dimensions=256`. It
writes `embedding_next` and does not change the live `embedding` column until
`--swap`, so a crash does not mix two spaces in one recall. Rows already
staged for the current `content_hash` are skipped. Inactive rows (archived,
forgotten, `recall_enabled=false`, expired) are not embedded.

```sh
export OPENAI_API_KEY=sk-...
uv run brain-pg-reembed --estimate
uv run brain-pg-reembed                 # resumable
uv run brain-pg-reembed --swap --require-complete
```

`--estimate` prices the pending rows at $0.02 / 1M input tokens using
`len/4` as the token count. It does not call the API. `--require-complete`
refuses the swap if any eligible row is unstaged.

After the swap, Cloud Run must run with:

```
BRAIN_EMBED_PROVIDER=openai
BRAIN_EMBED_MODEL=text-embedding-3-small
BRAIN_EMBED_DIM=256
```

Recall only uses a vector whose `embed_space` equals that identity. Until the
swap, recall is FTS-only if no vectors were copied.

The cosine floor for this space defaults to **0.40** (`BRAIN_VEC_FLOOR_COS`
overrides it). Duplicate detection inside `remember` still uses the historical
0.90 / 0.80 thresholds; retune those on real OpenAI vectors before trusting
the gray band.

## 5. Deploy the MCP to Cloud Run

The image (`Dockerfile`) installs FastMCP, psycopg, and pgvector. It does not
install the local ONNX embedder.

Build from the repo root (the Docker context excludes the corpus):

```sh
gcloud run deploy brain \
  --source . \
  --region REGION \
  --max-instances=1 \
  --memory=1Gi \
  --port=8080
```

In an org with domain-restricted sharing, `--allow-unauthenticated` fails
with `not a permitted customer` (it would grant `allUsers`). After the service
exists, disable the invoker IAM check instead:

```sh
gcloud run services update brain --region REGION --no-invoker-iam-check
```

Cloud Run then accepts callers that have no invoker identity. Google OAuth
inside the app still protects `/mcp` and `/mcp-ro`. The server will not boot
unless `BRAIN_AUTH_DOMAIN` or `BRAIN_AUTH_ALLOWED_EMAILS` is set.

The pooler URI contains characters that are awkward in `--set-env-vars`. Bind
env from Secret Manager. Names only, no values in this repo:

- `brain-database-url` — runtime transaction pooler (port 6543)
- `brain-database-url-ops` — session pooler (port 5432) for migrate, load, and re-embed
- `openai-api-key`
- `brain-jwt-signing-key`
- `brain-google-client-secret`
- `brain-app-db-password` — `brain_app` login, ops only

Startup and uptime probes should `GET /health`. See
[deploy/cloudrun/README.md](../deploy/cloudrun/README.md).

### Supavisor transaction mode

Cloud Run's `DATABASE_URL` is the **transaction** pooler, port **6543**.
Copy the pooler host from the dashboard's Connect panel. The `aws-0` /
`aws-1` prefix differs between projects, and the wrong one returns
`tenant not found`. Migrate, load, and re-embed use
the **session** pooler on the same host, port **5432** (`brain-database-url-ops`).

```
postgresql://postgres.<project-ref>:<password>@aws-0-SUPABASE_REGION.pooler.supabase.com:6543/postgres
postgresql://postgres.<project-ref>:<password>@aws-0-SUPABASE_REGION.pooler.supabase.com:5432/postgres
```

The app opens psycopg with `prepare_threshold=None` (no prepared statements)
and sets `search_path` inside each transaction. Locks are
`pg_advisory_xact_lock`, released at commit. Do not add a session-level
`pg_advisory_lock`: Supavisor transaction mode hands the server connection
back between transactions, and a session lock would stick to the wrong client.

`BRAIN_PG_POOL_MAX` defaults to 4. Keep
`max Cloud Run instances × pool max` under the pooler client limit. Start
with `--max-instances=1`.

### OAuth state

FastMCP's Google OAuth proxy stores dynamic client registrations, upstream
tokens, and the JTI mapping in `brain.oauth_kv`. Values are Fernet-encrypted
with a key derived from `BRAIN_JWT_SIGNING_KEY` (or the Google client secret
when that is unset). A replacement Cloud Run instance, including after
scale-to-zero, reads the same rows and keeps accepting tokens. Two revisions
overlapping during a deploy share them too. Do not point the service at a
local disk for this; the container filesystem does not survive replacement.

`--max-instances=1` is still the right starting point because each instance
opens its own pool (`BRAIN_PG_POOL_MAX`, default 4) against Supavisor. Scale
to zero (`min-instances=0`) is fine: the image does not load a local embedding
model. `remember` takes a transaction advisory lock in Postgres.

When `BRAIN_AUTH_DOMAIN` is set, the Google `hd` claim must equal that domain.
An email that merely ends in `@that.domain` is not enough: Google documents
that a missing `hd` means the account is outside a hosted domain.

`/health` is unauthenticated and does not touch Supabase. Use it for probes.
`/healthz` is the same handler, but Google's frontend 404s paths that end in
`z` on `run.app` URLs, so an outside check of `/healthz` never reaches the
container. `/mcp` and `/mcp-ro` require a bearer token.

### Google OAuth redirect

In Google Cloud Console → APIs & Services → Credentials → the OAuth client
(Web application):

```
Authorized redirect URI: https://<cloud-run-host>/auth/callback
```

That host is `BRAIN_PUBLIC_URL`. Workspace restriction is `hd=<BRAIN_AUTH_DOMAIN>`
plus a check of the identity Google returns. Custom `brain:*` scopes stay on
the FastMCP token and are not sent to Google.

## 6. Client

```sh
claude mcp add --transport http brain "$BRAIN_PUBLIC_URL/mcp"
```

Read-only gate (recall and get_episode only, by construction):

```sh
claude mcp add --transport http brain-ro "$BRAIN_PUBLIC_URL/mcp-ro"
```

The first connection runs the OAuth flow (dynamic client registration against
this server, then Google). A token with `brain:read` and not `brain:write`
cannot `remember`. `brain:personal` / `brain:work` select the profile filter
that used to be `$BRAIN_SCOPE`.

Stdio against the same database, for a laptop that has the direct or pooler
URI and does not need OAuth:

```sh
DATABASE_URL='postgresql://...6543/postgres' BRAIN_EMBED_PROVIDER=openai \
  uv run brain-server
```

That process does not ingest SQLite and does not git-commit on `remember`.

## 7. Sessions, device tokens, and hooks

New Claude Code sessions reach Supabase through `brain-push`. It parses
transcripts with the same parser `brain-ingest` used and posts batches to
`POST /ingest/episodes`. Cloud Run embeds them (`text-embedding-3-small` at
256) and upserts by the stable `ep_` id. A re-sent episode comes back
`unchanged` and costs nothing. A session that is still growing updates its
row in place and is re-embedded. A resumed or forked copy of an episode
already stored is skipped as `duplicate`. A push never touches lifecycle
columns (`status`, `recall_enabled`, `expires_at`) and never deletes.

Hooks cannot do browser OAuth, so each device gets its own token:

| Scope | Route | Used by |
| --- | --- | --- |
| `ingest` | `POST /ingest/episodes` | `brain-push` |
| `hook-read` | `POST /hook/state`, `POST /hook/recall` | `brain-remote-session-start`, `brain-remote-recall-hook` |

A token is `brn_dev_` plus 32 random bytes. The server keeps only its
SHA-256 in `brain.device_tokens` (migration `0006_device_tokens.sql`), with
`last_used_at` and `revoked_at`. Every request re-checks the account against
the live `BRAIN_AUTH_*` settings, so removing an email or narrowing its
profiles narrows its tokens too. A device token never reaches the MCP tools:
it cannot `recall`, `get_episode`, or `remember`.

Mint one per device. The CLI signs in through the browser against `/mcp`
and writes the token straight to `~/.config/brain/device-token`, so it never
lands in a chat transcript:

```sh
uv run --directory ~/src/brain-mcp-mcp brain-device-token create --device work-laptop
uv run --directory ~/src/brain-mcp-mcp brain-device-token list
uv run --directory ~/src/brain-mcp-mcp brain-device-token revoke 3
```

The same operations exist as MCP tools on `/mcp` (`device_token_create`,
`device_token_list`, `device_token_revoke`), but a token minted that way
sits in that session's transcript. `BRAIN_DEVICE_TOKEN` (env) or
`BRAIN_DEVICE_TOKEN_FILE` override the file. `BRAIN_URL` overrides the
server URL; set it to your `BRAIN_PUBLIC_URL` (the default,
`http://localhost:8080`, only fits a local test server).

The client needs no database credentials, no embedder, and no SQLite file.
Run it from a brain checkout (`uv run --no-sync --directory <checkout> ...`)
or install the console scripts with `uv tool install --from <checkout> brain`,
which puts `brain-push` and the hook commands on PATH.

### Claude Code hooks

Example `~/.claude/settings.json` hooks block. Adjust the checkout path; on
macOS or Linux use `~/src/brain-mcp`. `Stop` records after every turn and
`SessionEnd` catches the last one. Both detach and return at once.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "timeout": 10,
        "command": "uv run --no-sync --directory C:/Users/you/src/brain-mcp-mcp brain-remote-session-start" } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "timeout": 5,
        "command": "uv run --no-sync --directory C:/Users/you/src/brain-mcp-mcp brain-remote-recall-hook" } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "timeout": 10,
        "command": "uv run --no-sync --directory C:/Users/you/src/brain-mcp-mcp brain-push --hook" } ] }
    ],
    "SessionEnd": [
      { "hooks": [ { "type": "command", "timeout": 10,
        "command": "uv run --no-sync --directory C:/Users/you/src/brain-mcp-mcp brain-push --hook" } ] }
    ]
  }
}
```

`brain-push --hook` reads the hook JSON, starts a detached
`python -m brain.push --file <transcript>` (on Windows it also leaves the
hook's job object), and exits 0. The two read hooks make one request each
under a hard wall-clock deadline (`BRAIN_HOOK_TIMEOUT_S`, default 3 seconds
for recall and 5 for session start, DNS and connect included) and print
nothing on any failure. Kill switches
are `BRAIN_PUSH_DISABLE=1`, `BRAIN_HOOK_DISABLE=1`, and
`BRAIN_SESSION_HOOK_DISABLE=1`. `BRAIN_HOOK_DEBUG=1` prints errors to stderr.

The SessionStart brief keeps the local format: the state note, its
`written` stamp, the episodes recorded under the repo since, and a STALE
banner when the repo has newer commits or ten or more episodes landed. When
at least `BRAIN_REFLECT_DEBT_SESSIONS` sessions have piled up since the last
reflect and that pass is `BRAIN_REFLECT_MIN_AGE_H` hours old, the brief adds
a line asking for /reflect. That line is the hosted auto-trigger. The server
cannot launch Claude, so the session that sees the line starts the pass, and
the reflect lease keeps two machines from running it at once.

### Catch-up and backfill

A hook can miss a session (machine asleep, offline, hooks not installed
yet), so also run a periodic catch-up. It pushes every transcript that
changed since its last successful run.

Windows Task Scheduler, every 30 minutes, no console window:

```powershell
schtasks /Create /TN "brain-push" /SC MINUTE /MO 30 /F /TR "powershell -NoProfile -WindowStyle Hidden -Command uv run --no-sync --directory C:\Users\you\src\brain brain-push"
```

cron:

```sh
*/30 * * * * cd ~/src/brain-mcp-mcp && uv run --no-sync brain-push >/dev/null 2>&1
```

Once per device, after the hooks are in, record everything since the
snapshot import (or since the first session, for a fresh install):

```sh
uv run --directory ~/src/brain-mcp-mcp brain-push --backfill --dry-run   # counts only
uv run --directory ~/src/brain-mcp-mcp brain-push --backfill
```

`--since` moves the floor (`--since 2026-09-01`, or `--since all`).
Episodes the snapshot already holds come back `unchanged`, so overlap is
free.

Client state lives in `~/.config/brain/` (`BRAIN_CLIENT_DIR`).
`push-state.json` records each transcript's size and mtime and the content
hash of every episode the server acknowledged, and is replaced atomically.
Episodes the server refused (for example a work-only token sending a
personal session) are kept in a separate list: catch-up skips them and
`--backfill` sends them again, so mint the right token and backfill.
`push.lock` keeps two runs from overlapping on Windows and POSIX. A run that
finds it held appends its transcripts (or a full catch-up request) to
`push.pending`, and the holder pushes them before it lets go. `push.log`
holds counts, never episode text.

### Privacy

Sources match local ingest: every configured Claude Code projects root
(`~/.claude/projects`, `~/.claude-work/projects`, or
`BRAIN_CLAUDE_PROJECTS`), top-level sessions plus subagent sidechains, with
the `personal` or `work` scope taken from the root. A token limited to one
profile cannot write the other, and it stamps scope-less episodes with its
own. Every episode must carry its session id and uuid, and the server
re-derives the stable id from them, so a token cannot aim its text at
another session's row. An update never changes a stored row's scope and is
refused when that scope is outside the token's profiles. An episode whose cwd matches `BRAIN_EPISODE_EXCLUDE` (client or
server) is stored and recallable but never queued for the git export, the
same gate the local `episodes/` tree had. Anything shaped like a device
token is scrubbed from episode text before it is stored. The routes log the
device name and counts only.

## 8. Cutover from the local brain

Every client-side change, once the migration is applied and the service is
deployed:

1. **MCP.** Replace any local stdio `brain` entry (one that runs
   `brain-server`) with the hosted server.
   - Claude Code: `claude mcp remove brain`, then
     `claude mcp add --transport http brain https://brain-PROJECT_NUMBER.REGION.run.app/mcp`,
     then `/mcp` in a session to sign in.
   - Codex: point the `brain` entry in `~/.codex/config.toml` at the same
     URL, then `codex mcp login brain`.
   - pi: point its `brain` MCP entry at the same URL, then `/mcp-auth brain`.
   - Cursor: see [connecting-agents.md](connecting-agents.md).
2. **Device token.** On each machine run
   `brain-device-token create --device <name>` (section 7).
3. **Hooks.** Remove the old `brain-hook`, `brain-session-start`, and
   `brain-session-refresh` hook entries and add the four entries from
   section 7. The old hooks read `cache/brain.db` and go silent without it.
4. **Backfill.** Run `brain-push --backfill` once per machine.
5. **Catch-up job.** Add the Task Scheduler or cron entry from section 7.
   Remove any old `brain-autoreflect` or `brain-sync` schedule.
6. **Skills.** `/checkpoint`, `/handoff`, `/catchup`, `/reflect`,
   `/reflect-run`, and the Codex `reflect` skill in `skills/` now use the
   hosted MCP tools only. Reinstall or re-link them wherever the old copies
   live.
7. **Retire the local clone's runtime.** Stop running `brain-ingest`,
   `brain-sync`, `brain-autoreflect`, and `brain-reflect-window`, and delete
   `cache/` (`brain.db`, locks, logs). Keep a checkout only as the place
   `uv run` launches the client from, or install the console scripts and
   delete the clone. Do not edit `memories/` and the other markdown trees;
   they are the import snapshot and the nightly export target.

## Reflect lease

`reflect_claim` (MCP) and `brain-pg-reflect claim` (ops CLI) insert a
`reflect_runs` row. The MCP claim is for the signed-in profile: a work claim
does not block a personal one. A neutral claim (no profile, which is what
the ops CLI takes) still excludes every profile, and any open claim blocks a
neutral one. `reflect_complete` stamps that claim's watermark
(`meta.last_reflect` for a neutral pass, `last_reflect:personal` or
`last_reflect:work` for a profile pass) with the claim time, so episodes
that arrive during a pass are reviewed by the next one, and releases the
lease. A profile pass does not advance the other profile's watermark.
Readers that have no profile-specific key still use `last_reflect`. A claim
older than `BRAIN_REFLECT_CLAIM_TTL_S` (default 45 minutes) is expired on
the next claim. Session digests take the scope ingest stored on that
session's episodes; an unscoped session stays a shared digest, and older
digests with a null scope stay readable. The hosted pass reads its window
with `reflect_window`,
`reflect_sessions`, and `session_episodes`, and writes with `summary_write`,
`state_write`, `memory_update`, `memory_set_status`, `remember`, and
`proposal_write`. The old `brain-reflect-window` command still drives the
sqlite/git checkout and is not the production lease.

## Nightly backup and git export

`brain-nightly` dumps schema `brain` to
`gs://$BRAIN_BACKUP_BUCKET/brain/YYYY-MM-DD.sql.gz` and then drains
`git_exports` into one commit on `main`. The hosted database is Postgres 17.
The job image's `pg_dump` is the 17 client; a 16 client refuses the server.
Commands, the bucket lifecycle, and
the 03:00 schedule are in
[deploy/cloudrun/nightly.md](../deploy/cloudrun/nightly.md).

`meta.ingested_git_head` remains the commit `brain-pg-load` imported. The
export records `git_exports.exported_sha` and `meta.last_git_export_sha`.
The job also opens the database every night, so a free Supabase project does
not auto-pause after a week idle. Free projects still have no provider-side
backup; the dump is that copy.

`brain-git-export --resync` compares every renderable document to `main`
(dry run by default; `--apply` / `--apply --delete` write one commit). It
holds the same `git-export` lock and stamps `meta.last_git_resync_sha` /
`meta.last_git_resync_at`. See
[deploy/cloudrun/nightly.md](../deploy/cloudrun/nightly.md).

Migration `0005_git_export.sql` adds `git_exports.action` (`upsert` or
`delete`) and `brain.job_locks`. Apply it with the session pooler, the same
way as the earlier files.

## What was deliberately left on SQLite

Local `brain-ingest`, `brain-hook`, `brain-session-start`, and `brain-recall`
against `cache/brain.db` are unchanged so the existing tests stay on the
sqlite backend. Set `BRAIN_BACKEND=sqlite` to force that even when
`DATABASE_URL` is exported. The hosted replacements are `brain-push`,
`brain-remote-recall-hook`, and `brain-remote-session-start`.
