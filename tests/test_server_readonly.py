"""The read-only MCP entry point (brain.server_readonly) exposes ONLY the two read
tools — recall + get_episode — and never the durable-write `remember`.

This pins decision (b): a server whose every exposed tool is read-only, so a
downstream all-tools-must-auto-allow gate (e.g. an external agent's read-only tool passthrough) can wire it,
whereas the full server (which also exposes `remember`) can never qualify. The
tool surface is read via FastMCP's async `get_tools()`; no live index, network, or
child process is touched.
"""

from __future__ import annotations

import asyncio

from brain import server, server_readonly


def _tool_names(mcp) -> set[str]:
    return set(asyncio.run(mcp.get_tools()).keys())


def test_readonly_server_exposes_only_the_two_read_tools() -> None:
    assert _tool_names(server_readonly.mcp) == {"recall", "get_episode"}


def test_readonly_server_never_exposes_remember() -> None:
    # The whole point of the separate entry point: the durable write is absent, so
    # every exposed tool is auto-allowable/read-only.
    assert "remember" not in _tool_names(server_readonly.mcp)


def test_full_server_still_exposes_all_three() -> None:
    # The full server is unchanged by the refactor that made the tool functions
    # importable — it still carries the write tool that sinks it at the gate.
    assert _tool_names(server.mcp) == {"recall", "get_episode", "remember"}


def test_readonly_reuses_the_full_servers_implementations() -> None:
    # Genuine reuse, not a fork: the read-only server registers the SAME plain
    # functions the full server does (identity, not a copy).
    assert server_readonly.recall is server.recall
    assert server_readonly.get_episode is server.get_episode
