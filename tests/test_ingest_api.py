"""Hosted session ingest: validation without a database, upsert semantics with one."""

from __future__ import annotations

import pytest

from brain import db, ingest_api
from brain.device_tokens import DeviceIdentity

BOTH = DeviceIdentity(1, "sub-1", "you@example.com", "laptop", ("ingest",), ("personal", "work"))
WORK = DeviceIdentity(2, "sub-1", "you@example.com", "desk", ("ingest",), ("work",))


def _ep(session="s1", uuid="u1", ts="2026-10-20T10:00:00Z", text="USER: hello there", **kw):
    out = {
        "stable_id": db.episode_stable_id(session, uuid),
        "session_id": session,
        "uuid": uuid,
        "ts": ts,
        "cwd": "C:\\Users\\you\\src\\brain",
        "git_branch": "main",
        "is_sidechain": False,
        "text": text,
    }
    out.update(kw)
    return out


def test_validation_recomputes_hash_and_derives_project():
    ep, err = ingest_api.validate_episode(_ep(), BOTH)
    assert err is None
    assert ep["content_hash"] == db.episode_content_hash(
        "2026-10-20T10:00:00Z", "C:\\Users\\you\\src\\brain", "main", 0, "USER: hello there"
    )
    assert ep["project_id"] == "brain"
    assert ep["relpath"] == f"episodes/2026/10/{ep['stable_id']}.md"
    assert ep["scope"] is None


def test_validation_refuses_bad_ids_scope_and_size():
    assert ingest_api.validate_episode(_ep(stable_id="ep_nothex"), BOTH)[1]
    forged = _ep(uuid="other", stable_id=_ep()["stable_id"])
    assert "does not match" in ingest_api.validate_episode(forged, BOTH)[1]
    assert ingest_api.validate_episode(_ep(text=""), BOTH)[1]
    assert ingest_api.validate_episode(_ep(text="x" * 20_001), BOTH)[1]
    assert "cannot write personal" in ingest_api.validate_episode(_ep(scope="personal"), WORK)[1]
    ep, _ = ingest_api.validate_episode(_ep(), WORK)
    assert ep["scope"] == "work"  # a one-profile device stamps scope-less episodes
    with pytest.raises(ValueError):
        ingest_api.parse_payload({"episodes": [_ep()] * 201}, BOTH)


def test_export_gate_follows_client_flag_and_server_exclude(monkeypatch):
    assert ingest_api.validate_episode(_ep(git_export=False), BOTH)[0]["git_export"] is False
    monkeypatch.setattr(ingest_api, "_episode_excluded", lambda cwd: "brain" in (cwd or ""))
    assert ingest_api.validate_episode(_ep(), BOTH)[0]["git_export"] is False


# ------------------------------------------------------------- postgres ---

@pytest.fixture
def store(pg, monkeypatch):
    calls = []

    def fake_embed(texts):
        calls.append(len(texts))
        return [[1.0] + [0.0] * 255 for _ in texts]

    monkeypatch.setattr(ingest_api, "embed_texts", fake_embed)
    pg.calls = calls
    return pg


def _push(eps, identity=BOTH):
    return ingest_api.ingest(identity, {"episodes": eps})


def _row(pg, sid):
    from psycopg.rows import dict_row

    with pg.connection() as conn:
        conn.row_factory = dict_row
        row = conn.execute("select * from brain.documents where stable_id = %s", (sid,)).fetchone()
        n = conn.execute("select count(*) as n from brain.git_exports").fetchone()["n"]
        conn.commit()
    return row, n


def test_insert_then_resend_is_a_noop(store):
    ep = _ep()
    first = _push([ep])
    assert first["inserted"] == 1 and first["results"][ep["stable_id"]] == "inserted"
    row, exports = _row(store, ep["stable_id"])
    assert row["kind"] == "episode" and row["embedded_hash"] == row["content_hash"]
    assert row["project_id"] == "brain" and row["host"] == "laptop"
    again = _push([ep])
    assert again["unchanged"] == 1 and again["inserted"] == 0
    assert store.calls == [1]  # nothing re-embedded
    assert _row(store, ep["stable_id"])[1] == exports


def test_growing_session_updates_in_place_and_keeps_lifecycle(store):
    ep = _ep(source_mtime=100.0)
    _push([ep])
    with store.connection() as conn:
        conn.execute("update brain.documents set recall_enabled = false where stable_id = %s", (ep["stable_id"],))
        conn.commit()
    grown = _ep(text="USER: hello there\n\nASSISTANT: more", source_mtime=200.0)
    out = _push([grown])
    assert out["updated"] == 1
    row, _ = _row(store, ep["stable_id"])
    assert row["body"].endswith("more")
    assert row["recall_enabled"] is False  # lifecycle untouched
    older = _ep(text="USER: hello there\n\nASSISTANT: older read", source_mtime=150.0)
    assert _push([older])["stale"] == 1
    assert _row(store, ep["stable_id"])[0]["body"].endswith("more")


def test_forked_copy_is_a_duplicate(store):
    _push([_ep()])
    fork = _ep(session="s2", uuid="u9")
    out = _push([fork])
    assert out["duplicate"] == 1
    assert _row(store, fork["stable_id"])[0] is None


def test_excluded_episode_is_stored_but_not_exported(store):
    ep = _ep(git_export=False)
    _push([ep])
    row, exports = _row(store, ep["stable_id"])
    assert row is not None and row["attrs"]["git_export"] is False
    assert exports == 0


def test_embed_failure_writes_nothing(store, monkeypatch):
    def boom(_texts):
        raise RuntimeError("down")

    monkeypatch.setattr(ingest_api, "embed_texts", boom)
    with pytest.raises(ingest_api.EmbedUnavailable):
        _push([_ep()])
    assert _row(store, _ep()["stable_id"])[0] is None


def test_rejected_items_do_not_block_the_batch(store):
    out = _push([_ep(), _ep(uuid="u2", stable_id="ep_0000000000000000")])
    assert out["inserted"] == 1
    assert out["rejected"][0]["index"] == 1


def test_validation_requires_uuid_so_the_id_is_always_derived():
    blank = _ep(uuid="", stable_id=_ep()["stable_id"])
    assert "required" in ingest_api.validate_episode(blank, WORK)[1]
    assert "required" in ingest_api.validate_episode(_ep(session=""), WORK)[1]


def test_work_token_cannot_overwrite_or_rescope_a_personal_episode(store):
    mine = _ep(scope="personal")
    _push([mine])
    hijack = _ep(text="USER: replaced", source_mtime=999.0)
    out = _push([hijack], identity=WORK)
    assert out["forbidden"] == 1 and out["updated"] == 0
    row, _ = _row(store, mine["stable_id"])
    assert row["body"] == "USER: hello there" and row["scope"] == "personal"
    # Even a token allowed both profiles never moves a row to another scope.
    grown = _ep(text="USER: hello there\n\nASSISTANT: more", scope="work")
    assert _push([grown])["updated"] == 1
    row, _ = _row(store, mine["stable_id"])
    assert row["body"].endswith("more") and row["scope"] == "personal"


def _limit_pool(store, monkeypatch, *, max_size="1", timeout="3"):
    monkeypatch.setenv("BRAIN_PG_POOL_MAX", max_size)
    monkeypatch.setenv("BRAIN_PG_POOL_TIMEOUT", timeout)
    store.close_pool()


def _unit_vecs(texts):
    return [[1.0] + [0.0] * 255 for _ in texts]


def test_embedding_holds_neither_pool_slot_nor_advisory_lock(store, monkeypatch):
    """The embedder runs with the connection back in the pool and the lock free.

    Pool max is 1, so a checkout inside the embedder fails if ingest still
    holds its connection. A second session trying the transaction lock fails
    if that connection's transaction is still open.
    """
    import os

    import psycopg

    _limit_pool(store, monkeypatch)
    saw = {"embed": False}

    def fake_embed(texts):
        saw["embed"] = True
        with psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None) as probe:
            free = probe.execute(
                "select pg_try_advisory_xact_lock(hashtext('brain-ingest'))"
            ).fetchone()[0]
            probe.rollback()
        assert free is True
        with store.connection() as conn:
            conn.execute("select 1")
            conn.rollback()
        return _unit_vecs(texts)

    monkeypatch.setattr(ingest_api, "embed_texts", fake_embed)
    out = _push([_ep(text="USER: embed without the pool")])
    assert saw["embed"] is True
    assert out["inserted"] == 1
    assert _row(store, _ep(text="USER: embed without the pool")["stable_id"])[0]["body"].endswith("pool")


def test_concurrent_ingest_proceeds_during_embed_and_fork_dedupes(store, monkeypatch):
    """A second push must finish while the first is still inside the embedder.

    The second push is a fork of the same text. It inserts. The first, which
    planned before that insert, re-plans under the lock and skips the copy.
    """
    import threading

    _limit_pool(store, monkeypatch)
    started = threading.Event()
    release = threading.Event()
    calls = []
    slow_ep = _ep(session="slow-sess", uuid="slow-uuid", text="USER: same body")
    fast_ep = _ep(session="fast-sess", uuid="fast-uuid", text="USER: same body")

    def fake_embed(texts):
        calls.append(len(texts))
        if not started.is_set():
            started.set()
            if not release.wait(timeout=10):
                raise RuntimeError("concurrent ingest did not finish during embed")
        return _unit_vecs(texts)

    monkeypatch.setattr(ingest_api, "embed_texts", fake_embed)
    outcome = {}

    def run_slow():
        try:
            outcome["out"] = _push([slow_ep])
        except Exception as exc:
            outcome["error"] = exc
        finally:
            started.set()

    thread = threading.Thread(target=run_slow)
    thread.start()
    try:
        assert started.wait(timeout=10), "embed never started"
        fast = _push([fast_ep])
        assert fast["inserted"] == 1 and fast["duplicate"] == 0
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome.get("error") is None, outcome.get("error")
    assert outcome["out"]["duplicate"] == 1 and outcome["out"]["inserted"] == 0
    row, exports = _row(store, fast_ep["stable_id"])
    assert row is not None and row["body"] == "USER: same body" and exports == 1
    assert _row(store, slow_ep["stable_id"])[0] is None
    repeat = _push([fast_ep])
    assert repeat["unchanged"] == 1 and repeat["inserted"] == 0
    fork_again = _push([slow_ep])
    assert fork_again["duplicate"] == 1 and fork_again["inserted"] == 0
    assert calls == [1, 1]


def test_snapshot_episode_pushed_again_is_the_same_row(store, tmp_path):
    """An episode the git snapshot import already loaded comes back with the
    same stable id and hash: no new row, no body change, no re-export."""
    import json

    from brain import ingest, pg_loader, push

    path = tmp_path / "projects" / "-home-a-src-brain" / "sess-9.jsonl"
    path.parent.mkdir(parents=True)
    lines = [
        {"type": "user", "message": {"content": "where is the widget"}, "uuid": "u1",
         "timestamp": "2026-10-18T10:00:00Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "in project-alpha"}]},
         "uuid": "a1", "timestamp": "2026-10-18T10:00:05Z"},
    ]
    common = {"sessionId": "sess-9", "cwd": "/home/a/src/brain", "gitBranch": "main", "isSidechain": False}
    path.write_text("\n".join(json.dumps({**ln, **common}) for ln in lines) + "\n", encoding="utf-8")
    (wire,) = push.episodes_for(path)
    relpath = f"episodes/2026/10/{wire['stable_id']}.md"
    raw = ingest._render_episode_file(
        wire["stable_id"], wire["content_hash"], wire["session_id"], wire["uuid"], wire["ts"],
        wire["cwd"], wire["git_branch"], wire["is_sidechain"], wire["text"],
    ).encode("utf-8")
    doc = pg_loader.build_doc(tmp_path, relpath, {}, "", raw=raw)
    with store.connection() as conn:
        pg_loader.upsert_doc(conn, doc)
        conn.commit()
    before, exports = _row(store, wire["stable_id"])
    out = _push([{k: v for k, v in wire.items() if k != "content_hash"}])
    assert out["inserted"] == 0 and out["updated"] == 0 and out["duplicate"] == 0
    assert out["results"][wire["stable_id"]] in ("unchanged", "revectored")
    after, exports_after = _row(store, wire["stable_id"])
    assert after["id"] == before["id"] and after["body"] == before["body"]
    assert after["content_hash"] == before["content_hash"] == wire["content_hash"]
    assert exports_after == exports
