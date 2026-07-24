"""FastMCP stdio server exposing ONLY the read-only brain tools: recall + get_episode.

The full server (`brain.server` / `brain-server`) additionally exposes `remember`,
a durable write that commits + pushes to a private remote. A downstream gate that
requires *every* exposed tool to be auto-allowable/read-only (e.g. an external
agent whose tool passthrough auto-allows only read-only tools) can therefore
never wire the full server — the write tool sinks it.

This entry point registers ONLY the two read tools, reusing their implementations
from `brain.server` verbatim, so the whole server is read-only by construction and
passes such a gate. No write tool is registered here; `remember` is unreachable via
this server. Startup mirrors `brain.server.main`: a background daemon thread tops up
the index while `initialize` is answered immediately (stdout is the MCP protocol;
logs go to stderr).
"""

from __future__ import annotations

import threading

from fastmcp import FastMCP

from .server import _ensure_fresh_index, get_episode, recall

mcp = FastMCP("brain")

# Read-only surface only. The same plain functions the full server registers, minus
# `remember` — so this server exposes exactly {recall, get_episode}.
mcp.tool(recall)
mcp.tool(get_episode)


def main() -> None:
    threading.Thread(target=_ensure_fresh_index, name="brain-ingest", daemon=True).start()
    mcp.run()  # stdio transport — answers initialize immediately


if __name__ == "__main__":
    main()
