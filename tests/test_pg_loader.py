"""Git import: committed files only, archive status, vector reuse by embed_space."""

from __future__ import annotations

import sqlite3
import struct
import subprocess
from pathlib import Path

from brain.embedconf import LEGACY_EMBED_SPACE
from brain.ids import memory_stable_id_v1
from brain.pg_loader import (
    legacy_memory_path,
    load_repo,
    parse_alias_roots,
    summary_content_hash,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp: Path) -> Path:
    root = tmp / "repo"
    root.mkdir()
    (root / "memories").mkdir()
    (root / "memories" / "archive").mkdir()
    (root / "proposals").mkdir()
    (root / "episodes" / "2026" / "10").mkdir(parents=True)
    (root / "memories" / "widget.md").write_text(
        "---\n"
        "name: widget\n"
        'description: "alpha widget"\n'
        "project_id: project-alpha\n"
        "status: active\n"
        "recall_enabled: true\n"
        "metadata:\n"
        "  type: reference\n"
        "---\n\n"
        "The alpha widget lives in project-alpha.\n",
        encoding="utf-8",
    )
    (root / "memories" / "archive" / "old.md").write_text(
        "---\nname: old\ndescription: \"retired\"\nmetadata:\n  type: project\n---\n\nRetired fact.\n",
        encoding="utf-8",
    )
    (root / "proposals" / "move-db.md").write_text(
        "# Move the store\n\nUse Supabase as the canonical database.\n",
        encoding="utf-8",
    )
    (root / "episodes" / "2026" / "10" / "secret.md").write_text(
        "not committed and must not load\n",
        encoding="utf-8",
    )
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "memories", "proposals")
    _git(root, "commit", "-m", "import fixture")
    return root


def test_loads_committed_files_and_skips_untracked(pg, tmp_path):
    root = _repo(tmp_path)
    with pg.connection() as conn:
        stats = load_repo(conn, root, sqlite_path=None, accept_space=LEGACY_EMBED_SPACE)
    assert stats["by_kind"]["memory"] == 2
    assert stats["by_kind"]["proposal"] == 1
    assert stats["vectors"] == 0
    with pg.connection() as conn:
        rows = conn.execute(
            "select stable_id, kind, status, project_id, embedding is null as missing "
            "from brain.documents order by kind, relpath"
        ).fetchall()
        conn.commit()
    kinds = {row[1] for row in rows}
    assert "episode" not in kinds
    archived = [row for row in rows if row[2] == "archived"]
    assert len(archived) == 1
    active = [row for row in rows if row[1] == "memory" and row[2] == "active"]
    assert active[0][3] == "project-alpha"
    assert all(row[4] for row in rows)
    result = pg.recall("alpha widget")
    assert len(result["hits"]) == 1
    assert pg.recall("Retired")["hits"] == []


def test_reuses_vectors_only_when_embed_space_matches(pg, tmp_path):
    root = _repo(tmp_path)
    with pg.connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        load_repo(conn, root, sqlite_path=None, accept_space=LEGACY_EMBED_SPACE)
        row = conn.execute(
            "select content_hash from brain.documents where relpath = 'memories/widget.md'"
        ).fetchone()
        conn.commit()
    digest = row["content_hash"]
    blob = struct.pack("<256f", *([0.5] * 256))
    cache = tmp_path / "brain.db"
    sq = sqlite3.connect(cache)
    sq.execute("create table meta (key text primary key, value text)")
    sq.execute(
        "create table embedding_cache (kind text, content_hash text, embedding blob)"
    )
    sq.execute(
        "insert into embedding_cache values ('memory', ?, ?)",
        (digest, blob),
    )
    sq.execute("insert into meta values ('embed_space', ?)", ("openai:other:256",))
    sq.commit()
    sq.close()
    with pg.connection() as conn:
        mismatch = load_repo(
            conn, root, sqlite_path=cache, accept_space=LEGACY_EMBED_SPACE
        )
    assert mismatch["vectors"] == 0

    sq = sqlite3.connect(cache)
    sq.execute("update meta set value = ? where key = 'embed_space'", (LEGACY_EMBED_SPACE,))
    sq.commit()
    sq.close()
    with pg.connection() as conn:
        matched = load_repo(
            conn, root, sqlite_path=cache, accept_space=LEGACY_EMBED_SPACE
        )
        vec = conn.execute(
            "select embedding is not null from brain.documents where relpath = 'memories/widget.md'"
        ).fetchone()[0]
        conn.commit()
    assert matched["vectors"] >= 1
    assert vec is True
    # Second load is idempotent.
    with pg.connection() as conn:
        again = load_repo(conn, root, sqlite_path=cache, accept_space=LEGACY_EMBED_SPACE)
        count = conn.execute(
            "select count(*) from brain.documents where relpath = 'memories/widget.md'"
        ).fetchone()[0]
        conn.commit()
    assert again["upserted"] >= 1
    assert count == 1


def test_load_reads_committed_blobs_not_the_worktree(pg, tmp_path):
    root = _repo(tmp_path)
    memory = root / "memories" / "widget.md"
    committed = memory.read_text(encoding="utf-8")
    memory.write_text(committed + "\nUNCOMMITTED private draft.\n", encoding="utf-8")
    staged = root / "memories" / "staged-secret.md"
    staged.write_text(
        "---\nname: staged\ndescription: \"no\"\nmetadata:\n  type: reference\n---\n\n"
        "never committed\n",
        encoding="utf-8",
    )
    _git(root, "add", "memories/staged-secret.md")
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    with pg.connection() as conn:
        load_repo(conn, root, sqlite_path=None, accept_space=LEGACY_EMBED_SPACE)
        bodies = {
            row[0]: row[1]
            for row in conn.execute(
                "select relpath, body from brain.documents"
            ).fetchall()
        }
        recorded = conn.execute(
            "select value from brain.meta where key = 'ingested_git_head'"
        ).fetchone()[0]
        conn.commit()
    assert recorded == head
    assert "UNCOMMITTED" not in bodies["memories/widget.md"]
    assert "alpha widget" in bodies["memories/widget.md"]
    assert "memories/staged-secret.md" not in bodies


def test_windows_alias_root_keeps_drive_letter_and_spelling(pg, tmp_path, monkeypatch):
    monkeypatch.setenv("BRAIN_MEMORY_ALIAS_ROOTS", r"C:\review\brain;D:\other\brain")
    assert parse_alias_roots(r"C:\review\brain") == [r"C:\review\brain"]
    assert parse_alias_roots("/home/a:/home/b") == ["/home/a", "/home/b"]
    assert parse_alias_roots("C:\\review\\brain:/home/a") == [
        r"C:\review\brain",
        "/home/a",
    ]
    joined = legacy_memory_path(r"C:\review\brain", "memories/widget.md")
    assert joined == r"C:\review\brain\memories\widget.md"
    root = _repo(tmp_path)
    with pg.connection() as conn:
        load_repo(conn, root, sqlite_path=None, accept_space=LEGACY_EMBED_SPACE)
        aliases = {
            row[0]
            for row in conn.execute(
                "select old_id from brain.memory_id_aliases"
            ).fetchall()
        }
        conn.commit()
    assert memory_stable_id_v1(r"C:\review\brain\memories\widget.md") in aliases
    assert memory_stable_id_v1(r"D:\other\brain\memories\widget.md") in aliases
    assert memory_stable_id_v1("C") not in aliases


def test_summary_hash_is_stable():
    a = summary_content_hash("s", "2026-10-01", "brain", "/tmp/brain", "host", "d", "body")
    b = summary_content_hash("s", "2026-10-01", "brain", "/tmp/brain", "host", "d", "body")
    assert a == b
    assert a != summary_content_hash("s", "2026-10-01", "brain", "/tmp/brain", "host", "d", "other")
