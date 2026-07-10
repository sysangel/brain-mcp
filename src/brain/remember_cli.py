"""`brain-remember` — write a durable memory from stdin JSON, without MCP.

A thin transport shim over the exact `remember()` the MCP `remember` tool
delegates to (`remember.remember`), so CLI and MCP writes cannot diverge on
dedup, O_EXCL write, provenance commit, or best-effort push. Mirrors `cli.py`
(`brain-recall`): `uv run` against source means every invocation runs current
code, unlike a long-lived `brain-server` pinned at spawn time.

Reads ONE JSON object from stdin (no flags), e.g.:

    {"fact": "...", "type": "project", "name": "", "session": "",
     "project": "", "force": false}

Only `fact` is required; every other field defaults exactly as the MCP tool.
Prints the result dict as JSON on stdout. Exit 0 on a successful write AND on a
dedup refusal (`status:"duplicate"`) — both are valid outcomes the caller
renders and reasons about. Exit nonzero only on a usage/IO error (bad stdin),
with the diagnostic in the JSON `error` field so the caller can surface it.
"""

from __future__ import annotations

import json
import sys

from . import db
from .remember import remember as _remember_impl


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON on stdin: {e}"}))
        return 1
    if not isinstance(payload, dict):
        print(json.dumps({"error": "stdin must be a JSON object"}))
        return 1

    fact = payload.get("fact", "")
    if not isinstance(fact, str) or not fact.strip():
        print(json.dumps({"error": "fact must be a non-empty string"}))
        return 1

    # Same writable connection the MCP tool opens. Unlike the read path we do NOT
    # guard on DB existence: a write legitimately materializes the index, exactly
    # as server.remember does (remember() indexes the new file into `conn`).
    conn = db.connect()
    try:
        result = _remember_impl(
            conn,
            fact,
            type=payload.get("type") or "project",
            name=payload.get("name") or "",
            session=payload.get("session") or "",
            project=payload.get("project") or "",
            force=bool(payload.get("force", False)),
        )
    finally:
        conn.close()

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
