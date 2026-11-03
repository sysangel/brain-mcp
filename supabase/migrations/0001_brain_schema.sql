-- Supabase canonical store: documents + search live here, not in git or SQLite.
-- Apply with the Supabase direct/session connection (dashboard URI, port 5432).
-- Cloud Run uses the Supavisor transaction pooler (port 6543) at runtime.
-- Do not expose schema `brain` in the Data API. Do not serve MCP from an
-- Edge Function. Embeddings are computed by Cloud Run via the OpenAI API.
--
-- Private schema `brain`. RLS is on and forced. There are no policies for
-- anon or authenticated. The app role `brain_server` is the only role granted
-- a policy.
--
-- pgvector ships in the `extensions` schema on Supabase. On a local
-- pgvector image this file creates it in `public` (search_path at CREATE
-- EXTENSION time — do not point search_path at `brain` first).
-- Git is a one-time import and an optional export outbox, not the live
-- write path for remember.

create schema if not exists brain;

do $ext$
begin
  begin
    -- Pin to public: role name `brain` makes "$user" resolve to schema
    -- brain, so an unqualified CREATE EXTENSION would install there and
    -- pg_dump --schema=brain would emit brain.vector that restore cannot
    -- satisfy (vector is outside schema brain; see backup.py / nightly.md).
    create extension if not exists vector with schema public;
  exception
    when others then
      raise notice 'create extension vector skipped: %', sqlerrm;
  end;
end
$ext$;

set search_path = brain, public, extensions;

do $role$
begin
  if not exists (select 1 from pg_roles where rolname = 'brain_server') then
    create role brain_server nologin;
  end if;
end
$role$;

create table if not exists brain.documents (
  id                bigint generated always as identity primary key,
  stable_id         text not null unique,
  kind              text not null check (kind in ('episode', 'memory', 'summary', 'proposal', 'handoff')),
  scope             text check (scope is null or scope in ('personal', 'work')),
  project_id        text,
  relpath           text,
  title             text,
  description       text,
  body              text,
  content_hash      text not null,
  embedded_hash     text,
  embedding         vector(256),
  embedding_next    vector(256),
  embed_space       text,
  embed_space_next  text,
  embedded_hash_next text,
  fts               tsvector generated always as (
                      to_tsvector(
                        'simple',
                        coalesce(title, '') || ' ' || coalesce(description, '') || ' ' || coalesce(body, '')
                      )
                    ) stored,
  ts                timestamptz,
  day               date,
  cwd               text,
  session_id        text,
  uuid              text,
  git_branch        text,
  host              text,
  status            text not null default 'active' check (status in ('active', 'archived', 'forgotten')),
  recall_enabled    boolean not null default true,
  expires_at        timestamptz,
  verified_at       timestamptz,
  archived_at       timestamptz,
  attrs             jsonb not null default '{}'::jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create unique index if not exists documents_relpath_unique
  on brain.documents (relpath) where relpath is not null;

create index if not exists documents_fts on brain.documents using gin (fts);

create index if not exists documents_lifecycle
  on brain.documents (kind, project_id, status, recall_enabled, expires_at);

create index if not exists documents_day on brain.documents (kind, day);

create index if not exists documents_session
  on brain.documents (session_id) where session_id is not null;

create index if not exists documents_embed_space on brain.documents (embed_space);

-- HNSW is optional at today's corpus size (exact scan in recall_candidates).
-- Created when the installed pgvector provides vector_cosine_ops.
do $hnsw$
begin
  begin
    execute $sql$
      create index if not exists documents_embedding_hnsw
        on brain.documents using hnsw (embedding vector_cosine_ops)
        with (m = 16, ef_construction = 64)
    $sql$;
  exception
    when others then
      raise notice 'hnsw index skipped: %', sqlerrm;
  end;
end
$hnsw$;

create table if not exists brain.memory_id_aliases (
  old_id     text primary key,
  stable_id  text not null references brain.documents (stable_id) on update cascade on delete cascade
);

create index if not exists memory_id_aliases_stable
  on brain.memory_id_aliases (stable_id);

create table if not exists brain.meta (
  key         text primary key,
  value       text,
  updated_at  timestamptz not null default now()
);

create table if not exists brain.reflect_runs (
  id            bigint generated always as identity primary key,
  claimed_by    text not null,
  claimed_at    timestamptz not null default now(),
  completed_at  timestamptz,
  status        text not null default 'claimed'
                  check (status in ('claimed', 'completed', 'abandoned', 'expired')),
  watermark     timestamptz,
  host          text,
  detail        text
);

create index if not exists reflect_runs_open
  on brain.reflect_runs (status, claimed_at);

create table if not exists brain.sync_events (
  id           bigint generated always as identity primary key,
  ts           timestamptz not null default now(),
  kind         text not null,
  detail       text,
  resolved_at  timestamptz
);

create table if not exists brain.usage_recalls (
  id            bigint generated always as identity primary key,
  ts            timestamptz not null default now(),
  source        text not null check (source in ('mcp', 'hook', 'cli')),
  host          text,
  device        text,
  user_sub      text,
  query         text not null,
  scope         text not null,
  k             int not null,
  fts_only      boolean not null,
  n_hits        int not null,
  top_score     double precision,
  top_vec_cos   double precision
);

create table if not exists brain.usage_recall_hits (
  recall_id   bigint not null references brain.usage_recalls (id) on delete cascade,
  pos         int not null,
  kind        text,
  stable_id   text,
  score       double precision,
  primary key (recall_id, pos)
);

create table if not exists brain.usage_opens (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  source      text not null,
  host        text,
  device      text,
  user_sub    text,
  kind        text not null,
  stable_id   text not null
);

create table if not exists brain.users (
  sub               text primary key,
  email             text not null,
  domain            text,
  default_profile   text check (default_profile is null or default_profile in ('personal', 'work')),
  created_at        timestamptz not null default now()
);

create unique index if not exists users_email on brain.users (lower(email));

create table if not exists brain.clients (
  client_id    text primary key,
  user_sub     text references brain.users (sub) on delete set null,
  name         text,
  can_write    boolean not null default true,
  profiles     text[] not null default '{}',
  created_at   timestamptz not null default now(),
  revoked_at   timestamptz
);

-- Optional outbox. remember enqueues a row; nothing in this migration
-- commits git. A later exporter drains git_exports.
create table if not exists brain.git_exports (
  id            bigint generated always as identity primary key,
  document_id   bigint references brain.documents (id) on delete cascade,
  relpath       text,
  body          text,
  exported_sha  text,
  exported_at   timestamptz,
  error         text,
  created_at    timestamptz not null default now()
);

create index if not exists git_exports_pending
  on brain.git_exports (id) where exported_at is null and error is null;

create or replace function brain.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end
$$;

drop trigger if exists documents_touch_updated_at on brain.documents;
create trigger documents_touch_updated_at
  before update on brain.documents
  for each row execute function brain.touch_updated_at();

-- Eligibility predicate from the PR #1 lifecycle contract.
-- `at` is passed in so the function stays immutable (callers pass now()).
create or replace function brain.is_eligible(
  p_status text,
  p_recall_enabled boolean,
  p_expires_at timestamptz,
  at timestamptz
) returns boolean
language sql
immutable
as $$
  select p_status = 'active'
     and coalesce(p_recall_enabled, false)
     and (p_expires_at is null or p_expires_at > at);
$$;

create or replace function brain.norm_segments(s text) returns text
language sql
immutable
as $$
  select '-' || trim(both '-' from lower(
    replace(replace(coalesce(s, ''), E'\\', '-'), '/', '-')
  )) || '-';
$$;

comment on schema brain is
  'Private brain store (documents + search). Not exposed via PostgREST. No anon policies.';

-- RLS on, forced, no anon/authenticated policies. Superusers still bypass;
-- production should connect as a member of brain_server, not as a superuser
-- and not with the Supabase service key in a browser.
do $rls$
declare
  r record;
begin
  for r in
    select tablename from pg_tables where schemaname = 'brain'
  loop
    execute format('alter table brain.%I enable row level security', r.tablename);
    execute format('alter table brain.%I force row level security', r.tablename);
    execute format('drop policy if exists brain_server_all on brain.%I', r.tablename);
    execute format(
      'create policy brain_server_all on brain.%I for all to brain_server using (true) with check (true)',
      r.tablename
    );
    execute format('revoke all on table brain.%I from public', r.tablename);
    execute format(
      'grant select, insert, update, delete on table brain.%I to brain_server',
      r.tablename
    );
  end loop;
end
$rls$;

revoke all on schema brain from public;
grant usage on schema brain to brain_server;
grant usage, select on all sequences in schema brain to brain_server;

do $anon$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on schema brain from anon';
    execute 'revoke all on all tables in schema brain from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on schema brain from authenticated';
    execute 'revoke all on all tables in schema brain from authenticated';
  end if;
end
$anon$;

insert into brain.meta (key, value)
values ('schema_version', 'pg-1')
on conflict (key) do update set value = excluded.value, updated_at = now();
