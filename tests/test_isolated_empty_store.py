"""External-client empty-store isolation using real config, ingest, and SQLite.

All canaries are synthetic and live under tmp_path; no real user paths are read.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("override_projects", [True, False])
def test_external_client_empty_store_ignores_default_transcripts_and_other_store(
    tmp_path, override_projects,
):
    home = tmp_path / "home"
    for relative in (".claude/projects", ".claude-work/projects"):
        root = home / relative / "synthetic-session"
        root.mkdir(parents=True)
        (root / "private-canary.jsonl").write_text(json.dumps({
            "type": "user",
            "message": {"content": "synthetic transcript must stay out of the index"},
            "sessionId": "synthetic-session",
            "uuid": "synthetic-uuid",
            "timestamp": "2026-01-01T00:00:00Z",
            "cwd": "/synthetic-private-project",
        }) + "\n", encoding="utf-8")

    private_memory = tmp_path / "private-brain" / "memories"
    private_memory.mkdir(parents=True)
    (private_memory / "private-canary.md").write_text(
        "---\nname: private-canary\ndescription: must not index\n---\nsynthetic secret\n",
        encoding="utf-8",
    )
    store = tmp_path / "external-store"
    store.mkdir()
    empty_projects = tmp_path / "empty-claude-projects"
    empty_projects.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "BRAIN_DIR": str(store),
        "BRAIN_CLAUDE_PROJECTS": str(empty_projects),
        "BRAIN_INGEST_SOURCE": "episodes",
        "BRAIN_SYNC": "0",
        "BRAIN_NO_AUTOREFLECT": "1",
        "BRAIN_SCOPE": "personal",
        "FASTEMBED_CACHE_PATH": str(tmp_path / "models"),
    }
    if not override_projects:
        env.pop("BRAIN_CLAUDE_PROJECTS")
    # Config constants are resolved at import time: use a fresh process with
    # the exact environment an external MCP client would pass to the server.
    script = textwrap.dedent("""
        import json
        from brain import config, db, ingest

        stats = ingest.run_ingest(full=True)
        conn = db.connect()
        result = {
            "store": str(config.BRAIN_DIR),
            "roots": [str(p) for p in config.CLAUDE_PROJECTS_DIRS],
            "source": config.INGEST_SOURCE,
            "stats": stats,
            "counts": {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("files", "episodes", "memories", "summaries")
            },
        }
        conn.close()
        print(json.dumps(result))
    """)
    run = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True,
        text=True, check=True, timeout=30,
    )
    result = json.loads(run.stdout)
    assert result["store"] == str(store)
    assert result["roots"] == (
        [str(empty_projects)] if override_projects else [
            str(home / ".claude/projects"), str(home / ".claude-work/projects")
        ]
    )
    assert result["source"] == "episodes"
    assert result["stats"]["files_seen"] == 0
    assert result["stats"]["episodes_added"] == 0
    assert result["counts"] == dict.fromkeys(
        ("files", "episodes", "memories", "summaries"), 0
    )
    assert (store / "cache" / "brain.db").is_file()
    assert not list(empty_projects.iterdir())
    assert (private_memory / "private-canary.md").is_file()
    assert len(list(home.rglob("private-canary.jsonl"))) == 2
