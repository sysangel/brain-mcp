-- Nightly git export. remember already inserts git_exports rows. This adds
-- the action the drain understands, and a lock table the job can hold across
-- GitHub calls. Session advisory locks are unsafe on the transaction pooler
-- (port 6543); a row with an expiry works on either pooler.
--
-- action = 'upsert' writes the document's markdown. action = 'delete' removes
-- that path from git. status = 'forgotten' is also a delete at drain time.
-- Deleting the documents row cascades the outbox row, so a forget that should
-- remove the file must queue the outbox row before the delete, or set status
-- to forgotten and leave the row in place.

set search_path = brain, public, extensions;

alter table brain.git_exports
  add column if not exists action text not null default 'upsert';

alter table brain.git_exports
  drop constraint if exists git_exports_action_check;

alter table brain.git_exports
  add constraint git_exports_action_check
  check (action in ('upsert', 'delete'));

create table if not exists brain.job_locks (
  name        text primary key,
  owner       text not null,
  expires_at  timestamptz not null,
  updated_at  timestamptz not null default now()
);

alter table brain.job_locks enable row level security;
alter table brain.job_locks force row level security;
drop policy if exists brain_server_all on brain.job_locks;
create policy brain_server_all on brain.job_locks
  for all to brain_server using (true) with check (true);
revoke all on table brain.job_locks from public;
grant select, insert, update, delete on table brain.job_locks to brain_server;

do $roles$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table brain.job_locks from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table brain.job_locks from authenticated';
  end if;
end
$roles$;
