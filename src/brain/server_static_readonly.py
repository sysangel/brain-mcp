"""Static MCP stdio entrypoint exposing only recall and get_episode.

Unlike brain.server_readonly, startup does not launch background index maintenance:
no git pull, ingest, or reflect. It serves the existing index as-is. This is a
read-only *tool surface*, not a process-wide no-write guarantee: the reused
recall/get_episode implementations may write usage telemetry or SQLite caches.
"""

from __future__ import annotations

from fastmcp import FastMCP

from .server import get_episode, recall

mcp = FastMCP("brain")
mcp.tool(recall)
mcp.tool(get_episode)


def main() -> None:
    mcp.run()  # stdio transport; no startup maintenance thread


if __name__ == "__main__":
    main()
