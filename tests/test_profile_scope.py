"""Profile scope (BRAIN_SCOPE + `metadata.scope`): stamping on write, parsing
on ingest, and filtering on every read path.

Semantics under test:
  * BRAIN_SCOPE unset = neutral — reads unfiltered (byte-identical to the
    pre-scope behavior), writes unstamped.
  * BRAIN_SCOPE set = this process's profile — reads return shared rows
    (scope IS NULL) plus that profile's; non-project memory writes default-stamp
    it; summaries are never filtered.
  * A scope-less file is shared forever: nothing here migrates or backfills.

Everything runs against tmp DBs / tmp dirs with monkeypatched module constants
(the env var is read once at config import, so the tests patch the constant the
way the rest of the suite patches DB_PATH / EPISODE_EXCLUDE). The embedder is
stubbed absent throughout, so recall runs its FTS leg only: deterministic and
network-free.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from brain import db, embed, hook, ingest, remember, sync
from brain.config import EMBED_DIM


# ------------------------------------------------------------- fixtures ---

def _seed_memory(conn, mem_id: int, stable_id: str, text: str, scope) -> None:
    conn.execute(
        "INSERT INTO memories(id, stable_id, content_hash, embedded_hash, name,"
        " description, mtype, path, ts, text, scope)"
        " VALUES (?, ?, 'h', 'h', ?, '', 'note', ?, '2026-07-05', ?, ?)",
        (mem_id, stable_id, stable_id, f"{stable_id}.md", text, scope),
    )


def _seed_episode(conn, ep_id: int, stable_id: str, text: str, scope) -> None:
    conn.execute(
        "INSERT INTO episodes(id, stable_id, content_hash, embedded_hash,"
        " session_id, uuid, ts, cwd, git_branch, is_sidechain, source_file,"
        " text, scope)"
        " VALUES (?, ?, 'h', 'h', 's1', ?, '2026-07-05T00:00:00Z', '/proj',"
        " 'main', 0, 'x.jsonl', ?, ?)",
        (ep_id, stable_id, stable_id, text, scope),
    )


def _seed_summary(conn, text: str) -> None:
    conn.execute(
        "INSERT INTO summaries(session_id, date, project, cwd, host, description,"
        " path, ts, text)"
        " VALUES ('s1', '2026-07-05', 'proj', '/proj', 'h', '', 'sum.md',"
        " '2026-07-05', ?)",
        (text,),
    )


def _corpus(tmp_path, monkeypatch):
    """One shared / one personal / one work row for memories AND episodes, plus
    an (always-shared) summary. Every row carries the query token `quorple`."""
    monkeypatch.setattr(embed, "available", lambda: False)  # FTS-only, no network
    conn = db.connect(tmp_path / "brain.db")
    _seed_memory(conn, 1, "mem_shared0000001", "quorple shared memory", None)
    _seed_memory(conn, 2, "mem_personal00001", "quorple personal memory", "personal")
    _seed_memory(conn, 3, "mem_work000000001", "quorple work memory", "work")
    _seed_episode(conn, 1, "ep_shared00000001", "quorple shared episode", None)
    _seed_episode(conn, 2, "ep_personal000001", "quorple personal episode", "personal")
    _seed_episode(conn, 3, "ep_work0000000001", "quorple work episode", "work")
    _seed_summary(conn, "quorple digest")
    conn.commit()
    return conn


def _ids(result) -> set[str]:
    return {h["id"] for h in result["hits"]}


# ------------------------------------------------- writer stamping matrix ---

@pytest.mark.parametrize(
    "env_scope, mtype, explicit, expected",
    [
        # Neutral process: nothing is ever stamped by default.
        (None, "project", "", None),
        (None, "reference", "", None),
        # Profile process: non-project writes inherit it, project notes do not
        # (project memories and the *-state.md notes stay shared by design).
        ("work", "project", "", None),
        ("work", "reference", "", "work"),
        ("personal", "user", "", "personal"),
        ("personal", "project", "", None),
        # Explicit 'shared' refuses the stamp even under a profile.
        ("work", "reference", "shared", None),
        (None, "reference", "shared", None),
        # Explicit profile wins over the env — including on a project memory.
        ("work", "reference", "personal", "personal"),
        (None, "project", "work", "work"),
        ("personal", "project", "work", "work"),
        # Case/whitespace tolerant.
        (None, "reference", " Work ", "work"),
    ],
)
def test_resolve_scope_matrix(monkeypatch, env_scope, mtype, explicit, expected):
    monkeypatch.setattr(remember, "PROFILE_SCOPE", env_scope)
    assert remember.resolve_scope(explicit.strip().lower(), mtype) == expected


def test_resolve_scope_uses_request_profile_when_process_scope_is_unset(monkeypatch):
    """Hosted remember: BRAIN_SCOPE is unset; the signed-in profile is the default."""
    from brain.profile_ctx import override_profile

    monkeypatch.setattr(remember, "PROFILE_SCOPE", None)
    with override_profile("personal"):
        assert remember.resolve_scope("", "reference") == "personal"
        assert remember.resolve_scope("", "user") == "personal"
        assert remember.resolve_scope("", "project") is None
        assert remember.resolve_scope("shared", "reference") is None
    with override_profile("work"):
        assert remember.resolve_scope("", "feedback") == "work"
    with override_profile(None):
        assert remember.resolve_scope("", "reference") is None


def test_invalid_scope_is_a_clear_error_before_any_write(monkeypatch):
    # Validation precedes the locks, the file write, and git — so a bogus value
    # is refused without a connection (conn is never touched).
    monkeypatch.setattr(remember, "PROFILE_SCOPE", "work")
    out = remember.remember(None, "a durable fact", type="reference", scope="secret")
    assert out == {"error": "scope must be one of shared|personal|work"}


def test_format_memory_md_stamps_only_when_scoped(tmp_path):
    scoped = remember.format_memory_md("s", "desc", "reference", "body", "work")
    assert "metadata:\n  type: reference\n  scope: work\n---\n" in scoped
    # Shared renders exactly the pre-scope bytes — no empty/placeholder field.
    shared = remember.format_memory_md("s", "desc", "reference", "body", None)
    assert "scope" not in shared
    assert shared == remember.format_memory_md("s", "desc", "reference", "body")


# ------------------------------------------------ memory parser round-trip ---

@pytest.mark.parametrize(
    "line, expected",
    [
        ("  scope: work\n", "work"),
        ("  scope: personal\n", "personal"),
        ('  scope: "work"\n', "work"),
        ("  scope: elsewhere\n", None),  # unrecognized value = shared
        ("", None),                       # absent = shared (every legacy memory)
    ],
)
def test_parse_memory_reads_metadata_scope(tmp_path, line, expected):
    path = tmp_path / "note.md"
    path.write_text(
        "---\nname: note\ndescription: \"a note\"\nmetadata:\n  type: reference\n"
        f"{line}---\n\nthe body\n",
        encoding="utf-8", newline="\n",
    )
    mem = ingest.parse_memory(path)
    assert mem["scope"] == expected
    assert mem["mtype"] == "reference"  # the sibling metadata key still parses
    assert mem["text"] == "the body"


def test_memory_write_parse_index_round_trip(tmp_path, monkeypatch):
    """The writer's bytes parse back to the same scope and land in the DB."""
    monkeypatch.setattr(embed, "available", lambda: False)
    conn = db.connect(tmp_path / "brain.db")
    for slug, scope in (("scoped", "work"), ("plain", None)):
        path = tmp_path / f"{slug}.md"
        path.write_text(
            remember.format_memory_md(slug, "a desc", "reference", "the fact", scope),
            encoding="utf-8", newline="\n",
        )
        assert ingest.parse_memory(path)["scope"] == scope
        sid = ingest.index_memory_file(conn, path)
        row = conn.execute(
            "SELECT scope FROM memories WHERE stable_id = ?", (sid,)
        ).fetchone()
        assert row["scope"] == scope
    conn.close()


def test_scope_participates_in_the_memory_content_hash():
    args = ("name", "desc", "note", "text", "2026-07-05")
    base = db.memory_content_hash(*args)
    # Shared memories keep their pre-scope hash exactly (no corpus-wide churn)…
    assert db.memory_content_hash(*args, None) == base
    # …but adding or changing the stamp re-indexes the row.
    assert db.memory_content_hash(*args, "work") != base
    assert db.memory_content_hash(*args, "work") != db.memory_content_hash(*args, "personal")


# ----------------------------------------------- episode parser round-trip ---

def test_episode_file_frontmatter_round_trip(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    text = "USER: hi\n\nASSISTANT: hello"
    chash = db.episode_content_hash("2026-07-01T00:00:00Z", "/proj", "main", 0, text)
    row = {
        "stable_id": "ep_abcdef0123456789", "content_hash": chash, "session_id": "s1",
        "uuid": "u1", "ts": "2026-07-01T00:00:00Z", "cwd": "/proj",
        "git_branch": "main", "is_sidechain": 0, "text": text, "scope": "work",
    }
    path = ingest.emit_episode_file(row)
    parsed = ingest.parse_episode_file(path)
    assert parsed["scope"] == "work"
    assert parsed["text"] == text
    # scope is NOT in content_hash: an existing file keeps verifying once the
    # field appears, and the VM's verify-on-parse still passes.
    assert db.episode_content_hash(
        parsed["ts"], parsed["cwd"], parsed["git_branch"],
        parsed["is_sidechain"], parsed["text"],
    ) == parsed["content_hash"]

    # A shared episode renders the pre-scope bytes: no field, no git churn.
    shared = dict(row, stable_id="ep_beefbeefbeefbeef", scope=None)
    shared_bytes = ingest.emit_episode_file(shared).read_bytes()
    assert b"scope" not in shared_bytes
    # A row mapping with no scope key at all (pre-scope caller) still emits.
    legacy = {k: v for k, v in shared.items() if k != "scope"}
    legacy["stable_id"] = "ep_cafecafecafecafe"
    assert b"scope" not in ingest.emit_episode_file(legacy).read_bytes()

    # An unrecognized frontmatter value is shared, not an error.
    bogus = tmp_path / "bogus.md"
    bogus.write_text(
        ingest._render_episode_file(
            "ep_0000000000000001", chash, "s1", "u1", "2026-07-01T00:00:00Z",
            "/proj", "main", 0, text,
        ).replace("is_sidechain: 0", 'is_sidechain: 0\nscope: "elsewhere"'),
        encoding="utf-8", newline="\n",
    )
    assert ingest.parse_episode_file(bogus)["scope"] is None


# ------------------------------------------------- ingest: scan-root scope ---

def _wire_ingest(monkeypatch, tmp_path, transcripts, episodes_dir):
    """run_ingest against a tmp DB + tmp episodes/, no git, no network."""
    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "EPISODES_DIR", episodes_dir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    monkeypatch.setattr(ingest, "find_transcripts", lambda: list(transcripts))
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    monkeypatch.setattr(ingest, "commit_episode_files", lambda paths, log=None: False)
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    monkeypatch.setattr(sync, "enabled", lambda: False)
    monkeypatch.setattr(embed, "available", lambda: False)
    return dbp


def _transcript(root: str, tmp_path, name: str, session: str, uuid: str):
    path = tmp_path / root / "projects" / "-proj" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "type": "user", "message": {"content": f"prompt {uuid}"},
            "sessionId": session, "uuid": uuid, "timestamp": "2026-07-01T00:00:00Z",
            "cwd": "/proj", "gitBranch": "main", "isSidechain": False,
        }) + "\n",
        encoding="utf-8", newline="\n",
    )
    return path


def test_transcript_scope_from_scan_root(tmp_path, monkeypatch):
    roots = [tmp_path / ".claude" / "projects", tmp_path / ".claude-work" / "projects"]
    monkeypatch.setattr(ingest, "CLAUDE_PROJECTS_DIRS", roots)
    personal = _transcript(".claude", tmp_path, "a", "s1", "u1")
    work = _transcript(".claude-work", tmp_path, "b", "s2", "u2")
    assert ingest.transcript_scope(personal) == "personal"
    assert ingest.transcript_scope(work) == "work"
    # A root that is neither config dir is unclassifiable -> shared, never a guess.
    monkeypatch.setattr(ingest, "CLAUDE_PROJECTS_DIRS", [tmp_path / "elsewhere"])
    other = tmp_path / "elsewhere" / "-proj" / "c.jsonl"
    assert ingest.transcript_scope(other) is None


def test_ingest_stamps_episodes_and_emitted_files_from_scan_root(tmp_path, monkeypatch):
    roots = [tmp_path / ".claude" / "projects", tmp_path / ".claude-work" / "projects"]
    personal = _transcript(".claude", tmp_path, "a", "s1", "u1")
    work = _transcript(".claude-work", tmp_path, "b", "s2", "u2")
    edir = tmp_path / "episodes"
    dbp = _wire_ingest(monkeypatch, tmp_path, [personal, work], edir)
    monkeypatch.setattr(ingest, "CLAUDE_PROJECTS_DIRS", roots)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 2

    conn = db.connect(dbp)
    got = {
        r["source_file"]: r["scope"]
        for r in conn.execute("SELECT source_file, scope FROM episodes")
    }
    assert got == {str(personal): "personal", str(work): "work"}
    conn.close()

    # The emitted git-tracked files carry the same stamp.
    emitted = {
        p.stem: ingest.parse_episode_file(p)["scope"] for p in edir.rglob("*.md")
    }
    assert sorted(emitted.values()) == ["personal", "work"]


def test_episode_file_mode_reads_scope_from_frontmatter(tmp_path, monkeypatch):
    """The VM path (BRAIN_INGEST_SOURCE=episodes): scope comes from the file."""
    edir = tmp_path / "episodes"
    dbp = _wire_ingest(monkeypatch, tmp_path, [], edir)
    monkeypatch.setattr(ingest, "INGEST_SOURCE", "episodes")
    text = "USER: hi\n\nASSISTANT: hello"
    chash = db.episode_content_hash("2026-07-01T00:00:00Z", "/proj", "main", 0, text)
    ingest.emit_episode_file({
        "stable_id": "ep_abcdef0123456789", "content_hash": chash, "session_id": "s1",
        "uuid": "u1", "ts": "2026-07-01T00:00:00Z", "cwd": "/proj",
        "git_branch": "main", "is_sidechain": 0, "text": text, "scope": "work",
    })

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1
    conn = db.connect(dbp)
    assert conn.execute("SELECT scope FROM episodes").fetchone()["scope"] == "work"
    conn.close()


# ------------------------------------------------------- read filtering ---

def test_recall_unset_scope_is_unfiltered(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "PROFILE_SCOPE", None)
    conn = _corpus(tmp_path, monkeypatch)
    # No filter object is built at all — the pre-scope fast path, byte for byte.
    for kind in ("episode", "memory", "summary"):
        assert db._filter_ids(conn, kind, None, None, None) is None
    result = db.recall(conn, "quorple", k=20, scope="all")
    assert _ids(result) == {
        "mem_shared0000001", "mem_personal00001", "mem_work000000001",
        "ep_shared00000001", "ep_personal000001", "ep_work0000000001", "sum_1",
    }
    assert "filters" not in result and "filter_candidates" not in result
    conn.close()


@pytest.mark.parametrize(
    "profile, own, hidden",
    [
        ("work", {"mem_work000000001", "ep_work0000000001"},
         {"mem_personal00001", "ep_personal000001"}),
        ("personal", {"mem_personal00001", "ep_personal000001"},
         {"mem_work000000001", "ep_work0000000001"}),
    ],
)
def test_recall_returns_shared_plus_own_profile_only(
    tmp_path, monkeypatch, profile, own, hidden
):
    monkeypatch.setattr(db, "PROFILE_SCOPE", profile)
    conn = _corpus(tmp_path, monkeypatch)
    ids = _ids(db.recall(conn, "quorple", k=20, scope="all"))
    # Shared rows + this profile's, in BOTH kinds; summaries always ride along.
    assert ids == {"mem_shared0000001", "ep_shared00000001", "sum_1"} | own
    assert not (ids & hidden)  # the other profile is invisible
    # Same rule under a narrowed kind scope (the filter is per-kind, not global).
    assert _ids(db.recall(conn, "quorple", k=20, scope="memories")) == {
        "mem_shared0000001"
    } | {i for i in own if i.startswith("mem_")}
    conn.close()


def test_scope_filter_applies_to_the_vector_leg_too(tmp_path, monkeypatch):
    """_filter_ids feeds BOTH search legs, so the vec KNN sees the same
    candidate set (filter-then-fuse). Asserted at the source: the id sets."""
    monkeypatch.setattr(db, "PROFILE_SCOPE", "work")
    conn = _corpus(tmp_path, monkeypatch)
    mem_ids = db._filter_ids(conn, "memory", None, None, None)
    ep_ids = db._filter_ids(conn, "episode", None, None, None)
    assert sorted(mem_ids) == [1, 3] and sorted(ep_ids) == [1, 3]  # shared + work
    assert db._filter_ids(conn, "summary", None, None, None) is None
    # Both legs are given exactly those ids.
    assert set(db.fts_search(conn, "memory", "quorple", ids=mem_ids)) == {1, 3}
    if db.has_vec(conn):
        vec = [1.0] + [0.0] * (EMBED_DIM - 1)  # every row on the same unit axis
        for rowid in (1, 2, 3):
            conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, ?)",
                (rowid, db.serialize_f32(vec)),
            )
        unfiltered = [rid for rid, _d in db.vec_search(conn, "memory", vec)]
        assert sorted(unfiltered) == [1, 2, 3]  # the KNN would return all three…
        filtered = [rid for rid, _d in db.vec_search(conn, "memory", vec, ids=mem_ids)]
        assert sorted(filtered) == [1, 3]       # …but the candidate set drops #2
    conn.close()


def test_scope_filter_composes_with_the_project_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "PROFILE_SCOPE", "work")
    conn = _corpus(tmp_path, monkeypatch)
    ids = db._filter_ids(conn, "episode", "proj", None, None)
    assert sorted(ids) == [1, 3]  # cwd=/proj matches all three; scope drops #2
    conn.close()


# --------------------------------------------------------- hook filtering ---

def test_hook_query_filters_by_profile(tmp_path, monkeypatch):
    conn = _corpus(tmp_path, monkeypatch)
    expr = db._fts_or_query("quorple")

    monkeypatch.setattr(hook, "PROFILE_SCOPE", None)
    assert len(hook._query_memories(conn, expr)) == 3  # unfiltered = today

    monkeypatch.setattr(hook, "PROFILE_SCOPE", "work")
    names = {r["name"] for r in hook._query_memories(conn, expr)}
    assert names == {"mem_shared0000001", "mem_work000000001"}
    conn.close()


def test_hook_falls_back_when_the_db_predates_the_scope_column(tmp_path, monkeypatch):
    """The hook opens the DB read-only, so it can meet a DB that connect()
    has not yet migrated. Such a DB has no scoped rows, so the unfiltered
    query is equivalent — the hook must degrade to it, not go silent."""
    conn = _corpus(tmp_path, monkeypatch)
    conn.execute("DELETE FROM memories WHERE scope IS NOT NULL")
    try:
        conn.execute("ALTER TABLE memories DROP COLUMN scope")
    except sqlite3.OperationalError:  # pragma: no cover - old SQLite
        conn.close()
        pytest.skip("SQLite build lacks ALTER TABLE ... DROP COLUMN")
    conn.commit()
    monkeypatch.setattr(hook, "PROFILE_SCOPE", "work")
    rows = hook._query_memories(conn, db._fts_or_query("quorple"))
    assert {r["name"] for r in rows} == {"mem_shared0000001"}
    conn.close()


def test_connect_migrates_a_pre_scope_db_in_place(tmp_path):
    """No SCHEMA_VERSION bump: an existing index gains the column by ALTER and
    keeps its rows (a rebuild would re-derive and rewrite every episode file)."""
    path = tmp_path / "brain.db"
    conn = db.connect(path)
    _seed_memory(conn, 1, "mem_legacy0000001", "legacy row", None)
    conn.execute("ALTER TABLE memories DROP COLUMN scope")
    conn.execute("ALTER TABLE episodes DROP COLUMN scope")
    conn.commit()
    conn.close()

    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    assert "scope" in cols
    assert "scope" in {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
    row = conn.execute("SELECT scope FROM memories WHERE id = 1").fetchone()
    assert row["scope"] is None  # existing rows survive, unstamped = shared
    assert db.get_meta(conn, "schema_version") == str(db.SCHEMA_VERSION)  # no bump
    conn.close()
