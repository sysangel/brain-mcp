"""brain-push: parser parity with local ingest, state tracking, locking, retries, hook mode."""

from __future__ import annotations

import io
import json

import pytest

from brain import client, db, embed, ingest, push, sync


def _line(kind, session, uuid, ts, text):
    if kind == "user":
        message = {"content": text}
    else:
        message = {"content": [{"type": "text", "text": text}]}
    return json.dumps({
        "type": kind,
        "message": message,
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": "/home/a/src/brain",
        "gitBranch": "main",
        "isSidechain": False,
    })


def _write(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_CLIENT_DIR", str(tmp_path / "client"))
    monkeypatch.delenv("BRAIN_DEVICE_TOKEN", raising=False)
    monkeypatch.delenv("BRAIN_DEVICE_TOKEN_FILE", raising=False)
    return tmp_path


def _transcript(tmp_path, extra=()):
    path = tmp_path / "projects" / "-home-a-src-brain" / "sess-1.jsonl"
    _write(path, [
        _line("user", "sess-1", "u1", "2026-10-18T10:00:00Z", "first question about widgets"),
        _line("assistant", "sess-1", "a1", "2026-10-18T10:00:05Z", "first answer"),
        _line("user", "sess-1", "u2", "2026-10-18T10:01:00Z", "second question"),
        *extra,
    ])
    return path


def test_wire_episodes_match_local_ingest_rows(tmp_path, monkeypatch):
    """The pushed id, hash, and text are exactly what brain-ingest stores."""
    path = _transcript(tmp_path)
    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "find_transcripts", lambda: [path])
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    monkeypatch.setattr(ingest, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(ingest, "find_episode_files", lambda: [])
    monkeypatch.setattr(ingest, "commit_episode_files", lambda paths, log=None: False)
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    monkeypatch.setattr(embed, "available", lambda: False)
    ingest.run_ingest(full=True, log=lambda _m: None)
    conn = db.connect()
    try:
        local = {
            r["stable_id"]: (r["content_hash"], r["text"])
            for r in conn.execute("select stable_id, content_hash, text from episodes")
        }
    finally:
        conn.close()
    wire = {e["stable_id"]: (e["content_hash"], e["text"]) for e in push.episodes_for(path)}
    assert wire == local and len(wire) == 2


class FakeServer:
    def __init__(self, drop=()):
        self.batches = []
        self.drop = set(drop)

    def __call__(self, payload):
        self.batches.append(payload["episodes"])
        results = {e["stable_id"]: "inserted" for e in payload["episodes"] if e["stable_id"] not in self.drop}
        return {"results": results, "rejected": []}


def _run(path, state, server, *, full=False, since="all"):
    pusher = push.Pusher(state, since=push.parse_since(since), full=full, send=server, save=lambda _s: None)
    return pusher.run([path])


def test_state_skips_sent_and_resends_only_growth(tmp_path):
    path = _transcript(tmp_path)
    state = {"files": {}}
    server = FakeServer()
    out = _run(path, state, server)
    assert out["sent"] == 2 and len(server.batches) == 1
    assert all("content_hash" not in e for e in server.batches[0])
    assert _run(path, state, server)["changed"] == 0  # same mtime and size
    assert _run(path, state, server, full=True)["sent"] == 0  # backfill: hashes match
    _transcript(tmp_path, [_line("assistant", "sess-1", "a2", "2026-10-18T10:01:09Z", "second answer")])
    out = _run(path, state, server)
    assert out["sent"] == 1
    assert server.batches[-1][0]["text"].endswith("second answer")


def test_unacknowledged_episode_keeps_file_dirty(tmp_path):
    path = _transcript(tmp_path)
    eps = push.episodes_for(path)
    state = {"files": {}}
    out = _run(path, state, FakeServer(drop={eps[1]["stable_id"]}))
    assert out["sent"] == 1
    assert "mtime" not in state["files"][str(path)]
    retry = FakeServer()
    assert _run(path, state, retry)["sent"] == 1
    assert [e["stable_id"] for e in retry.batches[0]] == [eps[1]["stable_id"]]
    assert "mtime" in state["files"][str(path)]


def test_since_floor_skips_older_episodes(tmp_path):
    path = _transcript(tmp_path)
    server = FakeServer()
    out = _run(path, {"files": {}}, server, since="2026-10-18T10:00:30Z")
    assert out["sent"] == 1 and out["skipped"] == 1


def test_state_file_round_trip_is_atomic(home):
    state = {"version": 1, "files": {"a": {"mtime": 1.0, "size": 2, "sent": {"ep_x": "h"}}}}
    push.save_state(state)
    assert push.load_state() == state
    assert not list(push.state_path().parent.glob("*.tmp"))
    push.state_path().write_text("{torn", encoding="utf-8")
    assert push.load_state()["files"] == {}


def test_lock_held_queues_the_waiting_files_for_the_holder(home):
    """A hook that loses the lock race gets its own transcript pushed by the
    holder, not just a repeat of the holder's file list."""
    from brain import _flock as fcntl

    lock, pending = push._lock_paths()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert push.run_locked(lambda f, full: 0, ["a.jsonl"]) is None
        fcntl.flock(fh, fcntl.LOCK_UN)
    assert pending.exists()
    pending.unlink()
    runs = []

    def work(files, full):
        runs.append((files, full))
        if len(runs) == 1:
            # Two hooks fire mid-push, one for a file the holder never saw.
            push._leave_request(pending, ["other.jsonl"])
            push._leave_request(pending, ["mine.jsonl", "other.jsonl"])
        return 0

    assert push.run_locked(work, ["mine.jsonl"], full=True) == 0
    assert runs == [(["mine.jsonl"], True), (["other.jsonl", "mine.jsonl"], False)]
    assert not pending.exists()


def test_waiting_catch_up_request_becomes_a_full_pass(home):
    _lock, pending = push._lock_paths()
    pending.parent.mkdir(parents=True, exist_ok=True)
    push._leave_request(pending, ["a.jsonl"])
    push._leave_request(pending, None)
    runs = []
    assert push.run_locked(lambda f, full: runs.append(f) or 0, ["b.jsonl"]) == 0
    assert runs == [["b.jsonl"], None]


def test_refused_episodes_are_not_cached_as_sent(tmp_path):
    """A wrong-profile refusal is skipped by catch-up but retried by backfill."""
    path = _transcript(tmp_path)
    eps = push.episodes_for(path)
    refused_id = eps[1]["stable_id"]

    class Refusing(FakeServer):
        def __call__(self, payload):
            out = super().__call__(payload)
            out["results"].pop(refused_id, None)
            if any(e["stable_id"] == refused_id for e in payload["episodes"]):
                out["rejected"] = [{"stable_id": refused_id, "error": "this device token cannot write work"}]
            return out

    state = {"files": {}}
    out = _run(path, state, Refusing())
    assert out["sent"] == 1 and out["rejected"] == 1
    entry = state["files"][str(path)]
    assert refused_id not in entry["sent"] and refused_id in entry["rejected"]
    assert "mtime" in entry  # nothing left to retry on a plain catch-up
    path.touch()  # mtime moves; a catch-up rereads but still skips the refusal
    assert _run(path, state, FakeServer())["sent"] == 0
    retry = FakeServer()
    out = _run(path, state, retry, full=True)  # --backfill after a better token
    assert out["sent"] == 1 and [e["stable_id"] for e in retry.batches[0]] == [refused_id]
    assert refused_id in entry["sent"] and refused_id not in entry["rejected"]


def test_server_forbidden_outcome_is_treated_as_refused(tmp_path):
    path = _transcript(tmp_path)
    eps = push.episodes_for(path)

    def server(payload):
        return {"results": {e["stable_id"]: "forbidden" for e in payload["episodes"]}, "rejected": []}

    state = {"files": {}}
    out = _run(path, state, server)
    assert out["rejected"] == 2 and out["sent"] == 0
    assert not state["files"][str(path)].get("sent")
    assert set(state["files"][str(path)]["rejected"]) == {e["stable_id"] for e in eps}


def test_retry_backs_off_then_gives_up_on_4xx(monkeypatch):
    calls = []

    def flaky(path, payload, *, token, timeout):
        calls.append(path)
        if len(calls) < 3:
            raise client.ClientError(503, "busy")
        return {"ok": True}

    monkeypatch.setattr(client, "post_json", flaky)
    waits = []
    assert client.post_with_retry("/x", {}, token="t", sleep=waits.append) == {"ok": True}
    assert len(waits) == 2

    def denied(path, payload, *, token, timeout):
        raise client.ClientError(401, "revoked")

    monkeypatch.setattr(client, "post_json", denied)
    with pytest.raises(client.ClientError) as exc:
        client.post_with_retry("/x", {}, token="t", sleep=waits.append)
    assert exc.value.status == 401 and len(waits) == 2


def test_token_from_env_or_home_file(home, monkeypatch):
    assert client.load_token() == ""
    (home / "client").mkdir()
    (home / "client" / "device-token").write_text("brn_dev_fromfile\n", encoding="utf-8")
    assert client.load_token() == "brn_dev_fromfile"
    monkeypatch.setenv("BRAIN_DEVICE_TOKEN", "brn_dev_fromenv")
    assert client.load_token() == "brn_dev_fromenv"


def test_hook_mode_detaches_and_exits_zero(home, monkeypatch):
    spawned = []
    monkeypatch.setattr(push, "_spawn_detached", spawned.append)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": "C:/t/s.jsonl"})))
    assert push.main(["--hook"]) == 0
    assert spawned == []  # no token: silent no-op
    monkeypatch.setenv("BRAIN_DEVICE_TOKEN", "brn_dev_x")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": "C:/t/s.jsonl"})))
    assert push.main(["--hook"]) == 0
    assert spawned == [["--file", "C:/t/s.jsonl"]]
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert push.main(["--hook"]) == 0


def test_hook_transcript_includes_subagents(tmp_path):
    path = _transcript(tmp_path)
    side = path.with_suffix("") / "subagents" / "agent-1.jsonl"
    _write(side, [_line("user", "sess-1", "s1", "2026-10-18T10:02:00Z", "side task")])
    assert push.transcripts_for_hook(path) == [path, side]


def test_device_token_cli_writes_the_file_not_stdout(home, capsys):
    from brain import device_cli

    calls = []

    def fake(name, args):
        calls.append((name, args))
        return {
            "id": 7, "token": "brn_dev_" + "a" * 43, "prefix": "brn_dev_aaaaaa",
            "device": args["device"], "scopes": ["ingest", "hook-read"], "profiles": ["work"],
        }

    assert device_cli.main(["create", "--device", "laptop"], call=fake) == 0
    out = capsys.readouterr().out
    assert "brn_dev_" + "a" * 43 not in out and "saved to" in out
    assert client.load_token() == "brn_dev_" + "a" * 43
    assert device_cli.main(["create", "--device", "laptop"], call=fake) == 2  # no silent overwrite
    assert calls == [("device_token_create", {"device": "laptop", "profiles": "", "scopes": ""})]


def test_device_token_force_revokes_the_previous_token(home, capsys):
    from brain import device_cli
    from brain.device_tokens import hash_token

    issued = []
    calls = []

    def fake(name, args):
        calls.append((name, args))
        if name == "device_token_create":
            token = "brn_dev_" + ("ab" if not issued else "cd") + "e" * 41
            issued.append(token)
            return {
                "id": len(issued),
                "token": token,
                "prefix": token[:14],
                "device": args["device"],
                "scopes": ["ingest", "hook-read"],
                "profiles": ["work"],
            }
        if name == "device_token_revoke_hash":
            return {"revoked": 1}
        return {"error": name}

    assert device_cli.main(["create", "--device", "laptop"], call=fake) == 0
    assert device_cli.main(["create", "--device", "laptop", "--force"], call=fake) == 0
    out = capsys.readouterr().out
    assert issued[0] not in out and issued[1] not in out
    assert "revoked previous token 1" in out
    assert client.load_token() == issued[1]
    assert calls[1] == ("device_token_revoke_hash", {"token_hash": hash_token(issued[0])})
    assert calls[2][0] == "device_token_create"
