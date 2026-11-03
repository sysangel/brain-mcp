"""Hosted writes and reads that the brain skills and hooks need.

The local brain did these with files and git: rewrite
``memories/<project>-state.md``, write ``summaries/YYYY-MM/<session>.md``,
drop a handoff or a proposal, edit or retire a memory during compaction, and
read the reflect window out of SQLite. On Supabase each one is a row in
``brain.documents`` at the same relpath the files used, with the same stable
id scheme as ``brain-pg-load`` (:mod:`brain.ids`), so imported rows and new
rows are the same documents. Every write queues a ``git_exports`` row for the
nightly export, the way ``remember`` does.

Embedding failures do not fail a write. The row lands with no vector (FTS
still finds it) and ``vec_pending`` in the result.

Nothing here logs a body.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
from datetime import datetime, timedelta, timezone

from . import db
from .config import BRAIN_DIGEST_EXCLUDE, RETURN_TEXT_CAP
from .embed_api import embed_material, embed_texts
from .embedconf import embed_provider, embed_space
from .ids import handoff_stable_id, memory_stable_id_v2, proposal_stable_id, summary_stable_id
from .profile_ctx import current_profile
from .records import project_id as parse_project_id
from .remember import derive_description, slugify

STATE_BODY_MAX = 6_000
SUMMARY_BODY_MAX = 4_000
LOOSE_BODY_MAX = 20_000
DESCRIPTION_MAX = 300
_NOT_SIDECHAIN = "coalesce((attrs->>'is_sidechain')::boolean, false) = false"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _sha(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _project(value: str) -> str:
    """Canonical project name. Raises ValueError on junk."""
    return parse_project_id(value or "")


def _maybe_project(value: str | None) -> str | None:
    try:
        return _project(value) if value else None
    except ValueError:
        return None


def _scope_sql(profile: str | None, column: str = "scope") -> tuple[str, list]:
    if not profile:
        return "true", []
    return f"({column} is null or {column} = %s)", [profile]


def _excluded(cwd: str | None) -> bool:
    c = cwd or ""
    return any(x in c for x in BRAIN_DIGEST_EXCLUDE)


def _embed_one(kind: str, title, description, body) -> list[float] | None:
    try:
        if embed_provider() == "off":
            return None
        if embed_provider() == "local":
            from . import embed

            if not embed.available():
                return None
            return [float(x) for x in embed.embed_documents([f"{description or ''}\n{body or ''}"])[0]]
        return embed_texts([embed_material(kind, title, description, body)])[0]
    except Exception:
        return None


def _connection():
    from .pgstore import connection

    return connection()


def upsert_document(conn, doc: dict) -> dict:
    """Write one document by ``stable_id``. Re-sending the same content is a
    no-op that still returns the id. The caller commits.

    ``doc`` keys: stable_id, kind, relpath, title, description, body,
    content_hash, plus optional scope, project_id, ts, cwd, session_id,
    attrs. Lifecycle is reset to active, which is what rewriting a file did.
    """
    from .pgstore import as_vector

    existing = conn.execute(
        """
        select id, content_hash, embedded_hash, embed_space, status, scope,
               embedding is not null as has_vec
          from brain.documents where stable_id = %s
        """,
        (doc["stable_id"],),
    ).fetchone()
    if (
        existing is not None
        and existing["content_hash"] == doc["content_hash"]
        and existing["status"] == "active"
        and (existing["scope"] or None) == (doc.get("scope") or None)
    ):
        return {"id": doc["stable_id"], "status": "unchanged", "path": doc["relpath"]}
    vec = _embed_one(doc["kind"], doc.get("title"), doc.get("description"), doc.get("body"))
    space = embed_space() if vec is not None else None
    ts = doc.get("ts") or _now()
    row = conn.execute(
        """
        insert into brain.documents (
          stable_id, kind, scope, project_id, relpath, title, description, body,
          content_hash, embedded_hash, embedding, embed_space,
          ts, day, cwd, session_id, host, status, recall_enabled, attrs
        ) values (
          %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s,
          %s, %s, %s, %s, %s, 'active', true, %s::jsonb
        )
        on conflict (stable_id) do update set
          scope = excluded.scope,
          project_id = coalesce(excluded.project_id, brain.documents.project_id),
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
          cwd = coalesce(excluded.cwd, brain.documents.cwd),
          session_id = coalesce(excluded.session_id, brain.documents.session_id),
          host = excluded.host,
          status = 'active',
          recall_enabled = true,
          archived_at = null,
          attrs = brain.documents.attrs || excluded.attrs
        returning id
        """,
        (
            doc["stable_id"],
            doc["kind"],
            doc.get("scope"),
            doc.get("project_id"),
            doc["relpath"],
            doc.get("title"),
            doc.get("description"),
            doc.get("body"),
            doc["content_hash"],
            doc["content_hash"] if vec is not None else None,
            as_vector(vec),
            space,
            ts,
            ts.date(),
            doc.get("cwd"),
            doc.get("session_id"),
            doc.get("host") or socket.gethostname(),
            json.dumps(doc.get("attrs") or {}),
        ),
    ).fetchone()
    conn.execute(
        "insert into brain.git_exports (document_id, relpath, body) values (%s, %s, %s)",
        (row["id"], doc["relpath"], doc.get("body")),
    )
    out = {
        "id": doc["stable_id"],
        "status": "created" if existing is None else "updated",
        "path": doc["relpath"],
        "git_export": "queued",
    }
    if vec is None:
        out["vec_pending"] = True
    return out


def _run_write(doc: dict) -> dict:
    from psycopg.rows import dict_row

    with _connection() as conn:
        conn.row_factory = dict_row
        out = upsert_document(conn, doc)
        conn.commit()
    return out


def _clean(text: str, cap: int) -> str:
    text = (text or "").strip()
    return text[:cap].rstrip() if len(text) > cap else text


# ---------------------------------------------------------- state notes ---

def state_relpath(project: str) -> str:
    return f"memories/{_project(project)}-state.md"


def state_doc(project: str, body: str, description: str = "", *, written: datetime | None = None) -> dict:
    """The row for ``memories/<project>-state.md``. Never scoped: state notes
    stay shared across profiles, same rule reflect applied to the files."""
    name = f"{_project(project)}-state"
    body = _clean(body, STATE_BODY_MAX)
    if not body:
        raise ValueError("body must be a non-empty string")
    description = _clean(description, DESCRIPTION_MAX) or derive_description(body)
    stamp = written or _now()
    date = stamp.date().isoformat()
    relpath = state_relpath(project)
    return {
        "stable_id": memory_stable_id_v2(relpath),
        "kind": "memory",
        "relpath": relpath,
        "title": name,
        "description": description,
        "body": body,
        "content_hash": db.memory_content_hash(name, description, "project", body, date, None),
        "scope": None,
        "project_id": _project(project),
        "ts": stamp,
        "attrs": {"type": "project", "written": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")},
    }


def state_write(project: str, body: str, description: str = "") -> dict:
    """Overwrite a project's state note wholesale."""
    try:
        doc = state_doc(project, body, description)
    except ValueError as exc:
        return {"error": str(exc)}
    out = _run_write(doc)
    out["written"] = doc["attrs"]["written"]
    return out


def _cwd_clause(roots: list[str]) -> tuple[str, list]:
    """Episodes whose cwd is one of ``roots`` or under it, either separator."""
    clauses: list[str] = []
    params: list = []
    for root in roots:
        root = (root or "").rstrip("/\\")
        if not root:
            continue
        for sep in ("/", "\\"):
            prefix = root + sep
            clauses.append("(cwd = %s or left(cwd, %s) = %s)")
            params += [root, len(prefix), prefix]
    if not clauses:
        return "false", []
    return "(" + " or ".join(clauses) + ")", params


def read_state(conn, project: str, roots: list[str], profile: str | None) -> dict:
    """The state note plus its freshness numbers, or ``{"found": False}``."""
    try:
        relpath = state_relpath(project)
    except ValueError:
        return {"found": False}
    row = conn.execute(
        """
        select stable_id, title, description, body, ts, updated_at, attrs
          from brain.documents
         where relpath = %s and kind = 'memory'
           and brain.is_eligible(status, recall_enabled, expires_at, now())
        """,
        (relpath,),
    ).fetchone()
    if row is None or not (row["body"] or "").strip():
        return {"found": False}
    attrs = row["attrs"] or {}
    written = None
    if isinstance(attrs, dict) and attrs.get("written"):
        try:
            written = datetime.fromisoformat(str(attrs["written"]).replace("Z", "+00:00"))
        except ValueError:
            written = None
    if written is None:
        written = row["ts"]
    n_since = None
    if written is not None:
        cwd_sql, cwd_params = _cwd_clause(roots)
        scope_sql, scope_params = _scope_sql(profile)
        n_since = int(
            conn.execute(
                f"""
                select count(*) as n from brain.documents
                 where kind = 'episode' and {_NOT_SIDECHAIN}
                   and ts > %s and {cwd_sql} and {scope_sql}
                """,
                (written, *cwd_params, *scope_params),
            ).fetchone()["n"]
        )
    return {
        "found": True,
        "id": row["stable_id"],
        "path": relpath,
        "name": row["title"],
        "description": row["description"],
        "body": row["body"],
        "written": written.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if written else None,
        "episodes_since": n_since,
    }


def reflect_debt(conn, profile: str | None = None) -> dict:
    """Sessions with non-sidechain episodes since the last reflect.

    ``profile`` reads that profile's watermark (falling back to the shared
    one). A neutral reader uses only the shared watermark, so a work pass
    does not clear personal debt.
    """
    from .pgstore import reflect_watermark

    raw = reflect_watermark(conn, profile)
    watermark = None
    if raw:
        try:
            watermark = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            watermark = None
    scope_sql, scope_params = _scope_sql(profile)
    since = watermark or (_now() - timedelta(days=7))
    n = conn.execute(
        f"""
        select count(distinct session_id) as n from brain.documents
         where kind = 'episode' and {_NOT_SIDECHAIN} and ts > %s and {scope_sql}
        """,
        (since, *scope_params),
    ).fetchone()["n"]
    hours = None
    if watermark is not None:
        hours = round((_now() - watermark).total_seconds() / 3600, 1)
    return {"last_reflect": _iso(watermark), "sessions": int(n), "hours_since": hours}


def state_get(project: str, roots: list[str] | None = None) -> dict:
    from psycopg.rows import dict_row

    with _connection() as conn:
        conn.row_factory = dict_row
        out = read_state(conn, project, roots or [], current_profile())
        conn.commit()
    if out.get("found"):
        out["body"] = out["body"][:RETURN_TEXT_CAP]
    return out


# ------------------------------------------------------------ summaries ---

def _session_episode_scopes(conn, session_id: str) -> set[str | None]:
    rows = conn.execute(
        f"""
        select distinct scope
          from brain.documents
         where kind = 'episode' and session_id = %s
           and {_NOT_SIDECHAIN} and status = 'active'
        """,
        (session_id,),
    ).fetchall()
    return {row["scope"] for row in rows}


def _digest_scope(scopes: set[str | None], caller: str | None) -> tuple[str | None, str | None]:
    """``(scope, error)`` for a digest, using the scope ingest stored.

    Episodes that share one profile produce a digest of that profile, so a
    work session is not readable from personal. Episodes ingest left unscoped
    stay a shared digest (scope NULL), which is also how digests written
    before this rule remain readable. A session with no episodes takes the
    caller's profile when they have one, the same rule as a single-profile
    device that must not publish a row every profile can see. Mixed profiles,
    or a caller outside the session's profile, are refused.
    """
    if not scopes:
        return caller, None
    if caller and any(scope not in (None, caller) for scope in scopes):
        return None, "this session's episodes belong to another profile"
    concrete = {scope for scope in scopes if scope}
    if len(concrete) > 1:
        return None, "this session's episodes span more than one profile"
    if len(concrete) == 1:
        return next(iter(concrete)), None
    return None, None


def summary_write(
    session_id: str,
    body: str,
    description: str,
    project: str = "",
    cwd: str = "",
    episodes: int = 0,
    host: str = "",
) -> dict:
    """Write or rewrite one session digest. A rewrite keeps the path the
    session already has, so a resumed session never gets a second copy.

    Scope follows the session's episodes (see ``_digest_scope``). A profile
    cannot overwrite another profile's digest.
    """
    from psycopg.rows import dict_row

    session_id = (session_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", session_id):
        return {"error": "session_id must be a transcript session id"}
    body = _clean(body, SUMMARY_BODY_MAX)
    if not body:
        return {"error": "body must be a non-empty string"}
    description = _clean(description, DESCRIPTION_MAX) or derive_description(body)
    with _connection() as conn:
        conn.row_factory = dict_row
        if _excluded(cwd):
            conn.commit()
            return {"error": "this session's cwd is on BRAIN_DIGEST_EXCLUDE; not digested"}
        caller = current_profile()
        scope, scope_err = _digest_scope(_session_episode_scopes(conn, session_id), caller)
        if scope_err:
            conn.commit()
            return {"error": scope_err}
        if caller:
            foreign = conn.execute(
                """
                select 1 from brain.documents
                 where kind = 'summary' and session_id = %s
                   and scope is not null and scope <> %s
                 limit 1
                """,
                (session_id, caller),
            ).fetchone()
            if foreign is not None:
                conn.commit()
                return {"error": "a digest for this session belongs to another profile"}
        prior = conn.execute(
            """
            select relpath from brain.documents
             where kind = 'summary' and session_id = %s
             order by updated_at desc limit 1
            """,
            (session_id,),
        ).fetchone()
        first = conn.execute(
            """
            select min(ts) as ts, count(*) as n,
                   (array_agg(cwd order by ts))[1] as cwd
              from brain.documents
             where kind = 'episode' and session_id = %s and """ + _NOT_SIDECHAIN,
            (session_id,),
        ).fetchone()
        cwd = cwd or (first["cwd"] or "")
        if _excluded(cwd):
            conn.commit()
            return {"error": "this session's cwd is on BRAIN_DIGEST_EXCLUDE; not digested"}
        start = first["ts"] or _now()
        relpath = prior["relpath"] if prior else f"summaries/{start:%Y-%m}/{session_id}.md"
        project = project or (re.split(r"[\\/]", cwd.rstrip("/\\"))[-1] if cwd else "")
        host = host or socket.gethostname()
        n = int(episodes or first["n"] or 0)
        date = start.date().isoformat()
        from .pg_loader import summary_content_hash

        doc = {
            "stable_id": summary_stable_id(relpath),
            "kind": "summary",
            "relpath": relpath,
            "title": project or session_id,
            "description": description,
            "body": body,
            "content_hash": summary_content_hash(session_id, date, project, cwd, host, description, body),
            "project_id": _maybe_project(project),
            "ts": start,
            "cwd": cwd or None,
            "session_id": session_id,
            "host": host,
            "scope": scope,
            "attrs": {"project": project, "episodes": n},
        }
        out = upsert_document(conn, doc)
        conn.commit()
    return out


# ---------------------------------------------------- handoffs, proposals ---

def _loose_doc(kind: str, relpath: str, body: str, description: str, project: str, ts: datetime) -> dict:
    title = relpath.rsplit("/", 1)[-1].removesuffix(".md")
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip() or title
            break
    sid = proposal_stable_id(relpath) if kind == "proposal" else handoff_stable_id(relpath)
    return {
        "stable_id": sid,
        "kind": kind,
        "relpath": relpath,
        "title": title,
        "description": description,
        "body": body,
        "content_hash": _sha(kind, relpath, description, project, body),
        "project_id": _maybe_project(re.split(r"[\\/]", project.rstrip("/\\"))[-1] if project else ""),
        "ts": ts,
        "attrs": {"project": project} if project else {},
    }


def handoff_write(slug: str, body: str, description: str, project: str = "") -> dict:
    """Write ``handoffs/<UTC YYYY-MM-DD-HHMM>-<slug>.md``."""
    slug = slugify(slug or "")
    body = _clean(body, LOOSE_BODY_MAX)
    if not slug or not body:
        return {"error": "slug and body must be non-empty"}
    now = _now()
    relpath = f"handoffs/{now:%Y-%m-%d-%H%M}-{slug}.md"
    return _run_write(
        _loose_doc("handoff", relpath, body, _clean(description, DESCRIPTION_MAX), project, now)
    )


def proposal_write(slug: str, body: str, description: str) -> dict:
    """Write ``proposals/<UTC YYYY-MM-DD>-<slug>.md``. Same day, same slug overwrites."""
    slug = slugify(slug or "")
    body = _clean(body, LOOSE_BODY_MAX)
    if not slug or not body:
        return {"error": "slug and body must be non-empty"}
    now = _now()
    relpath = f"proposals/{now:%Y-%m-%d}-{slug}.md"
    return _run_write(
        _loose_doc("proposal", relpath, body, _clean(description, DESCRIPTION_MAX), "", now)
    )


def handoff_latest(topic: str = "", limit: int = 1) -> dict:
    """Newest handoff(s), optionally matching ``topic`` in path, title, or
    description. Full text, capped like get_episode."""
    from psycopg.rows import dict_row

    limit = max(1, min(int(limit or 1), 10))
    where = "kind = 'handoff' and brain.is_eligible(status, recall_enabled, expires_at, now())"
    params: list = []
    if (topic or "").strip():
        where += " and (relpath ilike %s or title ilike %s or description ilike %s)"
        like = "%" + topic.strip().replace("%", "").replace("_", "\\_") + "%"
        params += [like, like, like]
    with _connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            f"""
            select stable_id, relpath, title, description, body, ts, attrs
              from brain.documents where {where}
             order by relpath desc limit %s
            """,
            (*params, limit),
        ).fetchall()
        conn.commit()
    return {
        "handoffs": [
            {
                "id": r["stable_id"],
                "path": r["relpath"],
                "title": r["title"],
                "description": r["description"],
                "project": (r["attrs"] or {}).get("project"),
                "ts": _iso(r["ts"]),
                "text": (r["body"] or "")[:RETURN_TEXT_CAP],
            }
            for r in rows
        ]
    }


# ------------------------------------------------- memory maintenance ---

def memory_update(id: str, fact: str, description: str = "") -> dict:
    """Overwrite an existing memory's body in place (compaction merges and
    corrections). Name, type, scope, path, and lifecycle stay; the id stays
    valid.

    Archived and forgotten memories are refused. ``upsert_document`` resets
    lifecycle to active because rewriting a file (a state note, a digest)
    republishes it, and a compaction merge into an archived memory would
    otherwise put that memory back into recall. Restoring is
    ``memory_set_status``'s job. Refusing, rather than writing while keeping
    the old status, keeps a merge from reporting success for a fact that
    stays out of recall.
    """
    from psycopg.rows import dict_row

    fact = _clean(fact, STATE_BODY_MAX)
    if not fact:
        return {"error": "fact must be a non-empty string"}
    with _connection() as conn:
        conn.row_factory = dict_row
        row = conn.execute(
            """
            select stable_id, relpath, title, scope, project_id, attrs, status
              from brain.documents where stable_id = %s and kind = 'memory'
            """,
            (id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"error": f"no memory with id {id}"}
        profile = current_profile()
        if profile and row["scope"] not in (None, profile):
            conn.commit()
            return {"error": f"memory {id} belongs to another profile"}
        if row["status"] != "active":
            conn.commit()
            return {
                "error": (
                    f"memory {id} is {row['status']}; memory_update only edits "
                    "active memories and does not restore them. Use "
                    "memory_set_status to change lifecycle."
                )
            }
        attrs = row["attrs"] or {}
        mtype = attrs.get("type") if isinstance(attrs, dict) else None
        desc = _clean(description, DESCRIPTION_MAX) or derive_description(fact)
        now = _now()
        doc = {
            "stable_id": row["stable_id"],
            "kind": "memory",
            "relpath": row["relpath"],
            "title": row["title"],
            "description": desc,
            "body": fact,
            "content_hash": db.memory_content_hash(
                row["title"], desc, mtype, fact, now.date().isoformat(), row["scope"]
            ),
            "scope": row["scope"],
            "project_id": row["project_id"],
            "ts": now,
            "attrs": {"type": mtype} if mtype else {},
        }
        out = upsert_document(conn, doc)
        conn.commit()
    return out


def memory_set_status(id: str, status: str) -> dict:
    """Archive, forget, or restore a memory. ``forgotten`` is the delete the
    nightly export applies to git; ``archived`` keeps the row out of recall."""
    from psycopg.rows import dict_row

    if status not in ("active", "archived", "forgotten"):
        return {"error": "status must be active, archived, or forgotten"}
    with _connection() as conn:
        conn.row_factory = dict_row
        row = conn.execute(
            """
            update brain.documents
               set status = %s,
                   archived_at = case when %s = 'archived' then now() else null end
             where stable_id = %s and kind = 'memory'
               and (%s::text is null or scope is null or scope = %s)
            returning id, relpath
            """,
            (status, status, id, current_profile(), current_profile()),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"error": f"no memory with id {id} in this profile"}
        conn.execute(
            "insert into brain.git_exports (document_id, relpath) values (%s, %s)",
            (row["id"], row["relpath"]),
        )
        conn.commit()
    return {"id": id, "status": status, "git_export": "queued"}


# --------------------------------------------------------- reflect reads ---

def _parse_since(since: str | None, conn) -> datetime:
    if since:
        value = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    from .pgstore import reflect_watermark

    raw = reflect_watermark(conn, current_profile())
    if raw:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return _now() - timedelta(days=7)


def reflect_window(since: str = "", limit: int = 200, offset: int = 0, chars: int = 600) -> dict:
    """Non-sidechain episodes after ``since`` (default: the reflect
    watermark), oldest first, with a leading slice of each body."""
    from psycopg.rows import dict_row

    limit = max(1, min(int(limit or 200), 500))
    offset = max(0, int(offset or 0))
    chars = max(100, min(int(chars or 600), 2500))
    scope_sql, scope_params = _scope_sql(current_profile())
    with _connection() as conn:
        conn.row_factory = dict_row
        try:
            start = _parse_since(since, conn)
        except ValueError:
            conn.commit()
            return {"error": "since must be an ISO-8601 timestamp"}
        total = conn.execute(
            f"""
            select count(*) as n from brain.documents
             where kind = 'episode' and {_NOT_SIDECHAIN} and ts > %s and {scope_sql}
            """,
            (start, *scope_params),
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            select stable_id, ts, session_id, cwd, scope, left(body, %s) as text
              from brain.documents
             where kind = 'episode' and {_NOT_SIDECHAIN} and ts > %s and {scope_sql}
             order by ts, stable_id
             limit %s offset %s
            """,
            (chars, start, *scope_params, limit, offset),
        ).fetchall()
        conn.commit()
    return {
        "since": start.isoformat(),
        "total": int(total),
        "offset": offset,
        "episodes": [
            {
                "id": r["stable_id"],
                "ts": _iso(r["ts"]),
                "session_id": r["session_id"],
                "cwd": r["cwd"],
                "scope": r["scope"],
                "text": r["text"],
            }
            for r in rows
            if not _excluded(r["cwd"])
        ],
    }


def reflect_sessions(limit: int = 5) -> dict:
    """Digest candidates, newest first: sessions with at least 3 episodes or
    15,000 characters, idle for an hour, and either no digest yet or at
    least double the episodes the digest covered. Excluded cwds are dropped."""
    from psycopg.rows import dict_row

    limit = max(1, min(int(limit or 5), 50))
    profile = current_profile()
    scope_sql, scope_params = _scope_sql(profile)
    with _connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            f"""
            with s as (
              select session_id, count(*) as n, sum(length(body)) as chars,
                     min(ts) as first_ts, max(ts) as last_ts,
                     (array_agg(cwd order by ts))[1] as cwd,
                     (array_agg(scope order by ts))[1] as scope
                from brain.documents
               where kind = 'episode' and {_NOT_SIDECHAIN}
                 and session_id is not null and status = 'active' and {scope_sql}
               group by session_id
              having (count(*) >= 3 or sum(length(body)) >= 15000)
                 and max(ts) < now() - interval '1 hour'
            ), d as (
              select distinct on (session_id) session_id, relpath,
                     nullif(attrs->>'episodes', '')::int as covered
                from brain.documents
               where kind = 'summary' and session_id is not null
                 and (scope is null or %s::text is null or scope = %s)
               order by session_id, updated_at desc
            )
            select s.*, d.relpath as digest_path, d.covered
              from s left join d using (session_id)
             where d.session_id is null or (d.covered is not null and s.n >= 2 * d.covered)
             order by s.last_ts desc
             limit %s
            """,
            (*scope_params, profile, profile, limit * 4),
        ).fetchall()
        conn.commit()
    out = []
    for r in rows:
        if _excluded(r["cwd"]):
            continue
        out.append(
            {
                "session_id": r["session_id"],
                "episodes": int(r["n"]),
                "chars": int(r["chars"] or 0),
                "first_ts": _iso(r["first_ts"]),
                "last_ts": _iso(r["last_ts"]),
                "cwd": r["cwd"],
                "scope": r["scope"],
                "rewrite_of": r["digest_path"],
            }
        )
        if len(out) >= limit:
            break
    return {"sessions": out}


def session_episodes(session_id: str, chars: int = 2500, limit: int = 200) -> dict:
    """One session's non-sidechain episodes in order, each body cut to ``chars``."""
    from psycopg.rows import dict_row

    chars = max(200, min(int(chars or 2500), 8000))
    limit = max(1, min(int(limit or 200), 500))
    scope_sql, scope_params = _scope_sql(current_profile())
    with _connection() as conn:
        conn.row_factory = dict_row
        rows = conn.execute(
            f"""
            select stable_id, ts, cwd, scope, left(body, %s) as text
              from brain.documents
             where kind = 'episode' and session_id = %s and {_NOT_SIDECHAIN} and {scope_sql}
             order by ts, stable_id
             limit %s
            """,
            (chars, session_id, *scope_params, limit),
        ).fetchall()
        conn.commit()
    if rows and _excluded(rows[0]["cwd"]):
        return {"error": "this session's cwd is on BRAIN_DIGEST_EXCLUDE"}
    return {
        "session_id": session_id,
        "episodes": [
            {"id": r["stable_id"], "ts": _iso(r["ts"]), "cwd": r["cwd"], "scope": r["scope"], "text": r["text"]}
            for r in rows
        ],
    }


# --------------------------------------------------------- reflect lease ---

def reflect_claim(claimed_by: str) -> dict:
    """Claim the reflect lease for the caller's profile. On success also
    returns that profile's window start."""
    from .pgstore import claim_reflect, reflect_status

    profile = current_profile()
    if profile not in ("personal", "work"):
        profile = None
    out = claim_reflect(claimed_by, profile=profile)
    if out.get("ok"):
        out["window_start"] = reflect_status()["watermark"] or (_now() - timedelta(days=7)).isoformat()
    return out


def reflect_complete(run_id: int) -> dict:
    """Close a claim. The watermark becomes the claim time, so episodes that
    arrived while the pass ran are reviewed by the next one."""
    from psycopg.rows import dict_row

    from .pgstore import complete_reflect

    with _connection() as conn:
        conn.row_factory = dict_row
        row = conn.execute(
            "select claimed_at from brain.reflect_runs where id = %s and status = 'claimed'",
            (int(run_id),),
        ).fetchone()
        conn.commit()
    if row is None:
        return {"ok": False, "reason": "no open claim with that id"}
    return complete_reflect(int(run_id), watermark=row["claimed_at"])
