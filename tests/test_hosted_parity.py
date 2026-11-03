"""Hosted parity: device routes, skill tools, reflect window, and the remote hooks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastmcp")

from brain import hosted_docs, remote_hooks  # noqa: E402
from brain.authz import AuthSettings, Principal  # noqa: E402
from brain.device_tokens import SCOPE_HOOK_READ, SCOPE_INGEST, mint  # noqa: E402

ME = Principal(email="you@example.com", profile=None, can_write=True, sub="sub-1")
ROOT = "C:\\Users\\you\\src\\brain"


def _now():
    return datetime.now(timezone.utc)


def _episode(pg, sid, session, ts, *, cwd=ROOT, body="USER: q\n\nASSISTANT: a", sidechain=False, scope=None):
    import json

    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (stable_id, kind, body, content_hash, ts, day,
                                         cwd, session_id, attrs, relpath, scope)
            values (%s, 'episode', %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (sid, body, sid, ts, ts.date(), cwd, session,
             json.dumps({"is_sidechain": sidechain}), f"episodes/x/{sid}.md", scope),
        )
        conn.commit()


# --------------------------------------------------------- tool surface ---

def test_skill_tools_are_on_mcp_only():
    from brain.http_server import make_servers

    full, readonly = make_servers(None)
    full_names = set(asyncio.run(full.get_tools()))
    ro_names = set(asyncio.run(readonly.get_tools()))
    for name in (
        "device_token_create",
        "device_token_revoke_hash",
        "state_write",
        "summary_write",
        "reflect_claim",
        "reflect_window",
    ):
        assert name in full_names
        assert name not in ro_names
    assert ro_names == {"recall", "get_episode"}


def test_tools_refuse_without_a_hosted_session():
    from brain import hosted_tools
    from brain.device_api import device_token_create

    assert "error" in device_token_create("laptop")
    assert "error" in hosted_tools.state_write("brain", "body")


# --------------------------------------------------------- state notes ---

def test_state_write_then_read_with_freshness(pg):
    out = hosted_docs.state_write("brain", "Task: ship hosted ingest.", "hosted ingest in review")
    assert out["status"] == "created" and out["path"] == "memories/brain-state.md"
    assert hosted_docs.state_write("brain", "Task: ship hosted ingest.", "hosted ingest in review")["status"] in (
        "unchanged",
        "updated",
    )
    later = _now() + timedelta(minutes=5)
    _episode(pg, "ep_after", "s1", later)
    _episode(pg, "ep_side", "s1", later, sidechain=True)
    _episode(pg, "ep_elsewhere", "s2", later, cwd="C:\\Users\\you\\src\\other")
    _episode(pg, "ep_sub", "s3", later, cwd=ROOT + "\\src")
    from psycopg.rows import dict_row

    with pg.connection() as conn:
        conn.row_factory = dict_row
        got = hosted_docs.read_state(conn, "brain", [ROOT], None)
        missing = hosted_docs.read_state(conn, "nothing-here", [ROOT], None)
        conn.commit()
    assert got["found"] and got["body"].startswith("Task:")
    assert got["episodes_since"] == 2
    assert got["written"].endswith("Z")
    assert missing == {"found": False}


def test_summary_rewrite_keeps_path_and_exclude_refuses(pg, monkeypatch):
    start = _now() - timedelta(days=40)
    for i in range(3):
        _episode(pg, f"ep_{i}", "sess-a", start + timedelta(minutes=i))
    first = hosted_docs.summary_write("sess-a", "Digest one.", "first digest")
    assert first["path"] == f"summaries/{start:%Y-%m}/sess-a.md"
    again = hosted_docs.summary_write("sess-a", "Digest two, longer.", "rewritten")
    assert again["path"] == first["path"] and again["status"] == "updated"
    monkeypatch.setattr(hosted_docs, "BRAIN_DIGEST_EXCLUDE", ["brain"])
    assert "error" in hosted_docs.summary_write("sess-a", "x", "y")


def _scope_of(pg, stable_id: str):
    with pg.connection() as conn:
        row = conn.execute(
            "select scope from brain.documents where stable_id = %s", (stable_id,)
        ).fetchone()
        conn.commit()
    return row[0]


def test_summary_write_scopes_like_the_episodes(pg):
    """A work session's digest is hidden from personal. Unscoped rows stay readable."""
    from brain.profile_ctx import override_profile

    start = _now() - timedelta(days=2)
    _episode(pg, "ep_work", "sess-work", start, scope="work", body="quorplework session")
    _episode(pg, "ep_shared", "sess-shared", start, body="quorpleshared session")
    with override_profile(None):
        work = hosted_docs.summary_write("sess-work", "quorplework digest body", "work day")
        shared = hosted_docs.summary_write("sess-shared", "quorpleshared digest body", "shared day")
    assert _scope_of(pg, work["id"]) == "work"
    assert _scope_of(pg, shared["id"]) is None

    with override_profile("personal"):
        ids = {hit["id"] for hit in pg.recall("quorplework digest", scope="summaries", k=10)["hits"]}
        assert work["id"] not in ids
        assert "unavailable" in pg.get_item(work["id"])["error"]
        ids = {hit["id"] for hit in pg.recall("quorpleshared digest", scope="summaries", k=10)["hits"]}
        assert shared["id"] in ids
        refused = hosted_docs.summary_write("sess-work", "personal rewrite", "nope")
        assert "another profile" in refused["error"]
    with override_profile("work"):
        ids = {hit["id"] for hit in pg.recall("quorplework digest", scope="summaries", k=10)["hits"]}
        assert work["id"] in ids
        ids = {hit["id"] for hit in pg.recall("quorpleshared digest", scope="summaries", k=10)["hits"]}
        assert shared["id"] in ids

    # A digest written before scoping stays readable from every profile.
    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (
              stable_id, kind, scope, body, content_hash, title, description,
              relpath, session_id, ts, day, status, recall_enabled
            ) values (
              'sum_legacy', 'summary', null, 'quorplelegacy digest', 'legacy',
              'legacy', 'old', 'summaries/legacy/sess-old.md', 'sess-old',
              %s, %s, 'active', true
            )
            """,
            (start, start.date()),
        )
        conn.commit()
    with override_profile("personal"):
        ids = {hit["id"] for hit in pg.recall("quorplelegacy", scope="summaries", k=10)["hits"]}
    assert "sum_legacy" in ids
    with override_profile("work"):
        ids = {hit["id"] for hit in pg.recall("quorplelegacy", scope="summaries", k=10)["hits"]}
    assert "sum_legacy" in ids

    # Rewriting an unscoped digest of a work session stamps it.
    from brain.ids import summary_stable_id

    relpath = f"summaries/{start:%Y-%m}/sess-stamp.md"
    stamp_id = summary_stable_id(relpath)
    _episode(pg, "ep_stamp", "sess-stamp", start, scope="work", body="quorplestamp session")
    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (
              stable_id, kind, scope, body, content_hash, title, description,
              relpath, session_id, ts, day, status, recall_enabled
            ) values (%s, 'summary', null, 'old stamp', 'old-stamp', 'stamp', 'old',
                      %s, 'sess-stamp', %s, %s, 'active', true)
            """,
            (stamp_id, relpath, start, start.date()),
        )
        conn.commit()
    with override_profile(None):
        rewritten = hosted_docs.summary_write("sess-stamp", "quorplestamp digest rewritten", "work day")
    assert rewritten["id"] == stamp_id and rewritten["status"] == "updated"
    assert _scope_of(pg, stamp_id) == "work"


def test_summary_without_episodes_takes_the_caller_profile(pg):
    from brain.profile_ctx import override_profile

    with override_profile("work"):
        out = hosted_docs.summary_write("sess-empty", "quorpleempty digest", "nothing stored")
    assert _scope_of(pg, out["id"]) == "work"
    with override_profile("personal"):
        assert "unavailable" in pg.get_item(out["id"])["error"]


def test_reflect_claim_follows_the_caller_profile(pg):
    from brain.profile_ctx import override_profile

    with override_profile("work"):
        work = hosted_docs.reflect_claim("work-user")
    assert work["ok"]
    with pg.connection() as conn:
        profile = conn.execute(
            "select profile from brain.reflect_runs where id = %s", (work["id"],)
        ).fetchone()[0]
        conn.commit()
    assert profile == "work"
    with override_profile("personal"):
        personal = hosted_docs.reflect_claim("personal-user")
    assert personal["ok"] is True


def test_headless_reflect_allowlist_is_least_privilege():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "skills"
    reflect = (root / "reflect" / "SKILL.md").read_text(encoding="utf-8")
    run = (root / "reflect-run" / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__brain__*" not in reflect
    assert "+brain" not in run
    for tool in (
        "reflect_claim",
        "reflect_complete",
        "reflect_window",
        "reflect_sessions",
        "session_episodes",
        "summary_write",
        "get_episode",
        "recall",
        "remember",
        "memory_update",
        "memory_set_status",
        "state_get",
        "state_write",
        "proposal_write",
    ):
        assert f"mcp__brain__{tool}" in reflect
        assert f"mcp__brain__{tool}" in run
    for banned in ("device_token_create", "device_token_revoke", "device_token_revoke_hash"):
        assert banned not in reflect
    for path in root.rglob("SKILL.md"):
        assert "mcp__brain__*" not in path.read_text(encoding="utf-8")


def test_handoff_and_proposal_round_trip(pg):
    out = hosted_docs.handoff_write("Hosted Ingest", "# Hosted ingest\n\nNext: deploy.", "where we are", "brain")
    assert out["path"].startswith("handoffs/") and out["path"].endswith("-hosted-ingest.md")
    latest = hosted_docs.handoff_latest("ingest")["handoffs"]
    assert latest[0]["title"] == "Hosted ingest" and "deploy" in latest[0]["text"]
    assert hosted_docs.handoff_latest("nomatch")["handoffs"] == []
    prop = hosted_docs.proposal_write("weekly-digest", "Repeated twice.", "a skill idea")
    assert prop["path"].startswith("proposals/") and prop["id"].startswith("prop_")


def test_memory_update_and_status(pg):
    from brain import pgstore

    mem = pgstore.remember("Brain ingest runs from a hook.", type="reference", force=True)
    out = hosted_docs.memory_update(mem["id"], "Brain ingest runs from a SessionEnd hook.")
    assert out["status"] == "updated" and out["id"] == mem["id"]
    assert "SessionEnd" in pgstore.get_item(mem["id"])["text"]
    assert hosted_docs.memory_set_status(mem["id"], "archived")["status"] == "archived"
    assert "unavailable" in pgstore.get_item(mem["id"])["error"]
    assert "error" in hosted_docs.memory_set_status(mem["id"], "deleted")


def test_memory_update_refuses_to_restore_inactive_memories(pg):
    """Compaction must not put an archived or forgotten memory back into recall."""
    from brain import pgstore

    mem = pgstore.remember("Widget quota is forty.", type="reference", force=True)
    assert hosted_docs.memory_set_status(mem["id"], "archived")["status"] == "archived"
    refused = hosted_docs.memory_update(mem["id"], "Widget quota is forty two.")
    assert "archived" in refused["error"]
    with pg.connection() as conn:
        status, body, archived_at = conn.execute(
            "select status, body, archived_at is not null from brain.documents where stable_id = %s",
            (mem["id"],),
        ).fetchone()
        conn.commit()
    assert status == "archived" and archived_at is True
    assert "forty two" not in body

    assert hosted_docs.memory_set_status(mem["id"], "forgotten")["status"] == "forgotten"
    refused = hosted_docs.memory_update(mem["id"], "Widget quota is forgotten text.")
    assert "forgotten" in refused["error"]
    with pg.connection() as conn:
        status, body = conn.execute(
            "select status, body from brain.documents where stable_id = %s",
            (mem["id"],),
        ).fetchone()
        conn.commit()
    assert status == "forgotten" and "forgotten text" not in body


def test_state_rewrite_still_republishes_an_archived_note(pg):
    """memory_update refuses; rewriting a state note is a new publish."""
    out = hosted_docs.state_write("brain", "Task: one.", "one")
    assert hosted_docs.memory_set_status(out["id"], "archived")["status"] == "archived"
    again = hosted_docs.state_write("brain", "Task: two.", "two")
    assert again["status"] == "updated"
    with pg.connection() as conn:
        status, body = conn.execute(
            "select status, body from brain.documents where stable_id = %s",
            (out["id"],),
        ).fetchone()
        conn.commit()
    assert status == "active" and body.startswith("Task: two.")


# ------------------------------------------------------------- reflect ---

def test_reflect_window_sessions_and_lease(pg):
    old = _now() - timedelta(days=30)
    recent = _now() - timedelta(hours=3)
    _episode(pg, "ep_old", "s-old", old)
    for i in range(3):
        _episode(pg, f"ep_r{i}", "s-new", recent + timedelta(minutes=i))
    _episode(pg, "ep_rside", "s-new", recent, sidechain=True)

    claim = hosted_docs.reflect_claim("test-host")
    assert claim["ok"] and claim["window_start"]
    assert hosted_docs.reflect_claim("other-host")["ok"] is False

    window = hosted_docs.reflect_window(since=(old + timedelta(days=1)).isoformat())
    assert [e["id"] for e in window["episodes"]] == ["ep_r0", "ep_r1", "ep_r2"]
    sessions = hosted_docs.reflect_sessions()["sessions"]
    assert [s["session_id"] for s in sessions] == ["s-new"]
    hosted_docs.summary_write("s-new", "Digest.", "digest", episodes=3)
    assert hosted_docs.reflect_sessions()["sessions"] == []
    eps = hosted_docs.session_episodes("s-new")["episodes"]
    assert len(eps) == 3

    done = hosted_docs.reflect_complete(claim["id"])
    assert done["ok"]
    from psycopg.rows import dict_row

    with pg.connection() as conn:
        conn.row_factory = dict_row
        debt = hosted_docs.reflect_debt(conn)
        conn.commit()
    assert debt["sessions"] == 0 and debt["last_reflect"]
    assert hosted_docs.reflect_complete(claim["id"])["ok"] is False


# -------------------------------------------------------- device routes ---

@pytest.fixture
def app(pg, monkeypatch):
    pytest.importorskip("httpx")
    monkeypatch.setenv("BRAIN_AUTH_DOMAIN", "example.com")
    from brain import ingest_api
    from brain.google_auth import BrainGoogleProvider
    from brain.http_server import build_http_app

    monkeypatch.setattr(ingest_api, "embed_texts", lambda texts: [[1.0] + [0.0] * 255 for _ in texts])
    settings = AuthSettings(
        domain="example.com",
        allowed_emails=frozenset(),
        email_profiles={},
        default_profile=None,
        default_write=True,
        public_url="https://brain.example",
    )
    provider = BrainGoogleProvider(
        settings,
        client_id="test.apps.googleusercontent.com",
        client_secret="test-client-secret-not-real",
        base_url="https://brain.example",
        issuer_url="https://brain.example",
        jwt_signing_key="test-signing-key-32chars-minimum",
    )
    with pg.connection() as conn:
        tokens = {
            "ingest": mint(conn, ME, "laptop", ("personal", "work"), (SCOPE_INGEST,))["token"],
            "read": mint(conn, ME, "laptop", ("personal", "work"), (SCOPE_HOOK_READ,))["token"],
        }
        conn.commit()
    return build_http_app(provider, public_url="https://brain.example"), tokens


def _call(app, path, payload, token=None):
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://brain.example") as c:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            return await c.post(path, json=payload, headers=headers)

    return asyncio.run(go())


def test_ingest_route_scopes_and_idempotence(app):
    from brain import db

    application, tokens = app
    ep = {
        "stable_id": db.episode_stable_id("s1", "u1"),
        "session_id": "s1",
        "uuid": "u1",
        "ts": "2026-10-19T10:00:00Z",
        "cwd": ROOT,
        "text": "USER: hi",
    }
    assert _call(application, "/ingest/episodes", {"episodes": [ep]}).status_code == 401
    assert _call(application, "/ingest/episodes", {"episodes": [ep]}, tokens["read"]).status_code == 401
    first = _call(application, "/ingest/episodes", {"episodes": [ep]}, tokens["ingest"])
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 1
    again = _call(application, "/ingest/episodes", {"episodes": [ep]}, tokens["ingest"])
    assert again.json()["unchanged"] == 1
    bad = _call(application, "/ingest/episodes", {"episodes": "nope"}, tokens["ingest"])
    assert bad.status_code == 400


def test_hook_routes_and_remote_hooks(app, pg, monkeypatch, tmp_path):
    application, tokens = app
    from brain import pgstore

    pgstore.remember("Widget gateway restarts need the vendor portal.", type="reference", force=True)
    hosted_docs.state_write("brain", "Task: parity work.", "parity in progress")
    assert _call(application, "/hook/state", {"project": "brain"}, tokens["ingest"]).status_code == 401
    state = _call(application, "/hook/state", {"project": "brain", "roots": [ROOT]}, tokens["read"])
    assert state.status_code == 200 and state.json()["found"]

    def send(path, payload, *, token, timeout):
        resp = _call(application, path, payload, token)
        assert resp.status_code == 200, resp.text
        return resp.json()

    monkeypatch.setenv("BRAIN_CLIENT_DIR", str(tmp_path))
    monkeypatch.setattr("brain.session_start._resolve_root", lambda cwd: tmp_path / "brain")
    monkeypatch.setattr("brain.session_start.repo_drift", lambda root, epoch: False)
    block = remote_hooks.session_block({"cwd": ROOT}, token=tokens["read"], send=send)
    assert "Project state from brain (memories/brain-state.md" in block
    assert "Task: parity work." in block

    data = {"prompt": "how do widget gateway restarts work", "session_id": "sess-9"}
    block = remote_hooks.recall_block(data, token=tokens["read"], send=send)
    assert "Possibly relevant past memories" in block and "vendor portal" in block
    assert remote_hooks.recall_block(data, token=tokens["read"], send=send) == ""  # seen this session
    assert remote_hooks.recall_block({"prompt": "/clear"}, token=tokens["read"], send=send) == ""


def test_reflect_notice_threshold(monkeypatch):
    assert remote_hooks.reflect_notice(None) is None
    assert remote_hooks.reflect_notice({"sessions": 0, "hours_since": 99}) is None
    assert remote_hooks.reflect_notice({"sessions": 3, "hours_since": 1}) is None
    # BRAIN_REFLECT_STALL_H defaults to 36: under that, quiet even with debt.
    assert remote_hooks.reflect_notice({"sessions": 3, "hours_since": 30.0}) is None
    due = remote_hooks.reflect_notice({"sessions": 3, "hours_since": 40.0})
    assert due is not None
    assert "nightly reflect looks stalled" in due
    assert "Last pass 40h ago" in due
    assert "3 sessions pending" in due
    assert "/reflect" not in due
    unknown = remote_hooks.reflect_notice({"sessions": 2, "hours_since": None})
    assert unknown is not None
    assert "Last pass unknown ago" in unknown
    assert "2 sessions pending" in unknown


def test_remote_hooks_fail_silent(monkeypatch):
    from brain.client import ClientError

    def down(*_a, **_k):
        raise ClientError(0, "server unreachable")

    with pytest.raises(ClientError):
        remote_hooks.recall_block({"prompt": "widget gateway restarts"}, token="t", send=down)
    assert remote_hooks.recall_block({"prompt": "widget gateway"}, token="", send=down) == ""


def test_hook_deadline_covers_a_hung_network(monkeypatch, capsys):
    """A request stuck in DNS or connect cannot hold the prompt past the
    deadline: the hook leaves silently with exit 0."""
    import time

    exits = []
    monkeypatch.setattr(remote_hooks.os, "_exit", lambda code: exits.append(code))
    started = time.monotonic()
    remote_hooks._guarded("t", "UserPromptSubmit", lambda: time.sleep(5) or "late", 0.3)
    assert time.monotonic() - started < 2
    assert exits == [0] and capsys.readouterr().out == ""


def test_hook_within_deadline_still_prints(capsys):
    with pytest.raises(SystemExit) as done:
        remote_hooks._guarded("t", "UserPromptSubmit", lambda: "block", 2.0)
    assert done.value.code == 0
    assert "block" in capsys.readouterr().out
