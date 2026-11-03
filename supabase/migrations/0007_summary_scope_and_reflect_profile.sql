-- Digests participate in profile scope, and a reflect lease records the
-- profile that claimed it.
--
-- filtered_documents used to skip scope for every kind except episode and
-- memory, so a work digest was visible to a personal session. Summaries now
-- follow the same rule as episodes: a null scope stays shared (rows written
-- before this migration, and digests of unscoped sessions), and a stamped
-- scope is visible only to that profile. Proposals and handoffs stay shared.
--
-- reflect_runs.profile is null for a neutral claim (it still excludes every
-- profile) and personal or work for a profile claim. One profile's claim
-- does not block the other. 0001-0006 are already applied; this file only
-- adds the column and replaces the function.
--
-- Idempotent. RLS on reflect_runs stays enabled and forced, with the same
-- brain_server policy and grants as 0006.

set search_path = brain, public, extensions;

alter table brain.reflect_runs
  add column if not exists profile text;

do $profile$
begin
  if not exists (
    select 1
      from pg_constraint
     where conname = 'reflect_runs_profile_known'
       and conrelid = 'brain.reflect_runs'::regclass
  ) then
    alter table brain.reflect_runs
      add constraint reflect_runs_profile_known
      check (profile is null or profile in ('personal', 'work'));
  end if;
end
$profile$;

alter table brain.reflect_runs enable row level security;
alter table brain.reflect_runs force row level security;
drop policy if exists brain_server_all on brain.reflect_runs;
create policy brain_server_all on brain.reflect_runs
  for all to brain_server using (true) with check (true);
revoke all on table brain.reflect_runs from public;
grant select, insert, update, delete on table brain.reflect_runs to brain_server;
grant usage, select on all sequences in schema brain to brain_server;

do $roles$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table brain.reflect_runs from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table brain.reflect_runs from authenticated';
  end if;
end
$roles$;

create or replace function brain.filtered_documents(
  kinds text[],
  p_project text,
  p_since date,
  p_until date,
  p_scope text,
  at timestamptz
) returns setof brain.documents
language sql
stable
set search_path = brain, public, extensions
as $$
  select d.*
  from brain.documents d
  where d.kind = any (kinds)
    and brain.is_eligible(d.status, d.recall_enabled, d.expires_at, at)
    and (
      p_scope is null
      or d.kind not in ('episode', 'memory', 'summary')
      or d.scope is null
      or d.scope = p_scope
    )
    and (
      p_project is null
      or lower(d.project_id) = lower(btrim(p_project))
      or (
        d.project_id is null
        and position(
          brain.norm_segments(p_project)
          in brain.norm_segments(coalesce(d.cwd, '') || '/' || coalesce(d.relpath, ''))
        ) > 0
      )
    )
    and (p_since is null or d.day >= p_since)
    and (p_until is null or d.day <= p_until);
$$;

revoke all on function brain.filtered_documents(text[], text, date, date, text, timestamptz) from public;
grant execute on function brain.filtered_documents(text[], text, date, date, text, timestamptz) to brain_server;

do $anon$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on function brain.filtered_documents(text[], text, date, date, text, timestamptz) from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on function brain.filtered_documents(text[], text, date, date, text, timestamptz) from authenticated';
  end if;
end
$anon$;
