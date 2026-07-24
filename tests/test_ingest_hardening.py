"""Hardening fixes for run_ingest and config (concurrency + RAM + safety):

  * single-writer lock: a background caller (block=False) cleanly no-ops when
    another writer holds the ingest lock;
  * chunked embedding backfill: pending rows embed in bounded chunks, never one
    giant batch;
  * zero-transcript prune guard: an all-transcripts-vanished scan aborts instead
    of pruning every episode;
  * multi-dir BRAIN_CLAUDE_PROJECTS parsing;
  * last_ingest marker preserved across a --full rebuild.

All DB/scan state is a tmp dir + monkeypatched paths — never the real store. The
embedder is stubbed deterministic (or absent) so nothing hits the network.
"""

from __future__ import annotations

import fcntl
import json

import numpy as np
import pytest

from brain import config, db, embed, ingest, sync
from brain.config import EMBED_DIM


def _user_line(session, uuid, ts, text):
    return json.dumps({
        "type": "user",
        "message": {"content": text},
        "sessionId": session,
        "uuid": uuid,
        "timestamp": ts,
        "cwd": "/proj",
        "gitBranch": "main",
        "isSidechain": False,
    })


def _write_transcript(path, episodes):
    path.write_text("\n".join(_user_line(*e) for e in episodes) + "\n")


def _wire(monkeypatch, tmp_path, transcripts, *, embedder=None):
    dbp = tmp_path / "brain.db"
    monkeypatch.setattr(db, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "DB_PATH", dbp)
    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ingest, "find_transcripts", lambda: list(transcripts))
    monkeypatch.setattr(ingest, "find_memory_files", lambda: [])
    monkeypatch.setattr(ingest, "find_summary_files", lambda: [])
    # Episodes-in-git: keep these transcript-mode tests off the real repo/git —
    # emit into a tmp episodes/ dir, never scan it, never commit.
    monkeypatch.setattr(ingest, "EPISODES_DIR", tmp_path / "episodes")
    monkeypatch.setattr(ingest, "find_episode_files", lambda: [])
    monkeypatch.setattr(ingest, "commit_episode_files", lambda paths, log=None: False)
    monkeypatch.setattr(sync, "maybe_pull", lambda log=None: None)
    if embedder is None:
        monkeypatch.setattr(embed, "available", lambda: False)  # FTS-only
    else:
        monkeypatch.setattr(embed, "available", lambda: True)
        monkeypatch.setattr(embed, "embed_documents", embedder)
    return dbp


def _has_vec(tmp_path):
    conn = db.connect(tmp_path / "probe.db")
    ok = db.has_vec(conn)
    conn.close()
    return ok


# ---------------------------------------------------------------- config ---

def test_parse_project_dirs_explicit_list(monkeypatch):
    monkeypatch.setenv("BRAIN_CLAUDE_PROJECTS", "~/a:/b/c: :~/a")
    dirs = config._parse_project_dirs()
    # tilde expanded, blank part skipped, duplicate collapsed, order preserved.
    from pathlib import Path
    assert dirs == [Path("~/a").expanduser(), Path("/b/c")]


def test_parse_project_dirs_default_filters_missing(monkeypatch):
    monkeypatch.delenv("BRAIN_CLAUDE_PROJECTS", raising=False)
    dirs = config._parse_project_dirs()
    # Default returns only existing roots (each is_dir()); never a phantom path.
    assert all(d.is_dir() for d in dirs)


# ------------------------------------------------------------- lock no-op ---

def test_background_caller_noops_when_lock_held(tmp_path, monkeypatch):
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "hello world")])
    _wire(monkeypatch, tmp_path, [t])

    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    # Hold the single-writer lock from a separate open fd (flock contends across
    # descriptions even within one process).
    held = open(cache / ingest.INGEST_LOCK_NAME, "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    try:
        stats = ingest.run_ingest(block=False)
        assert stats.get("skipped") is True
        assert stats.get("reason") == "ingest-lock-held"
        # Nothing was written while the lock was held.
        assert not (tmp_path / "brain.db").exists()
    finally:
        held.close()

    # Lock free now: the same background call runs to completion.
    stats = ingest.run_ingest(block=False)
    assert stats.get("skipped") is None
    assert stats["episodes_added"] == 1


# ----------------------------------------------------------- prune guard ---

def test_zero_transcript_scan_aborts_prune(tmp_path, monkeypatch):
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [
        ("s1", "u1", "2026-07-01T00:00:00Z", "alpha"),
        ("s1", "u2", "2026-07-01T00:05:00Z", "beta"),
    ])
    _wire(monkeypatch, tmp_path, [t])
    ingest.run_ingest()  # indexes 2 episodes + a .jsonl files row

    # Simulate a misconfigured/empty scan root: zero transcripts found.
    monkeypatch.setattr(ingest, "find_transcripts", lambda: [])
    with pytest.raises(RuntimeError, match="refusing to prune"):
        ingest.run_ingest()

    # The guard fired BEFORE the prune: episodes are intact, not wiped.
    conn = db.connect(tmp_path / "brain.db")
    assert conn.execute("SELECT count(*) FROM episodes").fetchone()[0] == 2
    conn.close()


def test_zero_transcript_scan_ok_when_index_empty(tmp_path, monkeypatch):
    # Fresh machine: no transcripts and no indexed transcript rows -> no error.
    _wire(monkeypatch, tmp_path, [])
    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 0


# ------------------------------------------------------- chunked backfill ---

def test_embedding_backfill_is_chunked(tmp_path, monkeypatch):
    if not _has_vec(tmp_path):
        pytest.skip("sqlite-vec unavailable: embedding backfill is skipped")

    calls: list[int] = []

    def fake_embed_documents(texts):
        calls.append(len(texts))
        out = []
        for i, _ in enumerate(texts):
            v = np.zeros(EMBED_DIM, dtype=np.float32)
            v[i % EMBED_DIM] = 1.0
            out.append(v)
        return out

    t = tmp_path / "s.jsonl"
    _write_transcript(t, [
        ("s1", f"u{i}", f"2026-07-01T00:0{i}:00Z", f"prompt number {i}")
        for i in range(5)
    ])
    _wire(monkeypatch, tmp_path, [t], embedder=fake_embed_documents)
    monkeypatch.setattr(ingest, "EMBED_BACKFILL_CHUNK", 2)

    stats = ingest.run_ingest()
    assert stats["episodes_added"] == 5
    assert stats["embedded"] == 5
    # Bounded chunks: no single embed call exceeded the chunk size, and it took
    # more than one call (proof it paged rather than one giant batch).
    assert calls, "embedder was never called"
    assert max(calls) <= 2
    assert len(calls) >= 3
    assert sum(calls) == 5  # every pending row embedded exactly once

    conn = db.connect(tmp_path / "brain.db")
    assert conn.execute("SELECT count(*) FROM episodes_vec").fetchone()[0] == 5
    conn.close()


# ------------------------------------------- last_ingest preserved on --full ---

def test_full_rebuild_preserves_last_ingest_marker(tmp_path, monkeypatch):
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("s1", "u1", "2026-07-01T00:00:00Z", "seed")])
    dbp = _wire(monkeypatch, tmp_path, [t])

    ingest.run_ingest()  # first run stamps last_ingest = L1
    conn = db.connect(dbp)
    l1 = db.get_meta(conn, "last_ingest")
    conn.close()
    assert l1 is not None

    # Probe the marker mid-rebuild: find_transcripts runs AFTER connect + the
    # marker-preserve step but BEFORE the run finishes. With Fix 5 the drop no
    # longer wipes the marker, so a reader sees L1 (the preserved value), not
    # None ("empty + stale" -> stampede).
    observed = {}

    def probing_find_transcripts():
        ro = db.connect_ro(dbp)
        try:
            row = ro.execute(
                "SELECT value FROM meta WHERE key = 'last_ingest'"
            ).fetchone()
            observed["mid"] = row["value"] if row else None
        finally:
            ro.close()
        return [t]

    monkeypatch.setattr(ingest, "find_transcripts", probing_find_transcripts)
    ingest.run_ingest(full=True)

    assert observed.get("mid") == l1  # marker never went missing during --full
