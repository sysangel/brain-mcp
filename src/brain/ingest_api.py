"""Hosted session ingest: ``POST /ingest/episodes``.

``brain-push`` parses Claude Code transcripts on the laptop with the same
parser local ``brain-ingest`` uses and posts batches here. This module checks
the device token (scope ``ingest``), validates each episode, embeds new or changed text with the
process embedder (OpenAI ``text-embedding-3-small`` at 256 dimensions on
Cloud Run), and upserts by ``stable_id``.

Idempotent. The server recomputes ``content_hash`` from the fields it stores,
so a re-sent episode compares equal and nothing is written or embedded. A
still-growing session keeps its ``stable_id`` (sessionId + uuid of the
opening prompt) and gets a new hash as assistant text arrives; that updates
the row in place and re-embeds it. The guards mirror local ingest:

- a new ``stable_id`` whose content hash is already stored under another
  episode is a resumed/forked copy and is skipped;
- an update never moves an episode back in time (``ts``), and a payload built
  from an older read of the transcript (``source_mtime``) does not overwrite
  a newer one;
- lifecycle columns (``status``, ``recall_enabled``, ``expires_at``) are
  never touched, so a forgotten or archived episode stays that way.

Nothing is ever deleted here. Episodes that drop out of a transcript stay in
the store, which is what the git-tracked ``episodes/`` tree did.

Git export follows the old privacy gate: an episode whose cwd matches
``BRAIN_EPISODE_EXCLUDE`` on the server, or that the client flags with
``"git_export": false`` (its own exclude list), is stored and recallable but
never queued for the nightly export. Plan-and-write takes a
transaction-scoped advisory lock (``pg_advisory_xact_lock``), which the
transaction pooler holds only until commit. Embeddings are computed
before that transaction, with no pooled connection checked out, so a slow
OpenAI call cannot pin one of the pool's few slots or block ``/mcp``.
The locked section plans again and writes only what is still missing, so
a batch that landed during the embed is a no-op or a duplicate instead of
a second row. Forks of one transcript dedupe on content hash across
sessions, so the lock stays global for that short write. Session-level
advisory locks are not used; the pooler would leak them onto the next
client.

Episode bodies can carry client data. Nothing in this module logs a body;
the route logs counts only.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath

from . import db
from .config import EPISODE_TEXT_CAP, normalize_scope
from .embed_api import embed_material, embed_texts
from .embedconf import embed_provider, embed_space
from .ingest import _episode_excluded, _episode_shard
from .device_tokens import DeviceIdentity, redact_tokens

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_EPISODES = 200
_FIELD_CAPS = {
    "session_id": 200,
    "uuid": 200,
    "ts": 64,
    "cwd": 4096,
    "git_branch": 512,
}
_STABLE_ID = re.compile(r"^ep_[0-9a-f]{16}$")


class EmbedUnavailable(RuntimeError):
    """The embedder failed. Nothing was written; the client retries later."""


# ------------------------------------------------------------ validation ---

def _parse_ts(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _float_or_none(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _project_from_cwd(cwd: str) -> str | None:
    """Project id from the session directory name, Windows or POSIX spelling."""
    if not cwd:
        return None
    path = PureWindowsPath(cwd) if ("\\" in cwd or re.match(r"^[A-Za-z]:", cwd)) else PurePosixPath(cwd)
    try:
        from .records import project_id

        return project_id(path.name) if path.name else None
    except ValueError:
        return None


def validate_episode(raw, identity: DeviceIdentity) -> tuple[dict | None, str | None]:
    """A normalized episode ready to store, or the reason it was refused."""
    if not isinstance(raw, dict):
        return None, "episode must be an object"
    sid = raw.get("stable_id")
    if not isinstance(sid, str) or not _STABLE_ID.match(sid):
        return None, "stable_id must look like ep_ + 16 hex"
    fields: dict[str, str] = {}
    for key, cap in _FIELD_CAPS.items():
        value = raw.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str):
            return None, f"{key} must be a string"
        if len(value) > cap:
            return None, f"{key} is longer than {cap} characters"
        fields[key] = value
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "text must be a non-empty string"
    if len(text) > EPISODE_TEXT_CAP:
        return None, f"text is longer than {EPISODE_TEXT_CAP} characters"
    text = redact_tokens(text)
    # Always derive the id: without this a caller could aim any stable_id
    # (another profile's episode included) at its own text.
    if not fields["session_id"] or not fields["uuid"]:
        return None, "session_id and uuid are required"
    if db.episode_stable_id(fields["session_id"], fields["uuid"]) != sid:
        return None, "stable_id does not match session_id/uuid"
    is_sidechain = 1 if raw.get("is_sidechain") in (1, True, "1", "true") else 0

    scope_raw = raw.get("scope")
    scope = normalize_scope(scope_raw) if isinstance(scope_raw, str) else None
    if scope_raw not in (None, "") and scope is None:
        return None, "scope must be personal, work, or empty"
    if scope is not None and scope not in identity.profiles:
        return None, f"this device token cannot write {scope} episodes"
    if scope is None and len(identity.profiles) == 1:
        # A single-profile device must not create rows every profile can see.
        scope = identity.profiles[0]

    export = raw.get("git_export", True) not in (False, 0, "0", "false")
    if _episode_excluded(fields["cwd"]):
        export = False

    ts_dt = _parse_ts(fields["ts"])
    content_hash = db.episode_content_hash(
        fields["ts"], fields["cwd"], fields["git_branch"], is_sidechain, text
    )
    return {
        "stable_id": sid,
        "session_id": fields["session_id"] or None,
        "uuid": fields["uuid"] or None,
        "ts": fields["ts"],
        "ts_dt": ts_dt,
        "cwd": fields["cwd"] or None,
        "git_branch": fields["git_branch"] or None,
        "is_sidechain": is_sidechain,
        "text": text,
        "scope": scope,
        "content_hash": content_hash,
        "source_mtime": _float_or_none(raw.get("source_mtime")),
        "relpath": f"episodes/{_episode_shard(fields['ts'])}/{sid}.md",
        "project_id": _project_from_cwd(fields["cwd"]),
        "git_export": export,
    }, None


def parse_payload(payload, identity: DeviceIdentity) -> tuple[list[dict], list[dict]]:
    """Valid episodes (last copy of a repeated id wins) and per-item refusals."""
    if not isinstance(payload, dict):
        raise ValueError("body must be a JSON object")
    items = payload.get("episodes")
    if not isinstance(items, list):
        raise ValueError("episodes must be a list")
    if len(items) > MAX_EPISODES:
        raise ValueError(f"at most {MAX_EPISODES} episodes per request")
    good: dict[str, dict] = {}
    rejected: list[dict] = []
    for index, raw in enumerate(items):
        ep, reason = validate_episode(raw, identity)
        if ep is None:
            sid = raw.get("stable_id") if isinstance(raw, dict) else None
            rejected.append({"index": index, "stable_id": sid, "reason": reason})
            continue
        good[ep["stable_id"]] = ep
    return list(good.values()), rejected


# ---------------------------------------------------------------- upsert ---

def _embed_episodes(texts: list[str]) -> tuple[list | None, str | None]:
    """Vectors for ``texts`` and their space. ``(None, None)`` when embedding
    is switched off. Raises :class:`EmbedUnavailable` on a provider failure."""
    if not texts:
        return [], embed_space()
    provider = embed_provider()
    if provider == "off":
        return None, None
    material = [embed_material("episode", None, None, t) for t in texts]
    try:
        if provider == "local":
            from . import embed

            if not embed.available():
                return None, None
            vecs = [[float(x) for x in v] for v in embed.embed_documents(material)]
        else:
            vecs = embed_texts(material)
    except Exception as exc:
        raise EmbedUnavailable(type(exc).__name__) from exc
    if len(vecs) != len(texts):
        raise EmbedUnavailable("embedder returned the wrong number of vectors")
    return vecs, embed_space()


def _plan(
    conn, episodes: list[dict], space: str, profiles: tuple[str, ...] | list[str]
) -> tuple[dict[str, str], dict[str, dict]]:
    """Decide what each episode needs: insert, update, revector, or a skip.

    A stored row scoped to a profile the token cannot write is ``forbidden``
    whatever the payload says; the stored scope is never changed."""
    ids = [e["stable_id"] for e in episodes]
    existing = {
        row["stable_id"]: row
        for row in conn.execute(
            """
            select stable_id, kind, scope, content_hash, embedded_hash, embed_space,
                   embedding is not null as has_vec, ts, attrs
              from brain.documents
             where stable_id = any(%s)
            """,
            (ids,),
        ).fetchall()
    }
    new_hashes = [e["content_hash"] for e in episodes if e["stable_id"] not in existing]
    taken: set[str] = set()
    if new_hashes:
        taken = {
            row["content_hash"]
            for row in conn.execute(
                """
                select distinct content_hash
                  from brain.documents
                 where kind = 'episode'
                   and content_hash = any(%s)
                   and stable_id <> all(%s)
                """,
                (new_hashes, ids),
            ).fetchall()
        }
    actions: dict[str, str] = {}
    seen_new: set[str] = set()
    for ep in episodes:
        sid = ep["stable_id"]
        row = existing.get(sid)
        if row is None:
            if ep["content_hash"] in taken or ep["content_hash"] in seen_new:
                actions[sid] = "duplicate"
            else:
                actions[sid] = "insert"
                seen_new.add(ep["content_hash"])
            continue
        if row["kind"] != "episode":
            actions[sid] = "conflict"
            continue
        if row["scope"] is not None and row["scope"] not in profiles:
            actions[sid] = "forbidden"
            continue
        if row["content_hash"] == ep["content_hash"]:
            current = (
                row["has_vec"]
                and row["embed_space"] == space
                and row["embedded_hash"] == row["content_hash"]
            )
            actions[sid] = "unchanged" if current or not space else "revector"
            continue
        if row["ts"] is not None and ep["ts_dt"] is not None and ep["ts_dt"] < row["ts"]:
            actions[sid] = "stale"
            continue
        attrs = row["attrs"] or {}
        stored_mtime = _float_or_none(attrs.get("source_mtime")) if isinstance(attrs, dict) else None
        if (
            stored_mtime is not None
            and ep["source_mtime"] is not None
            and ep["source_mtime"] < stored_mtime
        ):
            actions[sid] = "stale"
            continue
        actions[sid] = "update"
    return actions, existing


def _attrs(ep: dict, identity: DeviceIdentity) -> str:
    attrs = {"is_sidechain": bool(ep["is_sidechain"]), "source": "push", "device": identity.device}
    if not ep["git_export"]:
        attrs["git_export"] = False
    if ep["source_mtime"] is not None:
        attrs["source_mtime"] = ep["source_mtime"]
    return json.dumps(attrs)


_VECTOR_ACTIONS = frozenset({"insert", "update", "revector"})


class _VectorsNeeded(Exception):
    """The locked re-plan needs vectors the caller has not computed yet.

    Raised before any write. The caller rolls the transaction back, which
    drops the advisory lock and returns the pooled connection, and only
    then calls the embedder.
    """

    def __init__(self, episodes: list[dict]):
        super().__init__(f"{len(episodes)} episode(s) still need embeddings")
        self.episodes = episodes


def _queue_export(conn, doc_id: int, relpath: str) -> None:
    """One pending outbox row per document; the exporter renders the current row."""
    conn.execute(
        """
        insert into brain.git_exports (document_id, relpath)
        select %s, %s
         where not exists (
           select 1 from brain.git_exports
            where document_id = %s and exported_at is null and error is null
         )
        """,
        (doc_id, relpath, doc_id),
    )


def upsert_episodes(
    conn,
    identity: DeviceIdentity,
    episodes: list[dict],
    *,
    vectors: dict[str, list] | None = None,
    space: str | None = None,
    embed: bool = True,
) -> dict:
    """Write ``episodes`` in one transaction. The caller commits.

    Vectors are computed by the caller outside this transaction, so the
    advisory lock is not held across the embedder. When the re-plan still
    needs a vector that was not supplied, raise :class:`_VectorsNeeded`
    before any write. ``embed=False`` stores null vectors, matching a
    provider that is off or unavailable. If the embedder fails, the caller
    raises :class:`EmbedUnavailable` and this transaction holds no writes.
    """
    from .pgstore import as_vector

    counts = {k: 0 for k in ("inserted", "updated", "revectored", "unchanged", "duplicate", "stale", "conflict", "forbidden")}
    if not episodes:
        return {**counts, "results": {}}
    # Transaction-scoped: Supavisor releases it at commit or rollback.
    conn.execute("select pg_advisory_xact_lock(hashtext('brain-ingest'))")
    provided = vectors or {}
    if embed and embed_provider() != "off":
        space_now = space or embed_space()
    else:
        space_now = ""
    actions, _existing = _plan(conn, episodes, space_now, identity.profiles)
    todo = [e for e in episodes if actions[e["stable_id"]] in _VECTOR_ACTIONS]
    if space_now:
        missing = [e for e in todo if e["stable_id"] not in provided]
        if missing:
            raise _VectorsNeeded(missing)
    space = space_now or None
    vec_by_id = provided if space is not None else {}
    results: dict[str, str] = {}
    for ep in episodes:
        sid = ep["stable_id"]
        action = actions[sid]
        vec = vec_by_id.get(sid)
        embedded_hash = ep["content_hash"] if vec is not None else None
        if action == "insert":
            row = conn.execute(
                """
                insert into brain.documents (
                  stable_id, kind, scope, project_id, relpath, body,
                  content_hash, embedded_hash, embedding, embed_space,
                  ts, day, cwd, session_id, uuid, git_branch, host,
                  status, recall_enabled, attrs
                ) values (
                  %s, 'episode', %s, %s, %s, %s,
                  %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s,
                  'active', true, %s::jsonb
                )
                on conflict (stable_id) do nothing
                returning id
                """,
                (
                    sid,
                    ep["scope"],
                    ep["project_id"],
                    ep["relpath"],
                    ep["text"],
                    ep["content_hash"],
                    embedded_hash,
                    as_vector(vec),
                    space if vec is not None else None,
                    ep["ts_dt"],
                    ep["ts_dt"].date() if ep["ts_dt"] else None,
                    ep["cwd"],
                    ep["session_id"],
                    ep["uuid"],
                    ep["git_branch"],
                    identity.device,
                    _attrs(ep, identity),
                ),
            ).fetchone()
            if row is None:
                action = "unchanged"  # a concurrent request inserted it first
            elif ep["git_export"]:
                _queue_export(conn, int(row["id"]), ep["relpath"])
        elif action == "update":
            row = conn.execute(
                """
                update brain.documents
                   set body = %s,
                       content_hash = %s,
                       embedded_hash = %s,
                       embedding = %s,
                       embed_space = %s,
                       ts = %s,
                       day = %s,
                       cwd = %s,
                       git_branch = %s,
                       project_id = coalesce(%s, project_id),
                       host = %s,
                       attrs = attrs || %s::jsonb
                 where stable_id = %s
                   and kind = 'episode'
                   and content_hash <> %s
                   and (scope is null or scope = any(%s))
                returning id, relpath
                """,
                (
                    ep["text"],
                    ep["content_hash"],
                    embedded_hash,
                    as_vector(vec),
                    space if vec is not None else None,
                    ep["ts_dt"],
                    ep["ts_dt"].date() if ep["ts_dt"] else None,
                    ep["cwd"],
                    ep["git_branch"],
                    ep["project_id"],
                    identity.device,
                    _attrs(ep, identity),
                    sid,
                    ep["content_hash"],
                    list(identity.profiles),
                ),
            ).fetchone()
            if row is None:
                action = "unchanged"
            elif ep["git_export"]:
                _queue_export(conn, int(row["id"]), row["relpath"] or ep["relpath"])
        elif action == "revector":
            if vec is None:
                action = "unchanged"
            else:
                conn.execute(
                    """
                    update brain.documents
                       set embedding = %s, embedded_hash = content_hash, embed_space = %s
                     where stable_id = %s and content_hash = %s
                    """,
                    (as_vector(vec), space, sid, ep["content_hash"]),
                )
        key = {"insert": "inserted", "update": "updated", "revector": "revectored"}.get(action, action)
        counts[key] += 1
        results[sid] = key
    counts["vec_pending"] = sum(
        1 for e in todo if e["stable_id"] not in vec_by_id and results.get(e["stable_id"]) in ("inserted", "updated")
    )
    return {**counts, "results": results}


def _embed_for(episodes: list[dict]) -> tuple[dict[str, list], str | None]:
    """Vectors keyed by stable id. ``({}, None)`` when embedding is off.

    Callers run this with no pooled connection checked out. The OpenAI
    call is the slow part of ingest, and the pool only has a few slots.
    """
    vecs, got = _embed_episodes([ep["text"] for ep in episodes])
    if vecs is None:
        return {}, None
    return {ep["stable_id"]: vec for ep, vec in zip(episodes, vecs)}, got


def ingest(identity: DeviceIdentity, payload) -> dict:
    """Validate, embed, and store one batch. Commits on success.

    The embedder runs between transactions. Each attempt locks, re-plans,
    and either writes or bails out so the connection can be returned
    before the next embed.
    """
    from psycopg.rows import dict_row

    from .pgstore import connection

    episodes, rejected = parse_payload(payload, identity)
    vectors: dict[str, list] = {}
    space: str | None = None
    embed = embed_provider() != "off"
    out: dict | None = None
    # One round discovers every row that still needs a vector; further
    # rounds cover a re-plan that finds more. The successful write is the
    # extra iteration, so a batch cannot spin.
    for _ in range(len(episodes) + 1):
        try:
            with connection() as conn:
                conn.row_factory = dict_row
                try:
                    out = upsert_episodes(
                        conn,
                        identity,
                        episodes,
                        vectors=vectors,
                        space=space,
                        embed=embed,
                    )
                except Exception:
                    conn.rollback()
                    raise
                else:
                    conn.commit()
        except _VectorsNeeded as need:
            found, got_space = _embed_for(need.episodes)
            if got_space is None:
                # Off, or the local model is unavailable. Store null vectors.
                embed = False
                vectors = {}
                space = None
            else:
                vectors.update(found)
                space = got_space
            continue
        break
    else:
        raise EmbedUnavailable("ingest could not settle embeddings")
    assert out is not None
    out["rejected"] = rejected
    out["accepted"] = len(episodes)
    return out


# ----------------------------------------------------------------- route ---

async def ingest_episodes_route(request):
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse

    from .device_api import authed_json
    from .device_tokens import SCOPE_INGEST

    identity, payload, error = await authed_json(request, SCOPE_INGEST, MAX_BODY_BYTES)
    if error is not None:
        return error
    try:
        out = await run_in_threadpool(ingest, identity, payload)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except EmbedUnavailable:
        logger.warning("ingest embed failed; batch not written (device %s)", identity.device)
        return JSONResponse({"error": "embedding unavailable"}, status_code=503, headers={"Retry-After": "60"})
    except Exception as exc:
        logger.warning("ingest write failed: %s (device %s)", type(exc).__name__, identity.device)
        return JSONResponse({"error": "store unavailable"}, status_code=503, headers={"Retry-After": "30"})
    logger.info(
        "ingest device=%s accepted=%d inserted=%d updated=%d unchanged=%d rejected=%d",
        identity.device,
        out["accepted"],
        out["inserted"],
        out["updated"],
        out["unchanged"],
        len(out["rejected"]),
    )
    return JSONResponse(out)
