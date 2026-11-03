# brain

**A personal memory engine and MCP server for coding agents.** Durable facts,
session digests, and recorded sessions are searchable through hybrid recall
(full-text plus vector search) from any MCP client.

[![CI](https://github.com/OWNER/brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/brain-mcp/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> Replace `OWNER` in the badge with the GitHub account that hosts the fork.

## Contents

- [Overview](#overview)
- [Choosing an install path](#choosing-an-install-path)
- [Path 1: Local](#path-1-local)
- [Path 2: Supabase store, local server](#path-2-supabase-store-local-server)
- [Path 3: Self-hosted server](#path-3-self-hosted-server)
- [Connecting clients](#connecting-clients)
- [Configuration reference](#configuration-reference)
- [Commands](#commands)
- [Memory file format](#memory-file-format)
- [Repository layout](#repository-layout)
- [Scope and limitations](#scope-and-limitations)
- [License](#license)

## Overview

An agent's context ends with the session. `brain` keeps what matters across
sessions and makes it recallable through three MCP tools on every
deployment: `recall`, `get_episode`, and `remember`.

It stores three kinds of documents:

- **Memories**: curated, durable facts (preferences, decisions, runbooks,
  per-project state notes).
- **Summaries**: per-session digests written by the consolidation pass.
- **Episodes**: recorded Claude Code transcript turns.

Each query runs a lexical leg (BM25 or Postgres full-text) and a vector leg.
The two rankings are fused with [reciprocal-rank fusion][rrf], then weighted
by kind (a curated memory outranks a raw episode at equal evidence) and by
recency. Every hit carries a `score` in `(0, 1]`.

Two storage backends share one code base:

| Backend | Source of truth | Index | Selected by |
| --- | --- | --- | --- |
| SQLite | Markdown files in a git repo | `cache/brain.db` (FTS5 + `sqlite-vec`), rebuildable | default |
| Postgres | Schema `brain` in Postgres 17 with pgvector (Supabase or any host) | Same database | `BRAIN_BACKEND=postgres`, or `DATABASE_URL` set |

[rrf]: https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf

## Choosing an install path

| | 1. Local | 2. Supabase store, local server | 3. Self-hosted server |
| --- | --- | --- | --- |
| Store | Markdown + git, SQLite index | Postgres (Supabase or any pgvector host) | Postgres |
| MCP transport | stdio | stdio | HTTP (`/mcp`, `/mcp-ro`) |
| Authentication | none (local process) | database credentials on each machine | Google OAuth for MCP, device tokens for hooks |
| Embeddings | local model (fastembed) | local model or OpenAI | OpenAI (or off) |
| Session recording | `brain-ingest` reads local transcripts | not automatic (see path notes) | `brain-push` hook per device |
| Multi-machine | optional git remote | yes, every machine needs the database URL | yes, machines need only a URL and a token |
| Cloud account needed | no | Supabase (or a Postgres host) | Postgres host, a container host, Google OAuth client, OpenAI key |

Pick path 1 to try the project or for a single machine. Pick path 3 to share
one brain across several machines or agents without handing out database
credentials. Path 2 sits between them: one shared database, no server to run.

## Path 1: Local

Everything runs on one machine. Markdown under `memories/` and `summaries/` is
the source of truth, and `cache/brain.db` is a derived index that can be
deleted and rebuilt at any time.

### Prerequisites

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Git.
- About 600 MB of disk for the embedding model, downloaded on first run.

### Steps

1. Clone and install.

   ```sh
   git clone https://github.com/OWNER/brain-mcp.git ~/src/brain-mcp
   cd ~/src/brain-mcp
   uv sync
   ```

2. Try recall against the synthetic store in `examples/`. Pointing
   `BRAIN_CLAUDE_PROJECTS` at an empty directory keeps real transcripts out.

   ```sh
   BRAIN_DIR=$PWD/examples BRAIN_CLAUDE_PROJECTS=$(mktemp -d) \
     uv run brain-ingest --full
   BRAIN_DIR=$PWD/examples uv run brain-recall "postgres backup"
   ```

3. Index real data. With `BRAIN_DIR` unset, the store is the checkout itself
   and `brain-ingest` reads Claude Code transcripts from
   `~/.claude/projects` (and `~/.claude-work/projects` when it exists).

   ```sh
   uv run brain-ingest
   ```

4. Register the stdio server with Claude Code.

   ```sh
   claude mcp add brain -- uv run --directory ~/src/brain-mcp brain-server
   ```

   For a client that must only see read tools, register
   `brain-server-readonly` instead (`recall` and `get_episode` only).

5. Optional: add the local hooks to `~/.claude/settings.json`.

   ```json
   {
     "hooks": {
       "SessionStart": [{ "hooks": [{ "type": "command", "timeout": 10,
         "command": "uv run --no-sync --directory ~/src/brain-mcp brain-session-start" }] }],
       "UserPromptSubmit": [{ "hooks": [{ "type": "command", "timeout": 5,
         "command": "uv run --no-sync --directory ~/src/brain-mcp brain-hook" }] }]
     }
   }
   ```

   `brain-session-start` injects the project's state note with a freshness
   banner. `brain-hook` runs a fast read-only search on each prompt and
   injects the strongest matches.

6. Optional: consolidation. Link `skills/reflect-local` into
   `~/.claude/skills/` and set `BRAIN_REFLECT_SKILL=/reflect-local` so the
   debt-triggered `brain-autoreflect` runs the local pass. The other skills
   in `skills/` target the hosted MCP tools (paths 2 and 3).

7. Optional: multi-machine sync. Add any private git remote as `origin`.
   Memory writes auto-commit, and `brain-sync` pulls and pushes. The index
   never leaves the machine. `BRAIN_SYNC=0` disables sync. An always-on Linux
   box can index the committed `episodes/` tree and run maintenance timers;
   see [`deploy/vm/`](deploy/vm) and [`specs/episodes-in-git.md`](specs/episodes-in-git.md).

### Environment

None is required. Common settings: `BRAIN_DIR`, `BRAIN_CLAUDE_PROJECTS`,
`BRAIN_SYNC`, `BRAIN_REFLECT_SKILL`, `FASTEMBED_CACHE_PATH`. Set
`BRAIN_BACKEND=sqlite` if `DATABASE_URL` is exported in the same shell for
another reason.

## Path 2: Supabase store, local server

Postgres holds every document and the search index. Each machine runs the
stdio server against the same database. Supabase is the documented host; any
Postgres 17 with the `vector` extension works (CI uses `pgvector/pgvector:pg17`).

### Prerequisites

- Everything from path 1.
- A Supabase project (or another Postgres 17 with pgvector).
- An OpenAI API key if OpenAI embeddings are used.

### Steps

1. Create the Supabase project. Leave schema `brain` off the Data API's
   exposed schemas. Details: [`docs/supabase.md`](docs/supabase.md), section 1.

2. Apply the migrations over the direct connection or the session pooler
   (port 5432), never the transaction pooler.

   ```sh
   export DATABASE_URL='postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres'
   uv run brain-pg-migrate
   ```

   Re-running is safe. For a least-privilege login, create `brain_app` and
   grant it `brain_server` (see `docs/supabase.md`, section 2).

3. Optional: import an existing path 1 store. Only git-tracked files are read.

   ```sh
   uv run brain-pg-load --repo /path/to/store --sqlite /path/to/store/cache/brain.db
   ```

4. Choose one embedding space and keep it. Either leave
   `BRAIN_EMBED_PROVIDER` unset (local model) or set `BRAIN_EMBED_PROVIDER=openai`
   with `OPENAI_API_KEY`, then fill vectors:

   ```sh
   uv run brain-pg-reembed --estimate
   uv run brain-pg-reembed
   uv run brain-pg-reembed --swap --require-complete
   ```

   Recall only uses vectors whose stored space matches the running process.

5. Register the server with Claude Code.

   ```sh
   claude mcp add brain \
     -e DATABASE_URL='postgresql://...' -e BRAIN_EMBED_PROVIDER=openai -e OPENAI_API_KEY=sk-... \
     -- uv run --directory ~/src/brain-mcp brain-server
   ```

   In this mode `remember` writes to Postgres and does not commit to git, and
   the server does not run `brain-ingest`.

### Environment

`DATABASE_URL` (or `SUPABASE_DB_URL`), `BRAIN_EMBED_PROVIDER`,
`OPENAI_API_KEY` for OpenAI embeddings, and optionally `BRAIN_PG_POOL_MAX`.

### Notes

New sessions are not recorded automatically in this path. `brain-push` and
the remote hooks post to the HTTP server's device routes, which only path 3
provides, and local `brain-ingest` writes only the SQLite index. Memories
written with `remember` and data imported with `brain-pg-load` are the
contents of the store.

## Path 3: Self-hosted server

One HTTP MCP server fronts the Postgres store. MCP clients sign in with
Google OAuth. Hooks and the session recorder on each device authenticate
with a per-device token, so no device holds database credentials.

Routes: `/mcp` (all tools), `/mcp-ro` (`recall` and `get_episode` only),
`/health` (open, does not touch the database), and the device routes
`/ingest/episodes`, `/hook/state`, `/hook/recall`.

### Prerequisites

- A Postgres store set up as in path 2, steps 1 and 2 (migrations through
  `0007` applied).
- A container host. Cloud Run is documented; the [`Dockerfile`](Dockerfile)
  runs on any host that serves HTTPS in front of port 8080.
- A Google OAuth client of type Web application (Google Cloud Console, APIs
  and Services, Credentials).
- An OpenAI API key. The image omits the local embedding model to keep cold
  starts fast; `BRAIN_EMBED_PROVIDER=off` runs full-text only.

### Steps

1. Decide the public origin, for example `https://brain.example.com` or the
   Cloud Run service URL. This is `BRAIN_PUBLIC_URL`.

2. In the OAuth client, add the redirect URI
   `${BRAIN_PUBLIC_URL}/auth/callback`.

3. Configure the service environment. Every variable is listed in
   [`deploy/cloudrun/env.example`](deploy/cloudrun/env.example). Store
   secrets in a secret manager, not in plain env flags. At runtime
   `DATABASE_URL` should be the Supavisor transaction pooler (port 6543).

4. Deploy. On Cloud Run:

   ```sh
   gcloud run deploy brain --source . --region REGION \
     --max-instances=1 --memory=1Gi --port=8080
   ```

   On another host, build with `docker build -t brain-mcp .` and run the
   image with the same environment. The server refuses to start without
   `BRAIN_PUBLIC_URL`, the Google client id and secret, and at least one of
   `BRAIN_AUTH_DOMAIN` or `BRAIN_AUTH_ALLOWED_EMAILS`. IAM notes for
   organizations with domain-restricted sharing are in
   [`deploy/cloudrun/README.md`](deploy/cloudrun/README.md).

5. Check health.

   ```sh
   curl -fsS "$BRAIN_PUBLIC_URL/health"
   ```

   Use `/health` for probes. Cloud Run's frontend does not route paths that
   end in `z`, so `/healthz` is not reachable from outside.

6. Optional: nightly job. `brain-nightly` dumps schema `brain` to a Cloud
   Storage bucket and drains the git export outbox into one commit on a
   GitHub repo. Setup: [`deploy/cloudrun/nightly.md`](deploy/cloudrun/nightly.md).
   Helper scripts for Cloud Run deploys and a full git resync are in
   [`ops/`](ops).

7. Connect each device as described in [Connecting clients](#connecting-clients).

### Environment

Required: `DATABASE_URL`, `BRAIN_PUBLIC_URL`, `BRAIN_GOOGLE_CLIENT_ID`,
`BRAIN_GOOGLE_CLIENT_SECRET`, and `BRAIN_AUTH_DOMAIN` and/or
`BRAIN_AUTH_ALLOWED_EMAILS`. Usually set: `BRAIN_BACKEND=postgres`,
`BRAIN_EMBED_PROVIDER=openai`, `OPENAI_API_KEY`, `BRAIN_JWT_SIGNING_KEY`.
The image sets the backend, embedding, host, and port defaults.

### Access control

- `BRAIN_AUTH_DOMAIN` admits a Google Workspace domain. Google's `hd` claim
  must match; an address that merely ends in the domain is refused.
- `BRAIN_AUTH_ALLOWED_EMAILS` admits individual accounts.
- `BRAIN_AUTH_EMAIL_PROFILES` limits an account to the `personal` or `work`
  profile, for example `you@example.com=personal|work`.
- OAuth client registrations and token mappings are stored encrypted in
  `brain.oauth_kv`. Keep `BRAIN_JWT_SIGNING_KEY` stable across deploys;
  changing it invalidates every stored sign-in.

## Connecting clients

### Claude Code, stdio (paths 1 and 2)

```sh
claude mcp add brain -- uv run --directory ~/src/brain-mcp brain-server
```

### Claude Code, HTTP (path 3)

```sh
claude mcp add --transport http brain "$BRAIN_PUBLIC_URL/mcp"
# read-only alternative
claude mcp add --transport http brain-ro "$BRAIN_PUBLIC_URL/mcp-ro"
```

Run `/mcp` in a session to complete the Google sign-in.

Other clients take the same URL. Cursor `mcp.json`:

```json
{ "mcpServers": { "brain": { "url": "https://brain.example.com/mcp" } } }
```

Codex: point the `brain` entry in `~/.codex/config.toml` at the URL, then run
`codex mcp login brain`. More detail: [`docs/connecting-agents.md`](docs/connecting-agents.md).

### Device tokens and session recording (path 3)

Hooks cannot complete a browser sign-in, so each device gets its own token.
The CLI signs in through the browser and writes the token to
`~/.config/brain/device-token`, so the token never appears in a transcript.

```sh
export BRAIN_URL="$BRAIN_PUBLIC_URL"
uv run --directory ~/src/brain-mcp brain-device-token create --device laptop
uv run --directory ~/src/brain-mcp brain-device-token list
uv run --directory ~/src/brain-mcp brain-device-token revoke <id>
```

`BRAIN_URL` must point at the server on every device; the default is
`http://localhost:8080`. The client needs no database credentials, no
embedder, and no SQLite file. Installing the console scripts with
`uv tool install --from ~/src/brain-mcp brain` puts them on `PATH`.

Hooks for `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "uv run --no-sync --directory ~/src/brain-mcp brain-remote-session-start" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "timeout": 5,
      "command": "uv run --no-sync --directory ~/src/brain-mcp brain-remote-recall-hook" }] }],
    "Stop": [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "uv run --no-sync --directory ~/src/brain-mcp brain-push --hook" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "timeout": 10,
      "command": "uv run --no-sync --directory ~/src/brain-mcp brain-push --hook" }] }]
  }
}
```

`brain-push --hook` detaches and returns immediately. A periodic catch-up
covers sessions a hook missed:

```sh
*/30 * * * * cd ~/src/brain-mcp && uv run --no-sync brain-push >/dev/null 2>&1
```

On Windows, schedule the same command with Task Scheduler. To record history
from before the hooks were installed, run `brain-push --backfill --dry-run`,
then `brain-push --backfill`.

### Skills

The [`skills/`](skills) directory holds Claude Code skills that drive the
hosted tools: `/checkpoint` (save a project's state note before `/clear`),
`/handoff`, `/catchup` (deep resume), `/reflect` (starts a headless
consolidation pass) and `/reflect-run` (the pass itself). `skills/codex/` has
the Codex variant, and `skills/reflect-local/` is the path 1 pass. Link or
copy them into `~/.claude/skills/`.

## Configuration reference

All configuration is environment variables. Defaults live in
[`src/brain/config.py`](src/brain/config.py), [`src/brain/embedconf.py`](src/brain/embedconf.py),
and [`src/brain/authz.py`](src/brain/authz.py).

### Store and backend

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_BACKEND` | auto | `sqlite` or `postgres`. Unset picks `postgres` when `DATABASE_URL` is set. |
| `DATABASE_URL` / `SUPABASE_DB_URL` | unset | Postgres connection string. |
| `BRAIN_PG_POOL_MAX` | `4` | Connection pool size per process. |
| `BRAIN_PG_POOL_TIMEOUT` | `15` | Seconds to wait for a pooled connection. |
| `BRAIN_DIR` | the checkout | Root of the markdown store and `cache/` (SQLite backend). |
| `BRAIN_CLAUDE_PROJECTS` | `~/.claude/projects` + `~/.claude-work/projects` | Transcript roots, colon-separated. A Windows drive letter is split on its colon, so leave it unset on Windows. |
| `BRAIN_INGEST_SOURCE` | `auto` | `auto`, `episodes`, or `transcripts` for `brain-ingest`. |
| `BRAIN_SCOPE` | unset | Profile (`personal` or `work`) for a local process. |
| `BRAIN_MEMORY_ALIAS_ROOTS` | unset | Extra checkout roots for legacy memory ids during `brain-pg-load`. |
| `BRAIN_SYNC` | on when a remote exists | `0` disables git sync. |

### Embeddings and ranking

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_EMBED_PROVIDER` | `local` | `local` (fastembed), `openai`, or `off`. |
| `BRAIN_EMBED_MODEL` | per provider | Model name. OpenAI default `text-embedding-3-small`. |
| `BRAIN_EMBED_DIM` | `256` | Vector dimensions. |
| `OPENAI_API_KEY` | unset | Required for `openai`. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. |
| `BRAIN_EMBED_TIMEOUT_S` / `BRAIN_EMBED_RETRIES` | `60` / `3` | OpenAI request limits. |
| `FASTEMBED_CACHE_PATH` | `cache/fastembed/` | Local model cache. |
| `BRAIN_EMBED_BATCH` / `BRAIN_EMBED_THREADS` / `BRAIN_EMBED_BACKFILL_CHUNK` | `32` / `4` / `128` | Local embedder tuning for small machines. |
| `BRAIN_VEC_FLOOR_COS` | per space | Cosine floor for the vector leg. |
| `BRAIN_RRF_K` | `60` | Reciprocal-rank-fusion constant. |
| `BRAIN_KIND_WEIGHT_{MEMORY,SUMMARY,EPISODE}` | `1.0 / 0.85 / 0.70` | Per-kind weights. |
| `BRAIN_HALF_LIFE_{MEMORY,SUMMARY,EPISODE}` | `180 / 90 / 45` days | Per-kind recency half-lives. |
| `BRAIN_RECENCY_FLOOR` | `0.35` | Lowest value the recency factor reaches. |

### HTTP server and access control (path 3)

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_PUBLIC_URL` | required | Public HTTPS origin, no trailing path. |
| `BRAIN_GOOGLE_CLIENT_ID` / `BRAIN_GOOGLE_CLIENT_SECRET` | required | Google OAuth client. |
| `BRAIN_AUTH_DOMAIN` | unset | Allowed Google Workspace domain, for example `example.com`. |
| `BRAIN_AUTH_ALLOWED_EMAILS` | unset | Comma-separated allowed accounts. |
| `BRAIN_AUTH_EMAIL_PROFILES` | unset | `email=personal|work` entries limiting profiles. |
| `BRAIN_AUTH_DEFAULT_PROFILE` | unset | Profile applied when a token names none. |
| `BRAIN_AUTH_DEFAULT_WRITE` | `1` | `0` makes new sign-ins read-only. |
| `BRAIN_JWT_SIGNING_KEY` | derived from the client secret | Signs tokens and encrypts `brain.oauth_kv`. |
| `BRAIN_HTTP_HOST` / `PORT` (or `BRAIN_HTTP_PORT`) | `0.0.0.0` / `8080` | Listen address. |

### Devices, hooks, and push

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_URL` | `http://localhost:8080` | Server base URL for `brain-push`, remote hooks, and `brain-device-token`. |
| `BRAIN_DEVICE_TOKEN` / `BRAIN_DEVICE_TOKEN_FILE` | `~/.config/brain/device-token` | Device token or its file. |
| `BRAIN_CLIENT_DIR` | `~/.config/brain` | Client state, lock, and log directory. |
| `BRAIN_HOOK_TIMEOUT_S` | `3` recall, `5` session start | Hard deadline for remote hooks. |
| `BRAIN_PUSH_DISABLE` / `BRAIN_HOOK_DISABLE` / `BRAIN_SESSION_HOOK_DISABLE` | unset | `1` turns the matching client off. |
| `BRAIN_HOOK_DEBUG` | unset | `1` prints client errors to stderr. |
| `BRAIN_PUSH_SINCE` | `all` | Default `--since` floor for `brain-push`, for example the date of a `brain-pg-load` import. |

### Privacy, consolidation, and nightly job

| Variable | Default | Purpose |
| --- | --- | --- |
| `BRAIN_DIGEST_EXCLUDE` | unset | Comma-separated cwd substrings never digested. |
| `BRAIN_EPISODE_EXCLUDE` | `BRAIN_DIGEST_EXCLUDE` | Cwd substrings whose episodes never reach git (still recallable). |
| `BRAIN_REFLECT_SKILL` | `/reflect-run` | Skill `brain-autoreflect` runs; `/reflect-local` for path 1. |
| `BRAIN_REFLECT_DEBT_SESSIONS` / `BRAIN_REFLECT_MIN_AGE_H` | see `config.py` | When a consolidation pass is due. |
| `BRAIN_REFLECT_TIMEOUT` | `1800` | Seconds before a headless pass is killed. |
| `BRAIN_NO_AUTOREFLECT` | unset | `1` disables the in-process reflect trigger. |
| `BRAIN_CLAUDE_BIN` | auto | Path to the `claude` binary. |
| `BRAIN_BACKUP_BUCKET` | required for backup | Cloud Storage bucket for `brain-backup`. |
| `BRAIN_BACKUP_TZ` | `UTC` | IANA zone that names the dump's date; match the scheduler. |
| `BRAIN_GIT_REPO` | required for export | `owner/name` of the GitHub repo `brain-git-export` commits to. |
| `BRAIN_GIT_BRANCH` | `main` | Branch for the export. |
| `GITHUB_TOKEN` | required for export | Fine-grained token with Contents read and write on that repo. |

## Commands

Every entry point is a console script, run with `uv run <name>`.

| Command | Path | Purpose |
| --- | --- | --- |
| `brain-server` | 1, 2 | stdio MCP server: `recall`, `get_episode`, `remember`. |
| `brain-server-readonly` | 1, 2 | stdio MCP server with `recall` and `get_episode` only. |
| `brain-server-http` | 3 | HTTP MCP server with OAuth and device routes. |
| `brain-ingest` | 1 | Index memories, summaries, and episodes into SQLite (`--full` rebuilds). |
| `brain-recall` / `brain-remember` | 1, 2 | Recall and write from the shell. |
| `brain-hook` / `brain-session-start` | 1 | Local prompt and session-start hooks. |
| `brain-sync` | 1 | `status`, `pull`, `push` against the git remote. |
| `brain-autoreflect` / `brain-reflect-window` | 1 | Debt-gated headless consolidation and its review window. |
| `brain-freshness-audit` / `brain-usage-report` | 1 | State-note audit and recall telemetry. |
| `brain-pg-migrate` | 2, 3 | Apply `supabase/migrations/*.sql`. |
| `brain-pg-load` | 2, 3 | Import a git markdown store into Postgres. |
| `brain-pg-reembed` | 2, 3 | Stage and swap embeddings. |
| `brain-pg-reflect` | 2, 3 | Ops CLI for the reflect lease. |
| `brain-push` | 3 | Record sessions to the server (`--hook`, `--backfill`). |
| `brain-remote-session-start` / `brain-remote-recall-hook` | 3 | Hooks that call the server. |
| `brain-device-token` | 3 | Create, list, and revoke device tokens. |
| `brain-backup` / `brain-git-export` / `brain-nightly` | 3 | Nightly dump, git export, and both in sequence. |

## Memory file format

A memory is YAML frontmatter plus a markdown body:

```markdown
---
name: postgres-nightly-backup
description: How the demo acme-webapp Postgres database is backed up each night.
metadata:
  type: reference
date: 2026-01-12
---
The `acme-webapp` database is dumped nightly at 02:00 UTC ...
```

Types are `user`, `feedback`, `project`, and `reference`. See
[`examples/`](examples) for one of each kind and the session-digest format.
The Postgres backend stores the same fields, and the git export renders rows
back into this format.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/brain/` | Server, ingest, both stores, HTTP app, device client. |
| `supabase/migrations/` | Postgres schema, applied in order by `brain-pg-migrate`. |
| `deploy/cloudrun/` | Cloud Run notes, env template, nightly job setup. |
| `deploy/vm/` | systemd user units for an always-on path 1 box. |
| `docs/` | Supabase setup in depth, connecting other agents. |
| `ops/` | Cloud Run deploy-by-digest and git resync helpers. |
| `skills/` | Claude Code and Codex skills. |
| `specs/` | Design notes for individual features. |
| `examples/` | Synthetic store for the quickstart. |

## Scope and limitations

- Built as a single-user personal system. Profiles (`personal`, `work`)
  separate one person's contexts; the server is not a hardened multi-tenant
  service.
- Google is the only OAuth provider the HTTP server supports.
- The container image has no local embedding model. A hosted server uses
  OpenAI embeddings or runs full-text only.
- CI runs the SQLite suite without the embedding model and runs the Postgres
  suite against `pgvector/pgvector:pg17`. Postgres tests skip locally unless
  `DATABASE_URL` is set.

## License

MIT. See [LICENSE](LICENSE).
