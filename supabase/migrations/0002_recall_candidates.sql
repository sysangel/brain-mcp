-- Hybrid recall: eligibility + scope + project + date, then FTS and vector,
-- then RRF. Kind/recency weights stay in Python (ranking.py).
--
-- The vector leg is an exact distance scan over the filtered pool (correct
-- at today's size, and it preserves filter-then-fuse). The HNSW index is
-- available for a later rewrite once the corpus outgrows an exact scan.
-- Cosine floor defaults to the OpenAI text-embedding-3-small prior (~0.40);
-- callers pass the configured floor. An emptied vector leg does not count
-- in n_lists.

set search_path = brain, public, extensions;

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
      or d.kind not in ('episode', 'memory')
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

drop function if exists brain.recall_candidates(
  text[], vector, text[], text, date, date, text, text, int, int, double precision, int, timestamptz
);

create or replace function brain.recall_candidates(
  q_tokens text[],
  q_vec vector(256),
  kinds text[] default array['episode', 'memory', 'summary', 'proposal', 'handoff'],
  p_project text default null,
  p_since date default null,
  p_until date default null,
  p_scope text default null,
  p_embed_space text default null,
  fts_limit int default 40,
  vec_limit int default 40,
  vec_floor double precision default 0.40,
  rrf_k int default 60,
  at timestamptz default now()
) returns table (
  kind text,
  id bigint,
  stable_id text,
  rrf double precision,
  n_lists int,
  ts timestamptz,
  best_cos double precision,
  title text,
  description text,
  body text,
  project_id text,
  cwd text,
  relpath text,
  session_id text,
  scope text,
  status text,
  recall_enabled boolean,
  expires_at timestamptz,
  verified_at timestamptz,
  attrs jsonb
)
language sql
stable
set search_path = brain, public, extensions
as $$
  with q as (
    select case
      when q_tokens is null or cardinality(q_tokens) = 0 then null::tsquery
      else to_tsquery('simple', (
        select string_agg(quote_literal(t), ' | ')
        from unnest(q_tokens) as t
        where t is not null and t <> ''
      ))
    end as tsq
  ),
  cand as (
    select *
    from brain.filtered_documents(kinds, p_project, p_since, p_until, p_scope, at)
  ),
  fts as (
    select * from (
      select c.kind, c.id,
             row_number() over (
               partition by c.kind
               order by ts_rank_cd(c.fts, q.tsq, 1) desc, c.id
             ) as r
      from cand c
      cross join q
      where q.tsq is not null and c.fts @@ q.tsq
    ) ranked
    where r <= fts_limit
  ),
  vec as (
    select * from (
      select c.kind, c.id,
             (1 - (c.embedding <=> q_vec)) as cos,
             row_number() over (
               partition by c.kind
               order by c.embedding <=> q_vec, c.id
             ) as r
      from cand c
      where q_vec is not null
        and c.embedding is not null
        and p_embed_space is not null
        and c.embed_space = p_embed_space
    ) ranked
    where r <= vec_limit
  ),
  vec_kept as (
    select * from vec where cos >= vec_floor
  ),
  lists as (
    select c.kind,
           (exists (select 1 from fts f where f.kind = c.kind))::int
             + (exists (select 1 from vec_kept v where v.kind = c.kind))::int
             as n_lists
    from cand c
    group by c.kind
  ),
  fused as (
    select u.kind, u.id, sum(1.0 / (rrf_k + u.r)) as rrf
    from (
      select kind, id, r from fts
      union all
      select kind, id, r from vec_kept
    ) u
    group by u.kind, u.id
  )
  select f.kind,
         f.id,
         c.stable_id,
         f.rrf,
         greatest(l.n_lists, 1),
         c.ts,
         (select max(v.cos) from vec v),
         c.title,
         c.description,
         c.body,
         c.project_id,
         c.cwd,
         c.relpath,
         c.session_id,
         c.scope,
         c.status,
         c.recall_enabled,
         c.expires_at,
         c.verified_at,
         c.attrs
  from fused f
  join cand c on c.kind = f.kind and c.id = f.id
  join lists l on l.kind = f.kind;
$$;

revoke all on function brain.is_eligible(text, boolean, timestamptz, timestamptz) from public;
revoke all on function brain.norm_segments(text) from public;
revoke all on function brain.filtered_documents(text[], text, date, date, text, timestamptz) from public;
revoke all on function brain.recall_candidates(
  text[], vector, text[], text, date, date, text, text, int, int, double precision, int, timestamptz
) from public;
revoke all on function brain.touch_updated_at() from public;

grant execute on function brain.is_eligible(text, boolean, timestamptz, timestamptz) to brain_server;
grant execute on function brain.norm_segments(text) to brain_server;
grant execute on function brain.filtered_documents(text[], text, date, date, text, timestamptz) to brain_server;
grant execute on function brain.recall_candidates(
  text[], vector, text[], text, date, date, text, text, int, int, double precision, int, timestamptz
) to brain_server;

do $anon$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on all functions in schema brain from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on all functions in schema brain from authenticated';
  end if;
end
$anon$;
