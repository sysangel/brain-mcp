"""`brain-remember` CLI shim (brain.remember_cli): stdin contract, validation,
and JSON output — WITHOUT ever performing a real write.

Every test either fails validation BEFORE the write path (malformed stdin,
non-object payload, empty fact, bogus type — the last against a tmp_path DB so
no real index is touched) or stubs `_remember_impl` outright (the happy path,
which asserts the payload→kwargs threading and the printed result JSON). No
memory file is created, no git commit runs, nothing is pushed.
"""

from __future__ import annotations

import io
import json
import sys

from brain import db, remember_cli


def _run(monkeypatch, capsys, stdin_text: str) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = remember_cli.main([])
    out = capsys.readouterr().out.strip()
    return code, json.loads(out)


def test_malformed_stdin_json_errors_exit_1(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "{not json")
    assert code == 1
    assert out["error"].startswith("invalid JSON on stdin")


def test_non_object_stdin_errors_exit_1(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "[1, 2, 3]")
    assert code == 1
    assert out["error"] == "stdin must be a JSON object"


def test_empty_fact_errors_before_any_db_or_write(monkeypatch, capsys):
    # Validation must reject BEFORE db.connect — a connect here would
    # materialize an index as a side effect of a bad request.
    def boom():
        raise AssertionError("db.connect must not be called for an empty fact")

    monkeypatch.setattr(remember_cli.db, "connect", boom)
    code, out = _run(monkeypatch, capsys, json.dumps({"fact": "   "}))
    assert code == 1
    assert out["error"] == "fact must be a non-empty string"


def test_missing_fact_errors_exit_1(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, "{}")
    assert code == 1
    assert out["error"] == "fact must be a non-empty string"


def test_bogus_type_returns_error_json_no_write(tmp_path, monkeypatch, capsys):
    # remember() validates `type` before touching locks, files, or git — an
    # isolated tmp DB proves nothing real is touched and no file is created.
    # Capture the real connect BEFORE patching (remember_cli.db IS brain.db,
    # so a lambda calling db.connect would recurse into the patch itself).
    real_connect = db.connect
    monkeypatch.setattr(remember_cli.db, "connect", lambda: real_connect(tmp_path / "brain.db"))
    code, out = _run(monkeypatch, capsys, json.dumps({"fact": "keep this", "type": "bogus"}))
    assert code == 0  # a validated refusal is a rendered outcome, not a usage error
    assert out == {"error": "type must be one of user|feedback|project|reference"}
    assert list(tmp_path.glob("*.md")) == []


def test_happy_path_threads_payload_and_defaults(monkeypatch, capsys):
    # Stub the write impl entirely — the CLI's job is transport, and this pins
    # the payload→kwargs threading + defaulting without any real write.
    captured: dict = {}

    def fake_impl(conn, fact, *, type, name, session, project, force):
        captured.update(
            fact=fact, type=type, name=name, session=session, project=project, force=force
        )
        return {"status": "created", "name": "n", "id": "mem_x", "committed": True}

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(remember_cli, "_remember_impl", fake_impl)
    monkeypatch.setattr(remember_cli.db, "connect", lambda: DummyConn())

    payload = {"fact": "keep this", "name": "my-note", "force": True}
    code, out = _run(monkeypatch, capsys, json.dumps(payload))
    assert code == 0
    assert out["status"] == "created"
    assert captured == {
        "fact": "keep this",
        "type": "project",  # CLI default mirrors the MCP tool default
        "name": "my-note",
        "session": "",
        "project": "",
        "force": True,
    }
