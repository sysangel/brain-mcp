"""brain — personal memory MCP server.

Source of truth: markdown files in git (memories/ here, plus Claude Code
auto-memory under ~/.claude/projects/*/memory/). Derived index: SQLite in
cache/brain.db — always rebuildable, never committed.
"""

__version__ = "0.1.0"
