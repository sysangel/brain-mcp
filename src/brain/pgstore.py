"""Supabase document store: recall, get, remember, reflect lease.

The canonical store is a Supabase Postgres database (private schema ``brain``).
Cloud Run is only the MCP process. Connections set ``prepare_threshold=None``
so the Supavisor transaction pooler (port 6543) does not reuse prepared
statements across clients. Each checkout sets ``search_path`` for that
transaction only; advisory locks used here are transaction-scoped
(``pg_advisory_xact_lock``), which the pooler keeps pinned until commit.

``remember`` commits the row and a ``git_exports`` outbox entry in one
transaction. A git commit is not required for success.
"""

from __future__ import annotations

import os
import re
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import db, ranking
from .authz import Principal
from .backend import database_url
from .config import (
    DUP_COSINE,
    DUP_SOFT_COSINE,
    FACT_MAX_CHARS,
    HALF_LIFE_DAYS,
    KIND_WEIGHTS,
    RECENCY_FLOOR,
    REFLECT_CLAIM_TTL_S,
    RETURN_TEXT_CAP,
    RRF_K,
    USAGE_MAX_HITS_LOGGED,
)
from .embed_api import embed_material, embed_texts
from .embedconf import embed_provider, embed_space, vec_floor_cos
from .ids import memory_stable_id_v2
from .profile_ctx import current_profile
from .records import project_id as parse_project_id
from .remember import (
    _VALID_TYPES,
    derive_description,
    resolve_scope,
    slugify,
)

_SCOPES = {
    "all": ["episode", "memory", "summary", "proposal", "handoff"],
    "episodes": ["episode"],
    "memories": ["memory"],
    "summaries": ["summary"],
    "proposals": ["proposal"],
    "handoffs": ["handoff"],
}

_PG_KIND_WEIGHTS = {**KIND_WEIGHTS, "proposal": 0.80, "handoff": 0.80}
_PG_HALF_LIVES = {**HALF_LIFE_DAYS, "proposal": 90.0, "handoff": 60.0}

_pool = None


def _configure(conn) -> None:
    from pgvector.psycopg import register_vector

    register_vector(conn)


def pool():
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool

        max_size = int(os.environ.get("BRAIN_PG_POOL_MAX") or "4")
        _pool = ConnectionPool(
            conninfo=database_url(),
            min_size=0,
            max_size=max(1, max_size),
            kwargs={"prepare_threshold": None, "autocommit": False},
            configure=_configure,
            open=True,
            timeout=float(os.environ.get("BRAIN_PG_POOL_TIMEOUT") or "15"),
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def as_vector(value):
    """pgvector's psycopg dumper accepts ``Vector``, not a bare list."""
    if value is None:
        return None
    from pgvector import Vector

    return Vector(list(value))


@contextmanager
def connection():
    from psycopg.rows import tuple_row

    with pool().connection() as conn:
        # Transaction-local: Supavisor drops session state between checkouts.
        # `extensions` is where Supabase installs pgvector; `public` covers a
        # local pgvector image. Reset the row factory so a previous checkout
        # that wanted dict rows does not leak into the next one.
        conn.row_factory = tuple_row
        conn.execute(
            "select set_config('search_path', 'brain, public, extensions', true)"
        )
        yield conn


def _tokens(query: str) -> list[str]:
    """FTS tokens. ``\\w`` keeps Unicode letters (``記憶``, ``café``), matching
    the sqlite recall gate, which treats any word character as searchable."""
    return re.findall(r"\w+", query)[:32]


def _searchable(query: str) -> bool:
    return re.search(r"\w", query) is not None


def _project_label(row: dict) -> str:
    if row.get("project_id"):
        return row["project_id"]
    attrs = row.get("attrs") or {}
    if isinstance(attrs, str):
        attrs = {}
    if attrs.get("project"):
        return str(attrs["project"])
    if row.get("kind") in ("episode", "summary", "proposal", "handoff"):
        cwd = row.get("cwd") or ""
        return Path(cwd).name or cwd or "?"
    return db._memory_project(row.get("relpath") or "")


def _truncate(text: str) -> str:
    if len(text) > RETURN_TEXT_CAP:
        return text[:RETURN_TEXT_CAP] + "\n…[truncated]"
    return text


def _embed_query(text: str):
    if embed_provider() == "local":
        from . import embed

        vec = embed.embed_query(text)
        return [float(x) for x in vec]
    return embed_texts([text[:2000]])[0]


def recall(
    query: str,
    k: int = 6,
    scope: str = "all",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
    *,
    source: str = "mcp",
    user_sub: str | None = None,
) -> dict:
    kinds = _SCOPES.get(scope)
    if kinds is None:
        raise ValueError(
            "scope must be one of all|episodes|memories|summaries|proposals|handoffs, "
            f"got {scope!r}"
        )
    if project is not None and not project.strip():
        raise ValueError("project must be a non-empty string")
    since_r = db._parse_date_bound(since) if since is not None else None
    until_r = db._parse_date_bound(until) if until is not None else None
    profile = current_profile()
    filters_echo: dict = {}
    if project is not None:
        filters_echo["project"] = project
    if since is not None:
        filters_echo["since"] = since_r
    if until is not None:
        filters_echo["until"] = until_r

    searchable = _searchable(query)
    tokens = _tokens(query) if searchable else []
    space = embed_space()
    floor = vec_floor_cos(space)
    qvec = None
    fts_only = True
    # Vector search follows the sqlite rule: any Unicode word is enough.
    # An ASCII-only tokenizer used to skip the embedder for queries like 記憶.
    if searchable and embed_provider() != "off":
        try:
            if embed_provider() == "local":
                from . import embed

                if embed.available():
                    qvec = _embed_query(query)
                    fts_only = False
            else:
                qvec = _embed_query(query)
                fts_only = False
        except Exception:
            qvec = None
            fts_only = True
    if not searchable:
        result = {"fts_only": True, "hits": [], "diag": _diag([], None, True)}
        if filters_echo:
            result["filters"] = filters_echo
            result["filter_candidates"] = 0
        return result

    sql = """
        select kind, id, stable_id, rrf, n_lists, ts, best_cos,
               title, description, body, project_id, cwd, relpath, session_id,
               scope, attrs
          from brain.recall_candidates(
            %s::text[], %s::vector, %s::text[], %s::text,
            %s::date, %s::date, %s::text, %s::text,
            40, 40, %s::float8, %s::int, now()
          )
    """
    count_sql = """
        select count(*) as n
          from brain.filtered_documents(%s, %s, %s::date, %s::date, %s, now())
    """
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        rows = conn.execute(
            sql,
            (
                tokens,
                as_vector(qvec),
                kinds,
                project,
                since_r,
                until_r,
                profile,
                space if qvec is not None else None,
                floor,
                RRF_K,
            ),
        ).fetchall()
        filter_n = None
        if filters_echo or profile:
            filter_n = conn.execute(
                count_sql, (kinds, project, since_r, until_r, profile)
            ).fetchone()["n"]
        conn.commit()

    now = datetime.now(timezone.utc)
    candidates = []
    by_key = {}
    best_cos = None
    for row in rows:
        if row["best_cos"] is not None:
            best_cos = float(row["best_cos"])
        ts = row["ts"].isoformat() if row["ts"] is not None else None
        cand = ranking.Candidate(
            row["kind"], int(row["id"]), float(row["rrf"]), int(row["n_lists"]), ts
        )
        candidates.append(cand)
        by_key[(row["kind"], int(row["id"]))] = row
    ranked = ranking.rank(
        candidates,
        now,
        kind_weights=_PG_KIND_WEIGHTS,
        half_lives=_PG_HALF_LIVES,
        floor=RECENCY_FLOOR,
        rrf_k=RRF_K,
    )
    hits = []
    for score, cand in ranked[: max(k, 0)]:
        row = by_key[(cand.kind, cand.rid)]
        text = row["body"] or ""
        if cand.kind != "episode":
            text = f"{row['description'] or ''} {text}"
        hit = {
            "kind": cand.kind,
            "id": row["stable_id"],
            "date": (cand.ts or "")[:10],
            "project": _project_label(row),
            "snippet": db.make_snippet(text, query),
            "score": round(score, 4),
        }
        if cand.kind in ("episode", "summary"):
            hit["session_id"] = row["session_id"]
        if cand.kind == "memory":
            hit["name"] = row["title"]
        hits.append(hit)
    if qvec is None:
        fts_only = True
    elif best_cos is None and hits:
        # FTS hits with an empty vector leg still count as hybrid attempt that
        # degraded per kind; surface fts_only only when no query vector ran.
        fts_only = False
    result = {"fts_only": fts_only, "hits": hits}
    if filters_echo:
        result["filters"] = filters_echo
        result["filter_candidates"] = int(filter_n or 0)
    diag = _diag(hits, best_cos, fts_only)
    result["diag"] = diag
    _log_recall(
        query=query,
        scope=scope,
        k=k,
        source=source,
        hits=hits,
        diag=diag,
        user_sub=user_sub,
    )
    return result


def _diag(hits: list[dict], best_cos, fts_only: bool) -> dict:
    return {
        "top_score": hits[0]["score"] if hits else None,
        "top_vec_cos": best_cos,
        "n_hits": len(hits),
        "fts_only": fts_only,
    }


def _log_recall(*, query, scope, k, source, hits, diag, user_sub) -> None:
    try:
        with connection() as conn:
            from psycopg.rows import dict_row

            conn.row_factory = dict_row
            row = conn.execute(
                """
                insert into brain.usage_recalls
                  (source, host, user_sub, query, scope, k, fts_only, n_hits,
                   top_score, top_vec_cos)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    source if source in ("mcp", "hook", "cli") else "mcp",
                    socket.gethostname(),
                    user_sub,
                    query,
                    scope,
                    k,
                    bool(diag["fts_only"]),
                    diag["n_hits"],
                    diag["top_score"],
                    diag["top_vec_cos"],
                ),
            ).fetchone()
            recall_id = row["id"]
            for pos, hit in enumerate(hits[:USAGE_MAX_HITS_LOGGED]):
                conn.execute(
                    """
                    insert into brain.usage_recall_hits (recall_id, pos, kind, stable_id, score)
                    values (%s, %s, %s, %s, %s)
                    """,
                    (recall_id, pos, hit["kind"], hit["id"], hit["score"]),
                )
            conn.commit()
    except Exception:
        return


def get_item(item_id: str, *, source: str = "mcp", user_sub: str | None = None) -> dict:
    prefix = item_id.split("_", 1)[0] + "_" if "_" in item_id else ""
    if prefix not in ("ep_", "mem_", "sum_", "prop_", "hand_"):
        return {"error": f"unknown id {item_id!r} (expected an ep_/mem_/sum_/prop_/hand_ prefix)"}
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        row = conn.execute(
            "select * from brain.documents where stable_id = %s",
            (item_id,),
        ).fetchone()
        if row is None and item_id.startswith("mem_"):
            alias = conn.execute(
                "select stable_id from brain.memory_id_aliases where old_id = %s",
                (item_id,),
            ).fetchone()
            if alias is not None:
                row = conn.execute(
                    "select * from brain.documents where stable_id = %s",
                    (alias["stable_id"],),
                ).fetchone()
        if row is None:
            conn.commit()
            return {"error": f"no document with id {item_id}"}
        profile = current_profile()
        if row["kind"] in ("episode", "memory", "summary") and profile and row["scope"] not in (None, profile):
            conn.commit()
            return {"error": f"{row['kind']} {item_id} is unavailable for recall"}
        eligible = conn.execute(
            "select brain.is_eligible(%s, %s, %s, now()) as ok",
            (row["status"], row["recall_enabled"], row["expires_at"]),
        ).fetchone()["ok"]
        if not eligible:
            conn.commit()
            return {"error": f"{row['kind']} {row['stable_id']} is unavailable for recall"}
        conn.execute(
            """
            insert into brain.usage_opens (source, host, user_sub, kind, stable_id)
            values (%s, %s, %s, %s, %s)
            """,
            (source, socket.gethostname(), user_sub, row["kind"], row["stable_id"]),
        )
        conn.commit()
    out = {
        "kind": row["kind"],
        "id": row["stable_id"],
        "ts": row["ts"].isoformat() if row["ts"] is not None else None,
        "path": row["relpath"],
        "project_id": row["project_id"],
        "status": row["status"],
        "recall_enabled": row["recall_enabled"],
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None,
        "title": row["title"],
        "description": row["description"],
        "scope": row["scope"],
        "text": _truncate(row["body"] or ""),
    }
    if row["kind"] == "memory":
        out["name"] = row["title"]
        out["type"] = (row["attrs"] or {}).get("type")
    if row["kind"] in ("episode", "summary"):
        out["session_id"] = row["session_id"]
        out["cwd"] = row["cwd"]
    if row["kind"] == "episode":
        out["uuid"] = row["uuid"]
        out["git_branch"] = row["git_branch"]
        out["is_sidechain"] = bool((row["attrs"] or {}).get("is_sidechain"))
        out["source_file"] = row["relpath"]
    return out


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _scope_clause(profile: str | None) -> tuple[str, list]:
    if not profile:
        return "true", []
    return "(scope is null or scope = %s)", [profile]


def remember(
    fact: str,
    type: str = "project",
    name: str = "",
    session: str = "",
    project: str = "",
    force: bool = False,
    scope: str = "",
    project_id: str = "",
    *,
    principal: Principal | None = None,
) -> dict:
    if type not in _VALID_TYPES:
        return {"error": "type must be one of user|feedback|project|reference"}
    try:
        pid = parse_project_id(project_id) if project_id else None
    except (ValueError, TypeError, AttributeError) as exc:
        return {"error": str(exc)}
    scope_in = (scope or "").strip().lower()
    if scope_in and scope_in not in ("shared", "personal", "work"):
        return {"error": "scope must be one of shared|personal|work"}
    file_scope = resolve_scope(scope_in, type)
    fact = (fact or "").strip()
    if not fact:
        return {"error": "fact must be a non-empty string"}
    if len(fact) > FACT_MAX_CHARS:
        fact = fact[:FACT_MAX_CHARS].strip()
    description = derive_description(fact)
    base_slug = slugify(name) if name else slugify(" ".join(description.split()[:6]))
    if not base_slug:
        base_slug = "memory"
    profile = current_profile()
    vec = None
    try:
        if embed_provider() == "local":
            from . import embed

            if embed.available():
                raw = embed.embed_documents([description + "\n" + fact])[0]
                vec = [float(x) for x in raw]
        else:
            vec = embed_texts([embed_material("memory", base_slug, description, fact)])[0]
    except Exception:
        vec = None

    space = embed_space() if vec is not None else None
    scope_sql, scope_params = _scope_clause(profile)
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        conn.execute("select pg_advisory_xact_lock(hashtext('brain-remember'))")
        eligible_sql = f"""
            select stable_id, title, description, relpath, body, embedding
              from brain.documents
             where kind = 'memory'
               and brain.is_eligible(status, recall_enabled, expires_at, now())
               and {scope_sql}
        """
        mems = conn.execute(eligible_sql, scope_params).fetchall()
        norm_fact = _normalize(fact)
        if norm_fact and not force:
            for mem in mems:
                if _normalize(mem["body"]) == norm_fact:
                    conn.commit()
                    return _dup("duplicate", mem, 1.0, "exact")
        best_cos = -1.0
        best = None
        if vec is not None and not force:
            hit = conn.execute(
                f"""
                select stable_id, title, description, relpath, body,
                       1 - (embedding <=> %s::vector) as cos
                  from brain.documents
                 where kind = 'memory'
                   and embedding is not null
                   and embed_space = %s
                   and brain.is_eligible(status, recall_enabled, expires_at, now())
                   and {scope_sql}
                 order by embedding <=> %s::vector
                 limit 5
                """,
                (as_vector(vec), space, *scope_params, as_vector(vec)),
            ).fetchall()
            for row in hit:
                cos = float(row["cos"])
                if cos > best_cos:
                    best_cos, best = cos, row
            if best is not None and best_cos >= DUP_COSINE:
                conn.commit()
                return _dup("duplicate", best, best_cos, "cosine")
            if best is not None and best_cos >= DUP_SOFT_COSINE:
                conn.commit()
                return _dup("possible_duplicate", best, best_cos, "cosine")
        if not force:
            for mem in mems:
                if (mem["title"] or "") == base_slug:
                    conn.commit()
                    return _dup("possible_duplicate", mem, 1.0, "name")
        slug = base_slug
        n = 2
        while True:
            relpath = f"memories/{slug}.md"
            taken = conn.execute(
                "select 1 from brain.documents where relpath = %s",
                (relpath,),
            ).fetchone()
            if taken is None:
                break
            slug = f"{base_slug}-{n}"
            n += 1
            if n > 1000:
                conn.rollback()
                return {"error": "could not allocate a unique memory slug"}
        sid = memory_stable_id_v2(relpath)
        content_hash = db.memory_content_hash(
            slug, description, type, fact, "", file_scope
        )
        attrs = {"type": type, "session": session, "project": project}
        row = conn.execute(
            """
            insert into brain.documents (
              stable_id, kind, scope, project_id, relpath, title, description, body,
              content_hash, embedded_hash, embedding, embed_space, ts, day,
              status, recall_enabled, attrs, host
            ) values (
              %s, 'memory', %s, %s, %s, %s, %s, %s,
              %s, %s, %s, %s, now(), current_date,
              'active', true, %s::jsonb, %s
            )
            on conflict (stable_id) do update set
              scope = excluded.scope,
              project_id = excluded.project_id,
              relpath = excluded.relpath,
              title = excluded.title,
              description = excluded.description,
              body = excluded.body,
              content_hash = excluded.content_hash,
              embedded_hash = excluded.embedded_hash,
              embedding = excluded.embedding,
              embed_space = excluded.embed_space,
              ts = excluded.ts,
              day = excluded.day,
              status = 'active',
              recall_enabled = true,
              attrs = excluded.attrs,
              host = excluded.host
            returning id, stable_id
            """,
            (
                sid,
                file_scope,
                pid,
                relpath,
                slug,
                description,
                fact,
                content_hash,
                content_hash if vec is not None else None,
                as_vector(vec),
                space,
                _json(attrs),
                (principal.email if principal else None) or socket.gethostname(),
            ),
        ).fetchone()
        conn.execute(
            """
            insert into brain.git_exports (document_id, relpath, body)
            values (%s, %s, %s)
            """,
            (row["id"], relpath, fact),
        )
        if principal and principal.email:
            sub = principal.sub or principal.email
            domain = principal.email.split("@", 1)[-1]
            conn.execute(
                """
                insert into brain.users (sub, email, domain, default_profile)
                values (%s, %s, %s, %s)
                on conflict (sub) do update set email = excluded.email
                """,
                (sub, principal.email, domain, principal.profile),
            )
        conn.commit()
    result = {
        "status": "created",
        "stored": "postgres",
        "name": slug,
        "path": relpath,
        "id": sid,
        "committed": False,
        "git_export": "queued",
    }
    if file_scope:
        result["scope"] = file_scope
    if pid:
        result["project_id"] = pid
    if vec is None:
        result["vec_pending"] = True
    return result


def _json(value: dict) -> str:
    import json

    return json.dumps(value)


def _dup(status: str, row: dict, sim: float, signal: str) -> dict:
    return {
        "status": status,
        "similarity": round(float(sim), 4),
        "signal": signal,
        "existing": {
            "id": row["stable_id"],
            "name": row.get("title"),
            "path": row.get("relpath"),
            "description": row.get("description"),
        },
        "hint": "pass force=true to write anyway",
    }


def _reflect_profile(profile: str | None) -> str | None:
    return profile if profile in ("personal", "work") else None


def reflect_watermark(conn, profile: str | None) -> str | None:
    """ISO watermark for ``profile``.

    A profile reads ``last_reflect:<profile>`` and falls back to the neutral
    ``last_reflect``, so a watermark written before per-profile keys still
    counts. A neutral reader reads only ``last_reflect``: one profile's pass
    does not mark the other profile's episodes reviewed.
    """

    def read(key: str) -> str | None:
        row = conn.execute("select value from brain.meta where key = %s", (key,)).fetchone()
        if row is None:
            return None
        value = row["value"] if isinstance(row, dict) else row[0]
        return value or None

    profile = _reflect_profile(profile)
    if profile:
        return read(f"last_reflect:{profile}") or read("last_reflect")
    return read("last_reflect")


def _write_reflect_watermark(conn, profile: str | None, stamp: datetime) -> None:
    value = stamp.astimezone(timezone.utc).isoformat()
    profile = _reflect_profile(profile)
    # A neutral pass reviewed every profile, so their keys move too.
    keys = [f"last_reflect:{profile}"] if profile else [
        "last_reflect",
        "last_reflect:personal",
        "last_reflect:work",
    ]
    for key in keys:
        conn.execute(
            """
            insert into brain.meta (key, value)
            values (%s, %s)
            on conflict (key) do update
              set value = excluded.value, updated_at = now()
            """,
            (key, value),
        )


def claim_reflect(
    claimed_by: str,
    host: str | None = None,
    ttl_s: float | None = None,
    profile: str | None = None,
) -> dict:
    """Row claim. The advisory lock is transaction-scoped, so it serializes
    the insert under Supavisor transaction mode and does not have to outlive
    the connection. The lease itself is the ``reflect_runs`` row.

    ``profile`` is ``personal``, ``work``, or None. A profile claim blocks
    only that profile. A neutral claim blocks every profile, and any open
    claim blocks a neutral one, because a neutral pass reads every episode.
    """
    ttl = REFLECT_CLAIM_TTL_S if ttl_s is None else ttl_s
    profile = _reflect_profile(profile)
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        conn.execute("select pg_advisory_xact_lock(hashtext('brain-reflect'))")
        conn.execute(
            """
            update brain.reflect_runs
               set status = 'expired'
             where status = 'claimed'
               and claimed_at < now() - make_interval(secs => %s)
            """,
            (ttl,),
        )
        held = conn.execute(
            """
            select id, claimed_by, claimed_at, host, profile
              from brain.reflect_runs
             where status = 'claimed'
               and (
                 profile is null
                 or %s::text is null
                 or profile = %s
               )
             order by id desc
             limit 1
            """,
            (profile, profile),
        ).fetchone()
        if held is not None:
            conn.commit()
            return {
                "ok": False,
                "reason": "held",
                "id": held["id"],
                "claimed_by": held["claimed_by"],
                "host": held["host"],
                "profile": held["profile"],
                "claimed_at": held["claimed_at"].isoformat(),
            }
        row = conn.execute(
            """
            insert into brain.reflect_runs (claimed_by, host, status, profile)
            values (%s, %s, 'claimed', %s)
            returning id, claimed_at
            """,
            (claimed_by, host or socket.gethostname(), profile),
        ).fetchone()
        conn.commit()
        return {
            "ok": True,
            "id": row["id"],
            "claimed_at": row["claimed_at"].isoformat(),
            "claimed_by": claimed_by,
            "profile": profile,
        }


def complete_reflect(run_id: int | None = None, watermark: datetime | None = None) -> dict:
    stamp = watermark or datetime.now(timezone.utc)
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        if run_id is None:
            row = conn.execute(
                """
                update brain.reflect_runs
                   set status = 'completed', completed_at = now(), watermark = %s
                 where id = (
                   select id from brain.reflect_runs
                    where status = 'claimed'
                    order by id desc
                    limit 1
                 )
                returning id, profile
                """,
                (stamp,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                update brain.reflect_runs
                   set status = 'completed', completed_at = now(), watermark = %s
                 where id = %s and status = 'claimed'
                returning id, profile
                """,
                (stamp, run_id),
            ).fetchone()
        if row is None:
            conn.rollback()
            return {"ok": False, "reason": "no open claim"}
        _write_reflect_watermark(conn, row["profile"], stamp)
        conn.commit()
        return {"ok": True, "id": row["id"], "watermark": stamp.isoformat()}


def reflect_status() -> dict:
    profile = _reflect_profile(current_profile())
    with connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        held = conn.execute(
            """
            select id, claimed_by, claimed_at, host, status, profile
              from brain.reflect_runs
             where status = 'claimed'
               and profile is not distinct from %s
             order by id desc
             limit 1
            """,
            (profile,),
        ).fetchone()
        watermark = reflect_watermark(conn, profile)
        conn.commit()
    return {
        "claim": None
        if held is None
        else {
            "id": held["id"],
            "claimed_by": held["claimed_by"],
            "host": held["host"],
            "profile": held["profile"],
            "claimed_at": held["claimed_at"].isoformat(),
        },
        "watermark": watermark,
    }
