"""Episodes-in-git transport (brain.ingest): the Mac emits one file per episode
into episodes/YYYY/MM/<stable_id>.md so a transcript-less VM can index episodes
over `git pull`. See specs/episodes-in-git.md.

Everything runs against a tmp DB + a tmp EPISODES_DIR with monkeypatched paths —
never the real store, never the real git. The commit step is stubbed to a no-op
(a real commit would touch the actual brain repo), the remote pull/push are
stubbed, and the embedder is stubbed absent so nothing hits the network. Covers:
emit idempotency + frontmatter round-trip, the BRAIN_EPISODE_EXCLUDE gate,
transcript-vs-episode-file source selection, the content_hash verify-on-parse
skip, and the episode-file prune guard.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from brain import db, embed, ingest, remember, sync


# ------------------------------------------------------------- fixtures ---

def _user_line(session, uuid, ts, text, cwd="/proj"):
    return json.dumps({
        "type": "user",
        "message": {"content": text},
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": cwd,
        "gitBranch": "main",
        "isSidechain": False,
    })


def _write_transcript(path, episodes):
    """episodes: list of (session, uuid, ts, user_text[, cwd])."""
    path.write_text("\n".join(_user_line(*e) for e in episodes) + "\n")


def _wire(monkeypatch, tmp_path, transcripts, episodes_dir):
    """Point run_ingest at a tmp DB + tmp episodes/ dir. Unlike the hardening
    suite this deliberately does NOT stub find_episode_files — the real scanner
    reads episodes_dir so source-mode selection is exercised for real — but it
    DOES stub the commit + remote so no git/network is touched."""
    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "EPISODES_DIR", episodes_dir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    monkeypatch.setattr(ingest, "find_transcripts", lambda: list(transcripts))
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    # No real git / no real network: record commit calls, never push.
    committed_paths: list = []
    monkeypatch.setattr(
        ingest, "commit_episode_files",
        lambda paths, log=None: (committed_paths.extend(paths), False)[1],
    )
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    monkeypatch.setattr(sync, "enabled", lambda: False)
    monkeypatch.setattr(embed, "available", lambda: False)  # FTS-only, no network
    return dbp, committed_paths


def _row(**kw):
    """An episodes-row-shaped mapping with a self-consistent content_hash."""
    r = {
        "stable_id": "ep_abcdef0123456789",
        "session_id": "s1",
        "uuid": "u1",
        "ts": "2026-07-01T00:00:00Z",
        "cwd": "/proj",
        "git_branch": "main",
        "is_sidechain": 0,
        "text": "USER: hello there\n\nASSISTANT: hi\n\nmore text",
    }
    r.update(kw)
    r["content_hash"] = db.episode_content_hash(
        r["ts"], r["cwd"], r["git_branch"], r["is_sidechain"], r["text"]
    )
    return r


def _put_episode_file(episodes_dir, row):
    """Write a raw episode file with `row`'s (possibly wrong) content_hash — used
    to seed episode-file mode + tamper cases without going through emit."""
    content = ingest._render_episode_file(
        row["stable_id"], row["content_hash"], row["session_id"], row["uuid"],
        row["ts"], row["cwd"], row["git_branch"], row["is_sidechain"], row["text"],
    )
    dest = episodes_dir / ingest._episode_shard(row["ts"]) / f"{row['stable_id']}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content, encoding="utf-8")
    return dest


# --------------------------------------------------- emit + round-trip ---

def test_emit_idempotent_and_frontmatter_roundtrip(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])

    row = _row(is_sidechain=1, git_branch="")  # exercise int + empty-string fields
    p1 = ingest.emit_episode_file(row)
    assert p1 == edir / "2026" / "07" / f"{row['stable_id']}.md"
    first_bytes = p1.read_bytes()

    # Re-emit an unchanged episode: no rewrite, byte-identical on disk.
    p2 = ingest.emit_episode_file(row)
    assert p2 is None  # unchanged -> left in place
    assert p1.read_bytes() == first_bytes

    # Frontmatter + body round-trip exactly through parse_episode_file.
    parsed = ingest.parse_episode_file(p1)
    for key in (
        "stable_id", "content_hash", "session_id", "uuid", "ts", "cwd",
        "git_branch", "is_sidechain", "text",
    ):
        assert parsed[key] == row[key], key
    # The content_hash recomputed from the parsed fields matches the frontmatter.
    assert db.episode_content_hash(
        parsed["ts"], parsed["cwd"], parsed["git_branch"],
        parsed["is_sidechain"], parsed["text"],
    ) == parsed["content_hash"]


def test_emit_overwrites_in_place_on_change(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])

    row = _row()
    p1 = ingest.emit_episode_file(row)
    changed = _row(text="USER: hello there\n\nASSISTANT: a different answer")
    p2 = ingest.emit_episode_file(changed)
    assert p2 == p1  # same filename (stable_id) -> overwrite in place
    assert ingest.parse_episode_file(p2)["text"] == changed["text"]
    assert len(list(edir.rglob("*.md"))) == 1  # not a second file


def test_parse_rejects_non_episode_file(tmp_path):
    junk = tmp_path / "junk.md"
    junk.write_text("no frontmatter here\njust text\n")
    assert ingest.parse_episode_file(junk) is None


@pytest.mark.parametrize("body", [
    "line1\r\nline2",     # CRLF
    "a\rb",               # bare CR (old Mac line ending)
    "x\r",                # trailing CR
    "u\r\n\r\nv",         # CRLF blank line
    "plain\nunix\ntext",  # control: LF still round-trips
])
def test_cr_and_crlf_bodies_roundtrip_byte_exact(tmp_path, monkeypatch, body):
    # Regression: text-mode I/O applies universal-newline translation, so a body
    # with \r\n / \r never hash-verified on parse (the VM skipped it, the Mac
    # rewrote it every run). Bytes I/O keeps it byte-exact.
    edir = tmp_path / "episodes"
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    row = _row(text=body)

    p = ingest.emit_episode_file(row)
    parsed = ingest.parse_episode_file(p)
    assert parsed["text"] == body  # CR/CRLF preserved exactly
    assert db.episode_content_hash(
        parsed["ts"], parsed["cwd"], parsed["git_branch"],
        parsed["is_sidechain"], parsed["text"],
    ) == parsed["content_hash"]  # verify-on-parse passes
    # Idempotent: re-emitting the unchanged episode leaves the file untouched.
    assert ingest.emit_episode_file(row) is None


# --------------------------------------------------------- exclude gate ---

def test_exclude_gate_direct(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", ["personal-finance"])
    row = _row(cwd="/Users/x/src/personal-finance")
    assert ingest.emit_episode_file(row) is None
    assert list(edir.rglob("*.md")) == [] if edir.exists() else True


def test_exclude_gate_in_run_ingest_indexes_but_never_writes(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [
        ("s1", "u1", "2026-07-01T00:00:00Z", "public work", "/proj/open"),
        ("s2", "u2", "2026-07-01T00:05:00Z", "secret work", "/Users/x/secret-proj"),
    ])
    dbp, _ = _wire(monkeypatch, tmp_path, [t], edir)
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", ["secret"])

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 2          # both indexed locally
    assert stats["episode_files_emitted"] == 1   # only the non-excluded one written

    files = list(edir.rglob("*.md"))
    assert len(files) == 1
    assert ingest.parse_episode_file(files[0])["cwd"] == "/proj/open"

    conn = db.connect(dbp)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2
    conn.close()


# ------------------------------------------------- source-mode selection ---

def test_transcript_mode_emits_files(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "a real prompt")])
    dbp, committed = _wire(monkeypatch, tmp_path, [t], edir)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1
    assert stats["episode_files_emitted"] == 1
    files = list(edir.rglob("*.md"))
    assert len(files) == 1
    assert committed == files  # the emitted path was handed to the commit step
    # The emitted file verifies against its own recomputed content_hash.
    parsed = ingest.parse_episode_file(files[0])
    assert db.episode_content_hash(
        parsed["ts"], parsed["cwd"], parsed["git_branch"],
        parsed["is_sidechain"], parsed["text"],
    ) == parsed["content_hash"]


def test_episode_file_mode_indexes_and_never_emits(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    # No transcripts (a VM): seed an episode file straight into episodes/.
    dbp, committed = _wire(monkeypatch, tmp_path, [], edir)
    row = _row(stable_id="ep_vm00000000000001", text="USER: from the vm")
    src = _put_episode_file(edir, row)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1
    assert "episode_files_emitted" not in stats  # episode-file mode never emits
    assert committed == []                        # nor commits

    conn = db.connect(dbp)
    got = conn.execute(
        "SELECT stable_id, source_file, text FROM episodes"
    ).fetchone()
    assert got["stable_id"] == row["stable_id"]
    assert got["source_file"] == str(src)  # source_file is the .md path
    assert got["text"] == row["text"]
    conn.close()

    # Idempotent: an unchanged episode file mtime-skips on the next run.
    stats = ingest.run_ingest()
    assert stats["files_changed"] == 0
    assert stats["episodes_added"] == 0


def test_transcripts_win_over_episode_files(tmp_path, monkeypatch):
    # Both present -> transcript mode (parse transcripts AND emit), never
    # episode-file mode. The pre-existing stray episode file is not indexed.
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "the transcript one")])
    dbp, _ = _wire(monkeypatch, tmp_path, [t], edir)
    _put_episode_file(edir, _row(stable_id="ep_stray00000000001", text="USER: stray"))

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1  # only the transcript episode, not the stray
    conn = db.connect(dbp)
    texts = [r["text"] for r in conn.execute("SELECT text FROM episodes")]
    assert any("the transcript one" in x for x in texts)
    assert not any("stray" in x for x in texts)
    conn.close()


# ---------------------------------------------------- BRAIN_INGEST_SOURCE ---

def test_episodes_source_ignores_transcripts_and_never_emits(tmp_path, monkeypatch):
    # The VM setting: transcripts are present (its own `claude -p` reflects) but
    # BRAIN_INGEST_SOURCE=episodes forces episode-file mode — the transcripts are
    # never scanned/indexed/emitted, only the pulled episode files are indexed.
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "a local reflect transcript")])
    dbp, committed = _wire(monkeypatch, tmp_path, [t], edir)
    monkeypatch.setattr(ingest, "INGEST_SOURCE", "episodes")
    row = _row(stable_id="ep_fromgit00000001", text="USER: from git")
    src = _put_episode_file(edir, row)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1              # the episode FILE, not the transcript
    assert "episode_files_emitted" not in stats      # episode-file mode never emits
    assert committed == []                            # nor commits

    conn = db.connect(dbp)
    rows = conn.execute("SELECT stable_id, source_file FROM episodes").fetchall()
    assert len(rows) == 1
    assert rows[0]["stable_id"] == "ep_fromgit00000001"
    assert rows[0]["source_file"] == str(src)        # sourced from the .md, not the .jsonl
    # The transcript is never scanned, so its file is not tracked.
    assert conn.execute(
        "SELECT count(*) FROM files WHERE path = ?", (str(t),)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM files WHERE path LIKE '%.jsonl'"
    ).fetchone()[0] == 0
    conn.close()


def test_transcripts_source_forces_transcript_mode(tmp_path, monkeypatch):
    # Explicit transcripts mode parses + emits transcripts even with a stray
    # episode file already sitting in episodes/ (which is ignored, not indexed).
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "forced transcript episode")])
    dbp, committed = _wire(monkeypatch, tmp_path, [t], edir)
    monkeypatch.setattr(ingest, "INGEST_SOURCE", "transcripts")
    _put_episode_file(edir, _row(stable_id="ep_stray00000000001", text="USER: ignored stray"))

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 1
    assert stats["episode_files_emitted"] == 1       # transcript episode emitted
    assert len(committed) == 1

    conn = db.connect(dbp)
    texts = [r["text"] for r in conn.execute("SELECT text FROM episodes")]
    assert any("forced transcript episode" in x for x in texts)
    assert not any("ignored stray" in x for x in texts)  # the stray .md is not indexed
    conn.close()


def test_invalid_ingest_source_raises_loudly(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    _wire(monkeypatch, tmp_path, [], edir)
    monkeypatch.setattr(ingest, "INGEST_SOURCE", "bogus")
    with pytest.raises(RuntimeError, match="BRAIN_INGEST_SOURCE=.bogus."):
        ingest.run_ingest()


def test_switch_to_episodes_mode_prunes_stale_transcript_rows(tmp_path, monkeypatch):
    # Switching source on an existing DB needs no --full: the now-absent
    # other-source rows fall out of current_paths and the ordinary prune loop
    # removes them. Also proves the .jsonl guard is SKIPPED in episodes mode (it
    # would otherwise abort on the still-tracked .jsonl row).
    edir = tmp_path / "episodes"
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "old transcript episode")])
    dbp, _ = _wire(monkeypatch, tmp_path, [t], edir)

    monkeypatch.setattr(ingest, "INGEST_SOURCE", "auto")  # transcript mode: tracks the .jsonl
    ingest.run_ingest()
    conn = db.connect(dbp)
    assert conn.execute(
        "SELECT count(*) FROM files WHERE path LIKE '%.jsonl'"
    ).fetchone()[0] == 1
    conn.close()
    for f in edir.rglob("*.md"):  # isolate the switch from run 1's own emit
        f.unlink()

    monkeypatch.setattr(ingest, "INGEST_SOURCE", "episodes")
    _put_episode_file(edir, _row(stable_id="ep_switch0000000001", text="USER: the git episode"))
    ingest.run_ingest()  # would raise if the .jsonl guard still fired here

    conn = db.connect(dbp)
    assert conn.execute(
        "SELECT count(*) FROM files WHERE path LIKE '%.jsonl'"
    ).fetchone()[0] == 0  # stale transcript row pruned
    assert [r["stable_id"] for r in conn.execute("SELECT stable_id FROM episodes")] == [
        "ep_switch0000000001"
    ]
    conn.close()


# --------------------------------------------- content_hash verify-on-parse ---

def test_content_hash_mismatch_is_skipped(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    dbp, _ = _wire(monkeypatch, tmp_path, [], edir)
    # Frontmatter carries the hash of "USER: correct", but the body is tampered.
    good_hash = db.episode_content_hash(
        "2026-07-01T00:00:00Z", "/proj", "main", 0, "USER: correct"
    )
    tampered = _row(
        stable_id="ep_tamper0000000001", text="USER: TAMPERED", git_branch="main",
    )
    tampered["content_hash"] = good_hash  # deliberate mismatch vs the body
    _put_episode_file(edir, tampered)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 0  # verify failed -> skipped
    conn = db.connect(dbp)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 0
    conn.close()


# --------------------------------------------------- episode-file prune guard ---

def test_episode_file_prune_guard_aborts_on_wiped_checkout(tmp_path, monkeypatch):
    edir = tmp_path / "episodes"
    dbp, _ = _wire(monkeypatch, tmp_path, [], edir)
    _put_episode_file(edir, _row(stable_id="ep_guard00000000001", text="USER: durable"))

    stats = ingest.run_ingest()  # episode-file mode: 1 episode + its .md files row
    assert stats["episodes_added"] == 1

    # Bad checkout: episodes/ wiped. No transcripts AND no episode files, but the
    # index still tracks the .md source -> abort before the prune wipes it.
    for f in edir.rglob("*.md"):
        f.unlink()
    with pytest.raises(RuntimeError, match="refusing to prune"):
        ingest.run_ingest()

    conn = db.connect(dbp)
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 1  # intact
    conn.close()


def test_empty_everything_is_a_clean_noop(tmp_path, monkeypatch):
    # Fresh machine: no transcripts, no episode files, empty index -> no guard.
    edir = tmp_path / "episodes"
    dbp, _ = _wire(monkeypatch, tmp_path, [], edir)
    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 0


# ------------------------------------------------------- stranded-file sweep ---

def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


def test_stranded_file_swept_on_idle_transcript_run(tmp_path, monkeypatch):
    # A prior commit that failed past its index.lock retries can leave episode
    # files stranded uncommitted. A later transcript-mode run with ZERO new
    # episodes must still sweep + commit them (the emit block no longer gates the
    # commit on `emitted_paths`). Uses a REAL throwaway git repo so the real
    # commit path runs — nothing touches the actual brain repo.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    edir = repo / "episodes"

    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "EPISODES_DIR", edir)
    monkeypatch.setattr(remember, "BRAIN_DIR", repo)  # commit_paths git -C + relpath
    monkeypatch.setattr(ingest, "BRAIN_EPISODE_EXCLUDE", [])
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    monkeypatch.setattr(sync, "enabled", lambda: False)  # never push in tests
    monkeypatch.setattr(embed, "available", lambda: False)

    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "a real episode")])
    monkeypatch.setattr(ingest, "find_transcripts", lambda: [t])

    # Run 1: index + emit + commit the transcript episode (real git), clean tree.
    stats1 = ingest.run_ingest()
    assert stats1["episode_files_emitted"] == 1
    assert stats1["episode_files_committed"] is True
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    # Strand a file: write a second episode file straight into episodes/, leaving
    # it uncommitted (simulating an earlier commit that failed).
    _put_episode_file(edir, _row(stable_id="ep_stranded000001", text="USER: stranded"))
    assert "episodes/" in _git(repo, "status", "--porcelain").stdout  # dirty

    # Run 2: transcript UNCHANGED -> 0 new episodes. The sweep still runs and
    # commits the stranded file (this is the fix — an idle run re-attempts it).
    stats2 = ingest.run_ingest()
    assert stats2["episode_files_emitted"] == 0
    assert stats2["episode_files_committed"] is True
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""  # nothing stranded
    assert "ep_stranded000001.md" in _git(repo, "ls-files", "episodes/").stdout
