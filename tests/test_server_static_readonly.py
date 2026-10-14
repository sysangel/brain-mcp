"""The static read-only entrypoint does not run resident maintenance."""

from __future__ import annotations

import asyncio
import importlib
import tomllib
from pathlib import Path

from brain import server, server_readonly


def test_static_entrypoint_has_an_explicit_script_name() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["brain-server-static-readonly"] == (
        "brain.server_static_readonly:main"
    )
    assert project["project"]["scripts"]["brain-server-readonly"] == (
        "brain.server_readonly:main"
    )


def test_static_entrypoint_registers_only_recall_and_get_episode() -> None:
    static = importlib.import_module("brain.server_static_readonly")
    tools = asyncio.run(static.mcp.get_tools())
    assert set(tools) == {"recall", "get_episode"}
    assert static.recall is server.recall
    assert static.get_episode is server.get_episode
    assert "remember" not in tools


def test_static_startup_runs_stdio_without_starting_maintenance(monkeypatch) -> None:
    static = importlib.import_module("brain.server_static_readonly")
    called = []

    def forbidden(*args, **kwargs):
        raise AssertionError("static entrypoint must not start maintenance")

    monkeypatch.setattr(server, "_ensure_fresh_index", forbidden)
    monkeypatch.setattr(server.sync, "maybe_pull", forbidden)
    monkeypatch.setattr(server, "run_ingest", forbidden)
    monkeypatch.setattr(server.autoreflect, "maybe_trigger", forbidden)
    monkeypatch.setattr(server_readonly.threading, "Thread", forbidden)
    monkeypatch.setattr(static.mcp, "run", lambda: called.append("run"))

    static.main()
    assert called == ["run"]
