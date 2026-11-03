"""Git export: loader round-trip, empty outbox, and idempotent drain."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from brain import db
from brain import git_export
from brain.git_export import (
    Change,
    GitHubClient,
    NonFastForward,
    blob_sha,
    export_once,
    publish,
    resync_once,
)
from brain.git_render import render_document
from brain.pg_loader import build_doc, summary_content_hash
from brain.remember import format_memory_md


def _loaded(doc):
    return {
        "kind": doc.kind,
        "relpath": doc.relpath,
        "title": doc.title,
        "description": doc.description,
        "body": doc.body,
        "content_hash": doc.content_hash,
        "scope": doc.scope,
        "project_id": doc.project_id,
        "ts": doc.ts,
        "cwd": doc.cwd,
        "session_id": doc.session_id,
        "uuid": doc.uuid,
        "git_branch": doc.git_branch,
        "host": doc.host,
        "status": doc.status,
        "recall_enabled": doc.recall_enabled,
        "expires_at": doc.expires_at,
        "verified_at": doc.verified_at,
        "attrs": doc.attrs,
        "stable_id": doc.stable_id,
        "action": "upsert",
    }


def _round_trip(tmp_path, doc):
    first = render_document(doc)
    assert first is not None
    loaded = build_doc(tmp_path, doc["relpath"], {}, "", raw=first, commit_ts="")
    assert loaded is not None
    second = render_document(_loaded(loaded))
    assert second == first
    return loaded


def test_memory_matches_remember_format_and_round_trips(tmp_path):
    description = 'alpha "widget"'
    fact = "The alpha widget lives in project-alpha."
    digest = db.memory_content_hash("widget", description, "reference", fact, "", "work")
    doc = {
        "kind": "memory",
        "relpath": "memories/widget.md",
        "title": "widget",
        "description": description,
        "body": fact,
        "content_hash": digest,
        "scope": "work",
        "project_id": "project-alpha",
        "status": "active",
        "recall_enabled": False,
        "expires_at": "2026-12-01",
        "verified_at": "2026-10-19T15:04:05+00:00",
        "ts": datetime(2026, 10, 19, 15, 4, 5, tzinfo=timezone.utc),
        "attrs": {"type": "reference"},
        "action": "upsert",
    }
    raw = render_document(doc)
    assert raw is not None
    text = raw.decode()
    # remember hashes an empty date. The row timestamp must not become `date:`.
    assert "\ndate:" not in text
    assert "project_id: project-alpha\n" in text
    assert "recall_enabled: false\n" in text
    assert "expires_at: 2026-12-01T00:00:00.000000+00:00\n" in text
    assert "verified_at: 2026-10-19T15:04:05.000000+00:00\n" in text
    assert 'description: "alpha \\"widget\\""\n' in text
    loaded = _round_trip(tmp_path, doc)
    assert loaded.content_hash == digest
    assert loaded.project_id == "project-alpha"
    assert loaded.recall_enabled is False
    assert loaded.scope == "work"


def test_plain_memory_matches_format_memory_md():
    fact = "A shared fact."
    doc = {
        "kind": "memory",
        "relpath": "memories/shared.md",
        "title": "shared",
        "description": "a shared fact.",
        "body": fact,
        "content_hash": db.memory_content_hash(
            "shared", "a shared fact.", "reference", fact, "", None
        ),
        "status": "active",
        "recall_enabled": True,
        "attrs": {"type": "reference"},
    }
    assert render_document(doc).decode() == format_memory_md(
        "shared", "a shared fact.", "reference", fact, None
    )


def test_dated_memory_keeps_the_hash(tmp_path):
    fact = "Dated fact."
    date = "2026-10-19"
    digest = db.memory_content_hash("dated", "dated fact.", "project", fact, date, None)
    doc = {
        "kind": "memory",
        "relpath": "memories/dated.md",
        "title": "dated",
        "description": "dated fact.",
        "body": fact,
        "content_hash": digest,
        "status": "active",
        "recall_enabled": True,
        "ts": date,
        "attrs": {"type": "project"},
    }
    loaded = _round_trip(tmp_path, doc)
    assert b"date: 2026-10-19\n" in render_document(doc)
    assert loaded.content_hash == digest


def test_archived_memory_round_trips_with_status_line(tmp_path):
    fact = "Retired fact."
    doc = {
        "kind": "memory",
        "relpath": "memories/archive/old.md",
        "title": "old",
        "description": "retired",
        "body": fact,
        "content_hash": db.memory_content_hash("old", "retired", "project", fact, "", None),
        "status": "active",
        "recall_enabled": True,
        "attrs": {"type": "project"},
    }
    raw = render_document(doc)
    assert b"status: archived\n" in raw
    loaded = _round_trip(tmp_path, doc)
    assert loaded.status == "archived"


def test_episode_summary_and_loose_round_trip(tmp_path):
    episode = {
        "kind": "episode",
        "relpath": "episodes/2026/10/ep_abc.md",
        "stable_id": "ep_abc",
        "body": "USER: hello\n\nASSISTANT: hi\n",
        "ts": "2026-10-19T01:02:03.004Z",
        "cwd": "/Users/you/src",
        "git_branch": "main",
        "session_id": "sess",
        "uuid": "uuid-1",
        "scope": "personal",
        "attrs": {"is_sidechain": 1},
        "status": "active",
        "recall_enabled": True,
    }
    loaded = _round_trip(tmp_path, episode)
    assert loaded.kind == "episode"
    assert loaded.body == episode["body"]
    assert loaded.scope == "personal"
    assert loaded.attrs["is_sidechain"] is True

    text = "Worked the ticket."
    description = 'App "slow"'
    project = "src"
    cwd = r"C:\Users\you\src"
    host = "devbox"
    session = "0f1e2d3c-4b5a-4968-8776-655443322110"
    date = "2026-10-14"
    summary = {
        "kind": "summary",
        "relpath": f"summaries/2026-10/{session}.md",
        "title": project,
        "description": description,
        "body": text,
        "session_id": session,
        "cwd": cwd,
        "host": host,
        "project_id": project,
        "ts": date,
        "content_hash": summary_content_hash(
            session, date, project, cwd, host, description, text
        ),
        "status": "active",
        "recall_enabled": True,
        "attrs": {},
    }
    loaded = _round_trip(tmp_path, summary)
    assert loaded.content_hash == summary["content_hash"]
    assert loaded.project_id == "src"

    loose_project = "~/src/brain"
    handoff = {
        "kind": "handoff",
        "relpath": "handoffs/2026-10-19-note.md",
        "title": "Handoff: note",
        "description": "ready to implement",
        "body": "# Handoff: note\n\nNext: ship it.",
        "ts": "2026-10-19T09:38:00-05:00",
        "project_id": "brain",
        "attrs": {"project": loose_project},
        "status": "active",
        "recall_enabled": True,
    }
    loaded = _round_trip(tmp_path, handoff)
    assert loaded.title == "Handoff: note"
    assert loaded.body == "# Handoff: note\n\nNext: ship it."
    assert loaded.attrs.get("project") == loose_project

    proposal = {
        "kind": "proposal",
        "relpath": "proposals/move-db.md",
        "title": "Move the store",
        "description": "",
        "body": "# Move the store\n\nUse Supabase as the canonical database.",
        "status": "active",
        "recall_enabled": True,
        "attrs": {},
    }
    loaded = _round_trip(tmp_path, proposal)
    assert loaded.title == "Move the store"
    assert not render_document(proposal).decode().startswith("---")


def test_forgotten_renders_as_delete():
    assert render_document(
        {"kind": "memory", "relpath": "memories/gone.md", "status": "forgotten", "action": "upsert"}
    ) is None
    assert render_document(
        {"kind": "memory", "relpath": "memories/gone.md", "status": "active", "action": "delete"}
    ) is None


class FakeGitHub:
    def __init__(self):
        self.head = "c0"
        self.trees = {"c0": "t0"}
        self.files = {"t0": {}}
        self.commits = 0
        self.updates = 0
        self.nff_remaining = 0
        self.messages: list[str] = []
        self.blobs = 0
        self.tree_calls: list[int] = []

    def head_sha(self):
        return self.head

    def commit_tree(self, sha):
        return self.trees[sha]

    def tree_files(self, tree_sha):
        return dict(self.files[tree_sha])

    def create_blob(self, data: bytes) -> str:
        self.blobs += 1
        return blob_sha(data)

    def create_tree(self, base_tree, entries):
        self.tree_calls.append(len(entries))
        merged = dict(self.files[base_tree])
        for entry in entries:
            if "content" in entry:
                merged[entry["path"]] = blob_sha(entry["content"].encode("utf-8"))
            elif entry["sha"] is None:
                merged.pop(entry["path"], None)
            else:
                merged[entry["path"]] = entry["sha"]
        sha = f"t{len(self.files)}"
        self.files[sha] = merged
        return sha

    def create_commit(self, message, tree, parent):
        self.commits += 1
        sha = f"c{self.commits}"
        self.trees[sha] = tree
        self.messages.append(message)
        return sha

    def update_ref(self, sha):
        self.updates += 1
        if self.nff_remaining:
            self.nff_remaining -= 1
            old_tree = self.trees[self.head]
            moved = f"other{self.updates}"
            extra = dict(self.files[old_tree])
            extra["memories/other.md"] = "deadbeef"
            new_tree = f"t-other{self.updates}"
            self.files[new_tree] = extra
            self.trees[moved] = new_tree
            self.head = moved
            raise NonFastForward(422, "not fast forward", "refs/heads/main")
        self.head = sha


def test_publish_retries_non_fast_forward_and_keeps_the_other_commit():
    hub = FakeGitHub()
    hub.nff_remaining = 1
    data = b"hello\n"
    result = publish(hub, [Change(path="memories/widget.md", data=data, ids=[1])])
    assert result.committed is True
    assert result.sha == hub.head
    assert hub.commits == 2
    assert hub.updates == 2
    tree = hub.files[hub.trees[hub.head]]
    assert tree["memories/widget.md"] == blob_sha(data)
    assert tree["memories/other.md"] == "deadbeef"
    assert result.added == 1
    assert "1 file (1 added, 0 updated, 0 deleted)" in hub.messages[-1]


def test_publish_skips_identical_blobs_and_absent_deletes():
    hub = FakeGitHub()
    data = b"same\n"
    sha = blob_sha(data)
    hub.files["t0"]["memories/widget.md"] = sha
    result = publish(
        hub,
        [
            Change(path="memories/widget.md", data=data, ids=[1]),
            Change(path="memories/missing.md", data=None, ids=[2]),
        ],
    )
    assert result.committed is False
    assert result.sha == "c0"
    assert hub.commits == 0


def _insert_memory(conn, **overrides):
    fields = {
        "stable_id": "mem_widget",
        "kind": "memory",
        "relpath": "memories/widget.md",
        "title": "widget",
        "description": "alpha widget",
        "body": "The fact.",
        "scope": None,
        "status": "active",
        "recall_enabled": True,
        "project_id": None,
        "attrs": {"type": "reference"},
    }
    fields.update(overrides)
    fields["content_hash"] = db.memory_content_hash(
        fields["title"],
        fields["description"],
        (fields["attrs"] or {}).get("type") or "",
        fields["body"],
        "",
        fields["scope"],
    )
    row = conn.execute(
        """
        insert into brain.documents (
          stable_id, kind, relpath, title, description, body, content_hash,
          scope, status, recall_enabled, project_id, attrs
        ) values (
          %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s::jsonb
        ) returning id
        """,
        (
            fields["stable_id"],
            fields["kind"],
            fields["relpath"],
            fields["title"],
            fields["description"],
            fields["body"],
            fields["content_hash"],
            fields["scope"],
            fields["status"],
            fields["recall_enabled"],
            fields["project_id"],
            json.dumps(fields["attrs"]),
        ),
    ).fetchone()
    return row[0]


def _queue(conn, document_id, relpath, action="upsert"):
    row = conn.execute(
        """
        insert into brain.git_exports (document_id, relpath, body, action)
        values (%s, %s, 'stale snapshot', %s)
        returning id
        """,
        (document_id, relpath, action),
    ).fetchone()
    return row[0]


def test_empty_outbox_does_not_call_github(pg):
    class Boom:
        def head_sha(self):
            raise AssertionError("GitHub was called")

    assert export_once(Boom()) == 0


def test_export_commits_once_and_retry_does_not(pg):
    hub = FakeGitHub()
    with pg.connection() as conn:
        doc_id = _insert_memory(conn)
        _queue(conn, doc_id, "memories/widget.md")
        conn.execute(
            """
            insert into brain.meta (key, value)
            values ('ingested_git_head', 'abc1234')
            on conflict (key) do update set value = excluded.value
            """
        )
        conn.commit()
    assert export_once(hub) == 0
    assert hub.commits == 1
    assert "1 file (1 added, 0 updated, 0 deleted)" in hub.messages[0]
    # The outbox body is the raw fact snapshot. The commit is the rendered
    # document, which is what brain-pg-load ingests.
    rendered = render_document(
        {
            "kind": "memory",
            "relpath": "memories/widget.md",
            "title": "widget",
            "description": "alpha widget",
            "body": "The fact.",
            "content_hash": db.memory_content_hash(
                "widget", "alpha widget", "reference", "The fact.", "", None
            ),
            "status": "active",
            "recall_enabled": True,
            "attrs": {"type": "reference"},
        }
    )
    assert hub.files[hub.trees[hub.head]]["memories/widget.md"] == blob_sha(rendered)
    assert b"stale snapshot" not in rendered
    with pg.connection() as conn:
        row = conn.execute(
            "select exported_sha, exported_at is not null from brain.git_exports"
        ).fetchone()
        head = conn.execute(
            "select value from brain.meta where key = 'ingested_git_head'"
        ).fetchone()
        exported = conn.execute(
            "select value from brain.meta where key = 'last_git_export_sha'"
        ).fetchone()
        conn.commit()
    assert row[1] is True
    assert row[0] == hub.head
    assert head[0] == "abc1234"
    assert exported[0] == hub.head

    with pg.connection() as conn:
        conn.execute(
            "update brain.git_exports set exported_at = null, exported_sha = null"
        )
        conn.commit()
    assert export_once(hub) == 0
    assert hub.commits == 1
    with pg.connection() as conn:
        sha = conn.execute("select exported_sha from brain.git_exports").fetchone()[0]
        conn.commit()
    assert sha == hub.head


def test_export_deletes_forgotten_and_retries_a_moved_main(pg):
    hub = FakeGitHub()
    hub.files["t0"]["memories/widget.md"] = "old"
    hub.nff_remaining = 1
    with pg.connection() as conn:
        doc_id = _insert_memory(conn, status="forgotten", stable_id="mem_gone")
        _queue(conn, doc_id, "memories/widget.md")
        conn.commit()
    assert export_once(hub) == 0
    assert hub.commits == 2
    tree = hub.files[hub.trees[hub.head]]
    assert "memories/widget.md" not in tree
    assert "memories/other.md" in tree
    assert "1 deleted" in hub.messages[-1]
    with pg.connection() as conn:
        sha = conn.execute("select exported_sha from brain.git_exports").fetchone()[0]
        conn.commit()
    assert sha == hub.head


def test_bad_relpath_is_marked_and_not_retried(pg):
    hub = FakeGitHub()
    with pg.connection() as conn:
        doc_id = _insert_memory(conn, relpath="../secrets.md", stable_id="mem_bad")
        _queue(conn, doc_id, "../secrets.md")
        conn.commit()
    assert export_once(hub) == 0
    assert hub.commits == 0
    with pg.connection() as conn:
        error, exported = conn.execute(
            "select error, exported_at from brain.git_exports"
        ).fetchone()
        conn.commit()
    assert error == "unsafe relpath"
    assert exported is None
    assert export_once(hub) == 0
    assert hub.commits == 0


def test_lock_blocks_a_second_run(pg):
    hub = FakeGitHub()
    with pg.connection() as conn:
        doc_id = _insert_memory(conn, stable_id="mem_locked")
        _queue(conn, doc_id, "memories/widget.md")
        conn.execute(
            """
            insert into brain.job_locks (name, owner, expires_at)
            values ('git-export', 'other', now() + interval '1 hour')
            """
        )
        conn.commit()
    assert export_once(hub) == 2
    assert hub.commits == 0
    with pg.connection() as conn:
        pending = conn.execute(
            "select count(*) from brain.git_exports where exported_at is null"
        ).fetchone()[0]
        conn.execute(
            """
            update brain.job_locks
               set expires_at = now() - interval '1 minute'
             where name = 'git-export'
            """
        )
        conn.commit()
    assert pending == 1
    assert export_once(hub) == 0
    assert hub.commits == 1


def test_nightly_runs_export_when_backup_fails(monkeypatch):
    from brain import nightly

    calls = []

    def fail_backup():
        calls.append("backup")
        return 1

    def export():
        calls.append("export")
        return 0

    monkeypatch.setattr(nightly.backup, "main", fail_backup)
    monkeypatch.setattr(nightly.git_export, "main", export)
    assert nightly.main([]) == 1
    assert calls == ["backup", "export"]


def test_nightly_ok_when_both_succeed(monkeypatch):
    from brain import nightly

    monkeypatch.setattr(nightly.backup, "main", lambda: 0)
    monkeypatch.setattr(nightly.git_export, "main", lambda: 0)
    assert nightly.main([]) == 0


def test_action_delete_removes_the_path(pg):
    hub = FakeGitHub()
    hub.files["t0"]["memories/widget.md"] = "old"
    with pg.connection() as conn:
        doc_id = _insert_memory(conn, stable_id="mem_del")
        _queue(conn, doc_id, "memories/widget.md", action="delete")
        conn.commit()
    assert export_once(hub) == 0
    assert "memories/widget.md" not in hub.files[hub.trees[hub.head]]
    assert "1 deleted" in hub.messages[-1]


def _rendered_widget(**overrides):
    doc = {
        "kind": "memory",
        "relpath": "memories/widget.md",
        "title": "widget",
        "description": "alpha widget",
        "body": "The fact.",
        "content_hash": db.memory_content_hash(
            "widget", "alpha widget", "reference", "The fact.", "", None
        ),
        "status": "active",
        "recall_enabled": True,
        "attrs": {"type": "reference"},
    }
    doc.update(overrides)
    return render_document(doc)


def test_resync_dry_run_writes_nothing(pg):
    hub = FakeGitHub()
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_dry")
        conn.commit()
    assert resync_once(hub) == 0
    assert hub.commits == 0
    assert hub.updates == 0
    assert hub.files["t0"] == {}
    with pg.connection() as conn:
        sha = conn.execute(
            "select value from brain.meta where key = 'last_git_resync_sha'"
        ).fetchone()
        conn.commit()
    assert sha is None


def test_resync_apply_commits_add_and_update_once(pg):
    hub = FakeGitHub()
    old = b"stale body\n"
    hub.files["t0"]["memories/widget.md"] = blob_sha(old)
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_upd")
        _insert_memory(
            conn,
            stable_id="mem_resync_add",
            relpath="memories/new.md",
            title="new",
            description="brand new",
            body="Fresh fact.",
        )
        conn.commit()
    assert resync_once(hub, apply=True) == 0
    assert hub.commits == 1
    assert hub.updates == 1
    tree = hub.files[hub.trees[hub.head]]
    assert "memories/new.md" in tree
    assert tree["memories/widget.md"] == blob_sha(_rendered_widget())
    assert "--resync" in hub.messages[0]
    with pg.connection() as conn:
        stamped = conn.execute(
            "select value from brain.meta where key = 'last_git_resync_sha'"
        ).fetchone()[0]
        at = conn.execute(
            "select value from brain.meta where key = 'last_git_resync_at'"
        ).fetchone()[0]
        conn.commit()
    assert stamped == hub.head
    assert at


def test_resync_apply_without_delete_leaves_orphans(pg):
    hub = FakeGitHub()
    hub.files["t0"]["memories/orphan.md"] = blob_sha(b"orphan\n")
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_keep")
        conn.commit()
    assert resync_once(hub, apply=True) == 0
    tree = hub.files[hub.trees[hub.head]]
    assert "memories/orphan.md" in tree
    assert "memories/widget.md" in tree


def test_resync_apply_delete_removes_orphans_keeps_unmanaged(pg):
    hub = FakeGitHub()
    hub.files["t0"]["memories/orphan.md"] = blob_sha(b"orphan\n")
    hub.files["t0"]["memories/notes.txt"] = blob_sha(b"not markdown\n")
    hub.files["t0"]["README.md"] = blob_sha(b"root\n")
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_del")
        conn.commit()
    assert resync_once(hub, apply=True, delete=True) == 0
    tree = hub.files[hub.trees[hub.head]]
    assert "memories/orphan.md" not in tree
    assert "memories/notes.txt" in tree  # unmanaged: not .md layout git_render uses
    assert "README.md" in tree  # outside managed prefixes
    assert "memories/widget.md" in tree


def test_resync_lock_contention_exits_without_writing(pg):
    hub = FakeGitHub()
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_lock")
        conn.execute(
            """
            insert into brain.job_locks (name, owner, expires_at)
            values ('git-export', 'other', now() + interval '1 hour')
            """
        )
        conn.commit()
    assert resync_once(hub, apply=True) == 2
    assert hub.commits == 0
    with pg.connection() as conn:
        sha = conn.execute(
            "select value from brain.meta where key = 'last_git_resync_sha'"
        ).fetchone()
        conn.commit()
    assert sha is None


def test_resync_render_error_is_not_deleted(pg, monkeypatch, capsys):
    hub = FakeGitHub()
    kept = b"keep me on git\n"
    hub.files["t0"]["memories/widget.md"] = blob_sha(kept)
    hub.files["t0"]["memories/orphan.md"] = blob_sha(b"orphan\n")
    real = git_export._render_row

    def flaky(row, path):
        if path == "memories/widget.md":
            return "simulated render failure"
        return real(row, path)

    monkeypatch.setattr(git_export, "_render_row", flaky)
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_err")
        conn.commit()
    assert resync_once(hub, apply=True, delete=True) == 0
    tree = hub.files[hub.trees[hub.head]]
    assert tree["memories/widget.md"] == blob_sha(kept)
    assert "memories/orphan.md" not in tree
    out = capsys.readouterr().out
    assert "error" in out
    assert "memories/widget.md" in out
    assert "simulated render failure" in out


def test_resync_dry_run_header_has_per_root_counts(pg, capsys):
    hub = FakeGitHub()
    with pg.connection() as conn:
        _insert_memory(conn, stable_id="mem_resync_roots")
        conn.commit()
    assert resync_once(hub) == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "add [memories=1]" in header
    assert "update [none]" in header
    assert "delete [none]" in header
    assert "error [none]" in header
    assert "excluded [none]" in header


def _insert_episode(conn, **overrides):
    fields = {
        "stable_id": "ep_public000000001",
        "kind": "episode",
        "relpath": "episodes/2026/10/ep_public000000001.md",
        "body": "USER: hello\n\nASSISTANT: hi\n",
        "content_hash": "episode-hash",
        "cwd": "/Users/you/src/public",
        "session_id": "sess",
        "uuid": "uuid-1",
        "git_branch": "main",
        "status": "active",
        "recall_enabled": True,
        "attrs": {},
        "ts": "2026-10-19T01:02:03.004000+00:00",
    }
    fields.update(overrides)
    row = conn.execute(
        """
        insert into brain.documents (
          stable_id, kind, relpath, body, content_hash, cwd, session_id, uuid,
          git_branch, status, recall_enabled, attrs, ts
        ) values (
          %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s::jsonb, %s
        ) returning id
        """,
        (
            fields["stable_id"],
            fields["kind"],
            fields["relpath"],
            fields["body"],
            fields["content_hash"],
            fields["cwd"],
            fields["session_id"],
            fields["uuid"],
            fields["git_branch"],
            fields["status"],
            fields["recall_enabled"],
            json.dumps(fields["attrs"]),
            fields["ts"],
        ),
    ).fetchone()
    return row[0]


def test_resync_git_export_false_is_not_added(pg, monkeypatch):
    from brain import ingest

    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    hub = FakeGitHub()
    hidden = "episodes/2026/10/ep_noexport00000001.md"
    public = "episodes/2026/10/ep_public000000001.md"
    hub.files["t0"][hidden] = blob_sha(b"already in git\n")
    with pg.connection() as conn:
        _insert_episode(
            conn,
            stable_id="ep_noexport00000001",
            relpath=hidden,
            cwd="/Users/you/src/public",
            attrs={"git_export": False},
        )
        _insert_episode(conn, stable_id="ep_public000000001", relpath=public)
        conn.commit()
    assert resync_once(hub, apply=True, delete=True) == 0
    tree = hub.files[hub.trees[hub.head]]
    assert hidden not in tree
    assert public in tree


def test_resync_episode_cwd_exclude_is_not_added(pg, monkeypatch):
    from brain import ingest

    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", ["privacy-denylist-token"])
    hub = FakeGitHub()
    hidden = "episodes/2026/10/ep_cwdsecret000001.md"
    public = "episodes/2026/10/ep_cwdpublic000001.md"
    hub.files["t0"][hidden] = blob_sha(b"already in git\n")
    with pg.connection() as conn:
        _insert_episode(
            conn,
            stable_id="ep_cwdsecret000001",
            relpath=hidden,
            cwd="/work/privacy-denylist-token/repo",
            attrs={},
        )
        _insert_episode(
            conn,
            stable_id="ep_cwdpublic000001",
            relpath=public,
            cwd="/Users/you/src/public",
            attrs={},
        )
        conn.commit()
    assert resync_once(hub, apply=True, delete=True) == 0
    tree = hub.files[hub.trees[hub.head]]
    assert hidden not in tree
    assert public in tree


def test_resync_dry_run_excluded_count_hides_paths(pg, monkeypatch, capsys):
    from brain import ingest

    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", ["privacy-denylist-token"])
    hub = FakeGitHub()
    flagged = "episodes/2026/10/ep_flaghidden000001.md"
    by_cwd = "episodes/2026/10/ep_cwdhidden0000001.md"
    hub.files["t0"][flagged] = blob_sha(b"flagged already in git\n")
    hub.files["t0"][by_cwd] = blob_sha(b"cwd already in git\n")
    with pg.connection() as conn:
        _insert_episode(
            conn,
            stable_id="ep_flaghidden000001",
            relpath=flagged,
            cwd="/Users/you/src/public",
            attrs={"git_export": False},
        )
        _insert_episode(
            conn,
            stable_id="ep_cwdhidden0000001",
            relpath=by_cwd,
            cwd="/work/privacy-denylist-token/repo",
            attrs={},
        )
        _insert_memory(conn, stable_id="mem_resync_visible")
        conn.commit()
    assert resync_once(hub) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "1 add [memories=1]" in out
    assert "2 excluded [episodes=2]" in out
    assert "memories/widget.md" in out
    for secret in (flagged, by_cwd, "privacy-denylist-token"):
        assert secret not in out
        assert secret not in captured.err


def test_create_blob_retries_after_403_with_retry_after(monkeypatch):
    import io
    import json
    import urllib.error
    from email.message import Message

    sleeps: list[float] = []
    monkeypatch.setattr(git_export.time, "sleep", lambda s: sleeps.append(s))

    payload = b"hello blob\n"
    expected = blob_sha(payload)
    hits = {"n": 0}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"sha": expected}).encode()

    class _Opener:
        def open(self, req, timeout=None):
            hits["n"] += 1
            if hits["n"] == 1:
                headers = Message()
                headers["Retry-After"] = "0"
                raise urllib.error.HTTPError(
                    req.full_url,
                    403,
                    "rate limited",
                    headers,
                    io.BytesIO(b'{"message":"secondary rate limit"}'),
                )
            return _Resp()

    client = GitHubClient("token", "OWNER/brain", "main", opener=_Opener())
    assert client.create_blob(payload) == expected
    assert hits["n"] == 2
    assert 0.0 in sleeps



def _as_postgres(loaded):
    """The row as psycopg returns it: ``timestamptz`` columns are aware datetimes."""
    row = _loaded(loaded)
    if isinstance(row["ts"], str) and row["ts"]:
        row["ts"] = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    return row


def _episode_file(ts):
    from brain.ingest import _render_episode_file

    text = "USER: hello\n\nASSISTANT: hi"
    digest = db.episode_content_hash(ts, "/Users/you/src", "main", 0, text)
    return _render_episode_file(
        "ep_abc", digest, "sess", "uuid-1", ts, "/Users/you/src", "main", 0, text
    ).encode("utf-8")


def test_episode_from_timestamptz_reproduces_the_committed_millisecond_file(tmp_path):
    committed = _episode_file("2026-06-18T20:03:38.669Z")
    loaded = build_doc(tmp_path, "episodes/2026/06/ep_abc.md", {}, "", raw=committed)
    assert render_document(_as_postgres(loaded)) == committed


def test_episode_from_timestamptz_keeps_a_microsecond_file_whose_hash_says_so(tmp_path):
    committed = _episode_file("2026-06-18T20:03:38.669000+00:00")
    loaded = build_doc(tmp_path, "episodes/2026/06/ep_abc.md", {}, "", raw=committed)
    assert render_document(_as_postgres(loaded)) == committed


def test_episode_without_a_matching_hash_falls_back_to_milliseconds_z():
    row = {
        "kind": "episode",
        "stable_id": "ep_abc",
        "body": "USER: hi",
        "ts": datetime(2026, 6, 18, 20, 3, 38, 669000, tzinfo=timezone.utc),
        "content_hash": "not-a-match",
        "attrs": {},
    }
    raw = render_document(row).decode()
    assert 'ts: "2026-06-18T20:03:38.669Z"' in raw


def test_handoff_from_timestamptz_reproduces_offset_date_and_quoting(tmp_path):
    for date in ("2026-07-03T09:38:00-05:00", "2026-07-06T21:01:07Z"):
        committed = (
            "---\n"
            f"date: {date}\n"
            "project: ~/src/brain\n"
            'description: "ready: ship it"\n'
            "---\n\n# Handoff: note\n\nNext: ship it.\n"
        ).encode("utf-8")
        relpath = "handoffs/2026-07-03-note.md"
        loaded = build_doc(tmp_path, relpath, {}, "", raw=committed)
        assert render_document(_as_postgres(loaded)) == committed


def test_publish_sends_text_inline_in_batched_trees():
    hub = FakeGitHub()
    changes = [
        Change(path=f"episodes/2026/10/ep_{i:04d}.md", data=f"body {i}\n".encode(), ids=[i])
        for i in range(250)
    ]
    result = publish(hub, changes)
    assert result.added == 250 and result.committed
    assert hub.blobs == 0
    assert hub.tree_calls == [100, 100, 50]
    landed = hub.files[hub.trees[hub.head]]
    assert all(landed[c.path] == blob_sha(c.data) for c in changes)


def test_publish_uses_a_blob_for_non_utf8_and_rejects_a_mismatched_tree():
    import pytest

    hub = FakeGitHub()
    publish(hub, [Change(path="memories/bin.md", data=b"\xff\xfe\n", ids=[1])])
    assert hub.blobs == 1

    class Corrupting(FakeGitHub):
        def create_tree(self, base_tree, entries):
            sha = super().create_tree(base_tree, entries)
            self.files[sha] = {path: "bad" for path in self.files[sha]}
            return sha

    bad = Corrupting()
    with pytest.raises(git_export.GitHubError):
        publish(bad, [Change(path="memories/a.md", data=b"a\n", ids=[1])])
    assert bad.commits == 0
