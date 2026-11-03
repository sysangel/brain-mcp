# Connecting another agent to the hosted brain

Supabase Postgres is the source of truth (your project, schema `brain`).
Cloud Run, or any container host, is only the MCP process in front of it.
Below, `https://brain-PROJECT_NUMBER.REGION.run.app` stands for your
`BRAIN_PUBLIC_URL`.

The markdown under `memories/`, `episodes/`, `summaries/`, `proposals/`, and
`handoffs/` in a store repo is whatever `brain-pg-load` imported, plus the
nightly export.
Editing those files does not update the hosted brain. Write a memory with the
MCP `remember` tool. Recall with `recall` and `get_episode`.

`remember` queues `brain.git_exports`. The nightly job commits that queue to
`main`. Those commits are a mirror of the database, not the other direction:
a file edited on `main` still does not change `recall` until `brain-pg-load`.

## MCP

Google sign-in is limited to the accounts allowed by `BRAIN_AUTH_DOMAIN` and
`BRAIN_AUTH_ALLOWED_EMAILS`.

Full tools (`recall`, `get_episode`, `remember`):

```
https://brain-PROJECT_NUMBER.REGION.run.app/mcp
```

Read-only (`recall`, `get_episode`):

```
https://brain-PROJECT_NUMBER.REGION.run.app/mcp-ro
```

Claude Code:

```sh
claude mcp add --transport http brain https://brain-PROJECT_NUMBER.REGION.run.app/mcp
```

Cursor `mcp.json` (replace a local stdio `brain` entry that runs `brain-server`):

```json
{
  "mcpServers": {
    "brain": {
      "url": "https://brain-PROJECT_NUMBER.REGION.run.app/mcp"
    }
  }
}
```

The first connection runs Google sign-in. A read-only client uses the
`/mcp-ro` URL instead.

Codex: point the `brain` entry in `~/.codex/config.toml` at the `/mcp` URL,
then run `codex mcp login brain`. pi: same URL, then `/mcp-auth brain`.

The full `/mcp` server also carries the tools the skills use (`state_get`,
`state_write`, `summary_write`, `handoff_write`, `handoff_latest`,
`proposal_write`, `memory_update`, `memory_set_status`, `reflect_claim`,
`reflect_window`, `reflect_sessions`, `session_episodes`, `reflect_complete`)
and device token management (`device_token_create`, `device_token_list`,
`device_token_revoke`, `device_token_revoke_hash`). Session recording and the SessionStart and prompt
hooks use a per-device token instead of OAuth; see
[supabase.md, section 7](supabase.md#7-sessions-device-tokens-and-hooks).

## What not to use

Direct database access is for ops: migrations, `brain-pg-load`,
`brain-pg-reembed`, and debugging. That login is `brain_app`. The connection
strings and password are in Secret Manager under `brain-database-url`,
`brain-database-url-ops`, and `brain-app-db-password`. See
[deploy/cloudrun/README.md](../deploy/cloudrun/README.md).

RLS is forced on schema `brain`. The schema is not in the Supabase Data API
exposed schemas, so the anon key, the service role, REST, and `supabase-js`
cannot read or write it. That is intentional.

Embeddings are OpenAI `text-embedding-3-small` at 256 dimensions
(`openai:text-embedding-3-small:256`). Do not point a client or a one-off
script at another model or another dimension. Recall ignores vectors whose
`embed_space` does not match.
