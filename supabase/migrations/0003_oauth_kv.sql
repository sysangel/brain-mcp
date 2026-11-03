-- FastMCP OAuth client registrations, upstream tokens, and JTI mappings.
-- Cloud Run instances share this table so a replacement (scale-to-zero, or
-- two revisions overlapping) still accepts tokens the previous process issued.
-- The app encrypts values before insert. Do not serve this from an Edge
-- Function, and do not add `brain` to the Data API exposed schemas.

set search_path = brain, public, extensions;

create table if not exists brain.oauth_kv (
  collection  text not null,
  key         text not null,
  value       jsonb not null,
  expires_at  timestamptz,
  primary key (collection, key)
);

alter table brain.oauth_kv enable row level security;
alter table brain.oauth_kv force row level security;
drop policy if exists brain_server_all on brain.oauth_kv;
create policy brain_server_all on brain.oauth_kv
  for all to brain_server using (true) with check (true);
revoke all on table brain.oauth_kv from public;
grant select, insert, update, delete on table brain.oauth_kv to brain_server;

do $anon$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on table brain.oauth_kv from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on table brain.oauth_kv from authenticated';
  end if;
end
$anon$;
