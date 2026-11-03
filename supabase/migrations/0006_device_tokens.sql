-- Per-device tokens for hooks and scheduled jobs.
--
-- Hooks cannot run the browser OAuth flow, so a signed-in full-access /mcp
-- session mints a long random token per device. Only the SHA-256 of the
-- token is stored; the plaintext is shown once.
--
-- `scopes` is explicit. `ingest` lets the device push parsed session
-- episodes (POST /ingest/episodes). `hook-read` lets its hooks read the
-- project state note and the ambient memory snippets (POST /hook/*). No
-- scope reaches the MCP tools: a device token cannot recall, get_episode,
-- or remember.
--
-- `profiles` limits which episode scopes the device may write and which
-- profile its hook reads see. A token with exactly one profile stamps
-- scope-less episodes with it. `revoked_at` is the kill switch; a revoked
-- row stays for audit. `last_used_at` is touched on every accepted request.
--
-- 0005 is taken by the nightly git export branch; this file sorts after it.

set search_path = brain, public, extensions;

create table if not exists brain.device_tokens (
  id            bigint generated always as identity primary key,
  token_hash    text not null unique,
  token_prefix  text not null,
  user_sub      text not null,
  email         text not null,
  device        text not null,
  scopes        text[] not null default '{ingest,hook-read}',
  profiles      text[] not null default '{}',
  created_at    timestamptz not null default now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  constraint device_tokens_hash_shape check (token_hash ~ '^[0-9a-f]{64}$'),
  constraint device_tokens_scopes_known check (
    scopes <@ array['ingest', 'hook-read']::text[] and cardinality(scopes) > 0
  ),
  constraint device_tokens_profiles_known check (profiles <@ array['personal', 'work']::text[])
);

create index if not exists device_tokens_user
  on brain.device_tokens (user_sub, created_at);

-- Cross-session dedup for pushed episodes: a resumed or forked session
-- rewrites the same messages under a new sessionId, so the content hash is
-- the key (same rule local ingest applies across transcript files).
create index if not exists documents_episode_content_hash
  on brain.documents (content_hash) where kind = 'episode';

-- Reflect reads unreflected episodes by time.
create index if not exists documents_episode_ts
  on brain.documents (ts) where kind = 'episode';

alter table brain.device_tokens enable row level security;
alter table brain.device_tokens force row level security;
drop policy if exists brain_server_all on brain.device_tokens;
create policy brain_server_all on brain.device_tokens
  for all to brain_server using (true) with check (true);
revoke all on table brain.device_tokens from public;
grant select, insert, update, delete on table brain.device_tokens to brain_server;
grant usage, select on all sequences in schema brain to brain_server;

do $roles$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table brain.device_tokens from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table brain.device_tokens from authenticated';
  end if;
end
$roles$;
