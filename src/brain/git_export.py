"""Drain ``brain.git_exports`` into one commit on ``$BRAIN_GIT_REPO``
``main``.

``remember`` inserts the outbox row (``action`` defaults to ``upsert``) and
does not talk to GitHub. This command renders each pending document, creates
one commit through the Git Data API, then stamps ``exported_sha``. An empty
outbox does not call GitHub.

Idempotent on retry. Blob ids are standard git SHAs, so a row that was
committed and not stamped compares equal and is stamped with the current
HEAD instead of being committed again. A non-fast-forward update of ``main``
re-reads the branch and retries. Paths that cannot be rendered are stamped
``error`` so they are not retried forever. A GitHub or network failure leaves
the rows pending.

``brain.meta.ingested_git_head`` is the commit ``brain-pg-load`` imported.
An export commit is that tree plus the outbox files, not a dump of the
database, so this command does not write that key. It records
``last_git_export_sha`` and ``last_git_export_at`` instead.

``--resync`` compares every renderable document against ``main`` under the
managed prefixes (``memories/``, ``summaries/``, ``proposals/``,
``handoffs/``, ``episodes/``). Default is a dry run. ``--apply`` writes
adds and updates in one commit; ``--apply --delete`` also removes paths
that have no rendering document. Unmanaged files are never touched.
Rows with ``attrs.git_export`` false, and episodes whose cwd matches
``BRAIN_EPISODE_EXCLUDE``, are counted as ``excluded`` (no paths printed)
and are never added or updated. An existing git file for one is removed
only with ``--delete``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .backend import database_url
from .git_render import check_relpath, render_document
from .ingest import _episode_excluded

_REPO = ""  # owner/name of the store repo; set BRAIN_GIT_REPO
_BRANCH = "main"
_LOCK = "git-export"
_LOCK_TTL_S = 1800
_ATTEMPTS = 5
_API = "https://api.github.com"
_MANAGED_PREFIXES = (
    "memories/",
    "summaries/",
    "proposals/",
    "handoffs/",
    "episodes/",
)
_MANAGED_ROOTS = tuple(p.rstrip("/") for p in _MANAGED_PREFIXES)
_RESYNC_EXAMPLE_LIMIT = 50
_EXPORT_KINDS = ("episode", "memory", "summary", "proposal", "handoff")
# GitHub secondary rate limit on content-creating requests is ~80/min.
_BLOB_MIN_INTERVAL_S = 1.0
_RATE_LIMIT_RETRIES = 5
# ...and ~500/hour, so text files ride inline in chained create_tree calls
# (one request per batch) instead of one create_blob each.
_TREE_BATCH_FILES = 100
_TREE_BATCH_BYTES = 1_000_000


class GitHubError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"GitHub {status} {path}: {body[:300]}")
        self.status = status


class NonFastForward(GitHubError):
    """``main`` moved between our read and the ref update."""


def _retry_after_seconds(exc: urllib.error.HTTPError, attempt: int) -> float:
    """Seconds to wait after a 403/429. Prefer ``Retry-After``, else 2**attempt."""
    raw = None
    if exc.headers is not None:
        raw = exc.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(2**attempt)


class GitHubClient:
    """Git Data API. ``opener`` is injectable so tests never touch the network."""

    def __init__(self, token: str, repo: str, branch: str, opener=None):
        self.token = token
        self.repo = repo
        self.branch = branch
        self.opener = opener or urllib.request.build_opener()
        self._last_blob_at = 0.0

    @classmethod
    def from_env(cls) -> GitHubClient:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise RuntimeError("GITHUB_TOKEN is required to export")
        repo = os.environ.get("BRAIN_GIT_REPO", _REPO).strip() or _REPO
        if not repo:
            raise RuntimeError("BRAIN_GIT_REPO (owner/name) is required to export")
        branch = os.environ.get("BRAIN_GIT_BRANCH", _BRANCH).strip() or _BRANCH
        return cls(token, repo, branch)

    def head_sha(self) -> str:
        payload = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        return payload["object"]["sha"]

    def commit_tree(self, sha: str) -> str:
        payload = self._request("GET", f"/repos/{self.repo}/git/commits/{sha}")
        return payload["tree"]["sha"]

    def tree_files(self, tree_sha: str) -> dict[str, str]:
        payload = self._request(
            "GET", f"/repos/{self.repo}/git/trees/{tree_sha}?recursive=1"
        )
        if payload.get("truncated"):
            raise GitHubError(200, "tree listing truncated", "git/trees")
        files = {}
        for entry in payload.get("tree") or []:
            if entry.get("type") == "blob" and entry.get("path"):
                files[entry["path"]] = entry["sha"]
        return files

    def create_blob(self, data: bytes) -> str:
        self._pace_blob()
        payload = self._request(
            "POST",
            f"/repos/{self.repo}/git/blobs",
            {
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            },
        )
        return payload["sha"]

    def create_tree(self, base_tree: str, entries: list[dict]) -> str:
        payload = self._request(
            "POST",
            f"/repos/{self.repo}/git/trees",
            {"base_tree": base_tree, "tree": entries},
        )
        return payload["sha"]

    def create_commit(self, message: str, tree: str, parent: str) -> str:
        payload = self._request(
            "POST",
            f"/repos/{self.repo}/git/commits",
            {"message": message, "tree": tree, "parents": [parent]},
        )
        return payload["sha"]

    def update_ref(self, sha: str) -> None:
        self._request(
            "PATCH",
            f"/repos/{self.repo}/git/refs/heads/{self.branch}",
            {"sha": sha, "force": False},
        )

    def _pace_blob(self) -> None:
        now = time.monotonic()
        wait = self._last_blob_at + _BLOB_MIN_INTERVAL_S - now
        if wait > 0:
            time.sleep(wait)
        self._last_blob_at = time.monotonic()

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        last_rate_detail = ""
        last_rate_code = 429
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            req = urllib.request.Request(_API + path, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("User-Agent", "brain-git-export")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with self.opener.open(req, timeout=60) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")
                if exc.code in (403, 429) and attempt < _RATE_LIMIT_RETRIES:
                    last_rate_detail = detail
                    last_rate_code = exc.code
                    time.sleep(_retry_after_seconds(exc, attempt))
                    continue
                if exc.code in (409, 422) and method == "PATCH":
                    raise NonFastForward(exc.code, detail, path) from exc
                raise GitHubError(exc.code, detail, path) from exc
            if not raw:
                return {}
            return json.loads(raw)
        raise GitHubError(last_rate_code, last_rate_detail or "rate limited", path)


def blob_sha(data: bytes) -> str:
    """Git blob id: SHA-1 of ``blob <len>\\0<bytes>``."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data, usedforsecurity=False).hexdigest()


@dataclass
class Change:
    path: str
    data: bytes | None
    ids: list[int] = field(default_factory=list)


@dataclass
class PublishResult:
    sha: str
    added: int
    updated: int
    deleted: int
    committed: bool


def commit_message(added: int, updated: int, deleted: int) -> str:
    total = added + updated + deleted
    noun = "file" if total == 1 else "files"
    return (
        f"brain-git-export: {total} {noun} "
        f"({added} added, {updated} updated, {deleted} deleted)\n\n"
        "Exported queued documents from the hosted brain."
    )


def publish(github, changes: list[Change], message_fn=commit_message) -> PublishResult:
    """One commit for every path that actually differs. Retries a moved ``main``."""
    last: NonFastForward | None = None
    for _attempt in range(_ATTEMPTS):
        head = github.head_sha()
        tree = github.commit_tree(head)
        current = github.tree_files(tree)
        entries = []
        expected: dict[str, str] = {}
        added = updated = deleted = 0
        for change in changes:
            have = current.get(change.path)
            if change.data is None:
                if have is None:
                    continue
                entries.append(
                    {"path": change.path, "mode": "100644", "type": "blob", "sha": None}
                )
                deleted += 1
                continue
            sha = blob_sha(change.data)
            if have == sha:
                continue
            entry = {"path": change.path, "mode": "100644", "type": "blob"}
            try:
                entry["content"] = change.data.decode("utf-8")
                expected[change.path] = sha
            except UnicodeDecodeError:
                made = github.create_blob(change.data)
                if made != sha:
                    raise GitHubError(200, "blob sha mismatch", "git/blobs")
                entry["sha"] = sha
            entries.append(entry)
            if have is None:
                added += 1
            else:
                updated += 1
        if not entries:
            return PublishResult(head, 0, 0, 0, committed=False)
        new_tree = tree
        for batch in _tree_batches(entries):
            new_tree = github.create_tree(new_tree, batch)
        if expected:
            landed = github.tree_files(new_tree)
            if any(landed.get(path) != sha for path, sha in expected.items()):
                raise GitHubError(200, "tree content sha mismatch", "git/trees")
        message = message_fn(added, updated, deleted)
        commit = github.create_commit(message, new_tree, head)
        try:
            github.update_ref(commit)
        except NonFastForward as exc:
            last = exc
            continue
        return PublishResult(commit, added, updated, deleted, committed=True)
    raise NonFastForward(last.status if last else 422, "main kept moving", "refs")


def _tree_batches(entries: list[dict]):
    batch: list[dict] = []
    size = 0
    for entry in entries:
        weight = len(entry.get("content") or "")
        if batch and (len(batch) >= _TREE_BATCH_FILES or size + weight > _TREE_BATCH_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(entry)
        size += weight
    if batch:
        yield batch


def export_once(github=None) -> int:
    """Drain the outbox. 0 when done or empty, 2 when another run holds the lock."""
    import psycopg

    url = database_url()
    with psycopg.connect(url, prepare_threshold=None) as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        pending = _load_pending(conn)
        if not pending:
            print("brain-git-export: nothing queued")
            return 0
        if github is None:
            github = GitHubClient.from_env()
        owner = f"{socket.gethostname()}:{os.getpid()}"
        if not _claim(conn, owner):
            print("brain-git-export: lock held", file=sys.stderr)
            return 2
        try:
            return _drain(conn, github, pending)
        finally:
            _release(conn, owner)


def _load_pending(conn) -> list[dict]:
    with conn.transaction():
        _search_path(conn)
        rows = conn.execute(
            """
            select e.id, e.document_id, e.relpath, e.action,
                   d.kind, d.scope, d.project_id, d.relpath as doc_relpath,
                   d.title, d.description, d.body as doc_body, d.content_hash,
                   d.ts, d.cwd, d.session_id, d.uuid, d.git_branch, d.host,
                   d.status, d.recall_enabled, d.expires_at, d.verified_at,
                   d.attrs, d.stable_id
              from brain.git_exports e
              left join brain.documents d on d.id = e.document_id
             where e.exported_at is null and e.error is null
             order by e.id
            """
        ).fetchall()
    return list(rows)


def _drain(conn, github, pending: list[dict]) -> int:
    changes, errors = _plan(pending)
    if errors:
        _stamp_errors(conn, errors)
        for export_id, message in errors:
            print(f"brain-git-export: row {export_id}: {message}", file=sys.stderr)
    if not changes:
        print("brain-git-export: nothing to commit")
        return 0
    # GitHub is outside the transaction. A failure here leaves rows pending.
    result = publish(github, changes)
    ids = [export_id for change in changes for export_id in change.ids]
    _stamp_exported(conn, ids, result.sha)
    if result.committed:
        print(
            f"brain-git-export: {result.sha} "
            f"({result.added} added, {result.updated} updated, {result.deleted} deleted)"
        )
    else:
        print(f"brain-git-export: unchanged, stamped {result.sha}")
    return 0


def _plan(pending: list[dict]) -> tuple[list[Change], list[tuple[int, str]]]:
    """Latest pending row wins per path. Earlier ids are stamped with it."""
    by_path: dict[str, Change] = {}
    order: list[str] = []
    errors: list[tuple[int, str]] = []
    for row in pending:
        delete, path, problem = _row_intent(row)
        if problem:
            errors.append((row["id"], problem))
            continue
        assert path is not None
        data = None if delete else _render_row(row, path)
        if data is None and not delete:
            errors.append((row["id"], "empty render"))
            continue
        if isinstance(data, str):
            errors.append((row["id"], data))
            continue
        _put(by_path, order, path, data, row["id"])
        old = row.get("relpath")
        if old and old != path and not delete and check_relpath(old) is None:
            _put(by_path, order, old, None, row["id"])
    return [by_path[path] for path in order], errors


def _put(by_path, order, path: str, data: bytes | None, export_id: int) -> None:
    change = by_path.get(path)
    if change is None:
        by_path[path] = Change(path=path, data=data, ids=[export_id])
        order.append(path)
        return
    change.data = data
    if export_id not in change.ids:
        change.ids.append(export_id)


def _row_intent(row: dict) -> tuple[bool, str | None, str | None]:
    """Return ``(delete, path, error)``."""
    action = row.get("action") or "upsert"
    missing = row.get("document_id") is not None and row.get("kind") is None
    path = row.get("doc_relpath") or row.get("relpath")
    if missing:
        if action == "delete" and row.get("relpath"):
            path = row["relpath"]
        else:
            return False, None, "document missing"
    if not path:
        return False, None, "missing relpath"
    problem = check_relpath(path)
    if problem:
        return False, None, problem
    delete = action == "delete" or row.get("status") == "forgotten" or missing
    return delete, path, None


def _render_row(row: dict, path: str):
    """Bytes, or an error string."""
    doc = {
        "action": "upsert",
        "kind": row.get("kind"),
        "relpath": path,
        "title": row.get("title"),
        "description": row.get("description"),
        "body": row.get("doc_body"),
        "content_hash": row.get("content_hash"),
        "scope": row.get("scope"),
        "project_id": row.get("project_id"),
        "ts": row.get("ts"),
        "cwd": row.get("cwd"),
        "session_id": row.get("session_id"),
        "uuid": row.get("uuid"),
        "git_branch": row.get("git_branch"),
        "host": row.get("host"),
        "status": row.get("status"),
        "recall_enabled": row.get("recall_enabled"),
        "expires_at": row.get("expires_at"),
        "verified_at": row.get("verified_at"),
        "attrs": row.get("attrs"),
        "stable_id": row.get("stable_id"),
    }
    try:
        rendered = render_document(doc)
    except ValueError as exc:
        return str(exc)
    if rendered is None:
        return None
    return rendered


def _stamp_errors(conn, errors: list[tuple[int, str]]) -> None:
    with conn.transaction():
        _search_path(conn)
        for export_id, message in errors:
            conn.execute(
                """
                update brain.git_exports
                   set error = %s
                 where id = %s and exported_at is null
                """,
                (message, export_id),
            )


def _stamp_exported(conn, ids: list[int], sha: str) -> None:
    from datetime import datetime, timezone

    stamped_at = datetime.now(timezone.utc).isoformat()
    with conn.transaction():
        _search_path(conn)
        conn.execute(
            """
            update brain.git_exports
               set exported_sha = %s, exported_at = now(), error = null
             where id = any(%s::bigint[]) and exported_at is null
            """,
            (sha, ids),
        )
        conn.execute(
            """
            insert into brain.meta (key, value, updated_at)
            values ('last_git_export_sha', %s, now())
            on conflict (key) do update
              set value = excluded.value, updated_at = now()
            """,
            (sha,),
        )
        conn.execute(
            """
            insert into brain.meta (key, value, updated_at)
            values ('last_git_export_at', %s, now())
            on conflict (key) do update
              set value = excluded.value, updated_at = now()
            """,
            (stamped_at,),
        )


def _claim(conn, owner: str) -> bool:
    with conn.transaction():
        _search_path(conn)
        row = conn.execute(
            """
            insert into brain.job_locks (name, owner, expires_at)
            values (%s, %s, now() + make_interval(secs => %s))
            on conflict (name) do update
              set owner = excluded.owner,
                  expires_at = excluded.expires_at,
                  updated_at = now()
            where job_locks.expires_at <= now()
               or job_locks.owner = excluded.owner
            returning name
            """,
            (_LOCK, owner, _LOCK_TTL_S),
        ).fetchone()
    return row is not None


def _release(conn, owner: str) -> None:
    with conn.transaction():
        _search_path(conn)
        conn.execute(
            "delete from brain.job_locks where name = %s and owner = %s",
            (_LOCK, owner),
        )


def _search_path(conn) -> None:
    conn.execute(
        "select set_config('search_path', 'brain, public, extensions', true)"
    )


@dataclass
class ResyncPlan:
    """Desired git changes from a full Supabase → git comparison."""

    add: list[Change] = field(default_factory=list)
    update: list[Change] = field(default_factory=list)
    delete: list[Change] = field(default_factory=list)
    unmanaged: list[str] = field(default_factory=list)
    error: list[tuple[str, str]] = field(default_factory=list)
    # Paths only. Dry-run output prints the count, never these paths.
    excluded: list[str] = field(default_factory=list)
    head: str = ""


def _under_managed(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _MANAGED_PREFIXES)


def _root_counts(paths: list[str]) -> str:
    """Compact ``memories=N summaries=M …`` for non-zero managed roots."""
    counts = {root: 0 for root in _MANAGED_ROOTS}
    for path in paths:
        root = path.split("/", 1)[0]
        if root in counts:
            counts[root] += 1
    parts = [f"{root}={counts[root]}" for root in _MANAGED_ROOTS if counts[root]]
    return " ".join(parts) if parts else "none"


def _load_export_documents(conn) -> list[dict]:
    with conn.transaction():
        _search_path(conn)
        rows = conn.execute(
            """
            select id, kind, scope, project_id, relpath, title, description,
                   body as doc_body, content_hash, ts, cwd, session_id, uuid,
                   git_branch, host, status, recall_enabled, expires_at,
                   verified_at, attrs, stable_id
              from brain.documents
             where kind = any(%s::text[])
               and relpath is not null
             order by id
            """,
            (list(_EXPORT_KINDS),),
        ).fetchall()
    return list(rows)


def _export_excluded(row: dict) -> bool:
    """True when this document must not be written to git.

    ``attrs.git_export is False`` is the stored decision (the client flag,
    or the server exclude list at ingest). Episodes whose cwd matches
    ``BRAIN_EPISODE_EXCLUDE`` are skipped even when that attr was never set.
    """
    attrs = row.get("attrs")
    if isinstance(attrs, dict) and attrs.get("git_export") is False:
        return True
    if row.get("kind") == "episode" and _episode_excluded(row.get("cwd")):
        return True
    return False


def _desired_renders(
    rows: list[dict],
) -> tuple[dict[str, bytes], list[tuple[str, str]], list[str]]:
    """Path → bytes for documents that should exist; errors and exclusions.

    Forgotten / delete renders (``None``) are omitted so their git files can
    land in ``delete``. A render failure (error string) is listed in
    ``errors`` and must not become a deletion. Excluded rows are listed by
    path for counting only: they are not rendered, so they cannot land in
    ``add`` or ``update``. An existing git file for one still falls through
    to ``delete``.
    """
    desired: dict[str, bytes] = {}
    errors: list[tuple[str, str]] = []
    excluded: list[str] = []
    for row in rows:
        path = row.get("relpath")
        if not path or check_relpath(path) is not None:
            continue
        if _export_excluded(row):
            excluded.append(path)
            continue
        data = _render_row(row, path)
        if isinstance(data, str):
            errors.append((path, data))
            continue
        if data is None:
            continue
        desired[path] = data
    return desired, errors, excluded


def plan_resync(github, rows: list[dict]) -> ResyncPlan:
    """Compare rendered documents against the managed tree on ``main``."""
    head = github.head_sha()
    tree = github.commit_tree(head)
    current = github.tree_files(tree)
    desired, errors, excluded = _desired_renders(rows)
    error_paths = {path for path, _msg in errors}

    managed_on_git = {
        path: sha
        for path, sha in current.items()
        if _under_managed(path)
    }

    plan = ResyncPlan(head=head, error=list(errors), excluded=list(excluded))
    for path, data in sorted(desired.items()):
        have = managed_on_git.get(path)
        sha = blob_sha(data)
        change = Change(path=path, data=data)
        if have is None:
            plan.add.append(change)
        elif have != sha:
            plan.update.append(change)

    for path in sorted(managed_on_git):
        if path in desired or path in error_paths:
            continue
        if check_relpath(path) is None:
            plan.delete.append(Change(path=path, data=None))
        else:
            plan.unmanaged.append(path)
    return plan


def _print_resync_plan(plan: ResyncPlan, *, apply: bool, delete: bool) -> None:
    mode = "apply" if apply else "dry-run"
    if apply and delete:
        mode = "apply+delete"
    add_paths = [c.path for c in plan.add]
    update_paths = [c.path for c in plan.update]
    delete_paths = [c.path for c in plan.delete]
    unmanaged_paths = list(plan.unmanaged)
    error_paths = [path for path, _msg in plan.error]
    # Counts only. Paths can name the excluded project, so they are never
    # printed — including when the same path also sits in ``delete``.
    hidden = set(plan.excluded)
    print(
        f"brain-git-export --resync ({mode}): "
        f"{len(add_paths)} add [{_root_counts(add_paths)}], "
        f"{len(update_paths)} update [{_root_counts(update_paths)}], "
        f"{len(delete_paths)} delete [{_root_counts(delete_paths)}], "
        f"{len(unmanaged_paths)} unmanaged [{_root_counts(unmanaged_paths)}], "
        f"{len(error_paths)} error [{_root_counts(error_paths)}], "
        f"{len(plan.excluded)} excluded [{_root_counts(plan.excluded)}] "
        f"(head {plan.head[:12]})"
    )
    for label, paths in (
        ("add", [path for path in add_paths if path not in hidden]),
        ("update", [path for path in update_paths if path not in hidden]),
        ("delete", [path for path in delete_paths if path not in hidden]),
        ("unmanaged", [path for path in unmanaged_paths if path not in hidden]),
        ("error", [f"{path}: {msg}" for path, msg in plan.error if path not in hidden]),
    ):
        if not paths:
            continue
        shown = paths[:_RESYNC_EXAMPLE_LIMIT]
        print(f"  {label} ({len(paths)}):")
        for path in shown:
            print(f"    {path}")
        if len(paths) > _RESYNC_EXAMPLE_LIMIT:
            print(f"    ... and {len(paths) - _RESYNC_EXAMPLE_LIMIT} more")


def _stamp_resync(conn, sha: str) -> None:
    from datetime import datetime, timezone

    stamped_at = datetime.now(timezone.utc).isoformat()
    with conn.transaction():
        _search_path(conn)
        conn.execute(
            """
            insert into brain.meta (key, value, updated_at)
            values ('last_git_resync_sha', %s, now())
            on conflict (key) do update
              set value = excluded.value, updated_at = now()
            """,
            (sha,),
        )
        conn.execute(
            """
            insert into brain.meta (key, value, updated_at)
            values ('last_git_resync_at', %s, now())
            on conflict (key) do update
              set value = excluded.value, updated_at = now()
            """,
            (stamped_at,),
        )


def resync_commit_message(added: int, updated: int, deleted: int) -> str:
    total = added + updated + deleted
    noun = "file" if total == 1 else "files"
    return (
        f"brain-git-export --resync: {total} {noun} "
        f"({added} added, {updated} updated, {deleted} deleted)\n\n"
        "Resynced managed git paths from the hosted brain (Supabase wins)."
    )


def resync_once(
    github=None,
    *,
    apply: bool = False,
    delete: bool = False,
) -> int:
    """Compare Supabase documents to ``main``. 0 ok, 2 when the lock is held."""
    import psycopg

    url = database_url()
    with psycopg.connect(url, prepare_threshold=None) as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        if github is None:
            github = GitHubClient.from_env()
        owner = f"{socket.gethostname()}:{os.getpid()}"
        if not _claim(conn, owner):
            print("brain-git-export: lock held", file=sys.stderr)
            return 2
        try:
            rows = _load_export_documents(conn)
            plan = plan_resync(github, rows)
            _print_resync_plan(plan, apply=apply, delete=delete)
            if not apply:
                return 0
            changes = list(plan.add) + list(plan.update)
            if delete:
                changes.extend(plan.delete)
            if not changes:
                _stamp_resync(conn, plan.head)
                print(f"brain-git-export --resync: nothing to commit, stamped {plan.head}")
                return 0
            result = publish(github, changes, message_fn=resync_commit_message)
            _stamp_resync(conn, result.sha)
            if result.committed:
                print(
                    f"brain-git-export --resync: {result.sha} "
                    f"({result.added} added, {result.updated} updated, "
                    f"{result.deleted} deleted)"
                )
            else:
                print(f"brain-git-export --resync: unchanged, stamped {result.sha}")
            return 0
        finally:
            _release(conn, owner)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain-git-export")
    parser.add_argument(
        "--resync",
        action="store_true",
        help="compare every renderable document to main (dry run unless --apply)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="with --resync, write add+update in one commit",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="with --resync --apply, also remove the delete bucket",
    )
    args = parser.parse_args(argv)
    if args.apply and not args.resync:
        print("brain-git-export: --apply requires --resync", file=sys.stderr)
        return 1
    if args.delete and not (args.resync and args.apply):
        print(
            "brain-git-export: --delete requires --resync --apply",
            file=sys.stderr,
        )
        return 1
    try:
        if args.resync:
            return resync_once(apply=args.apply, delete=args.delete)
        return export_once()
    except Exception as exc:
        print(f"brain-git-export: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
