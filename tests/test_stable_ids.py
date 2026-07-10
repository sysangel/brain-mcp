"""Determinism of the shared identity + content-hash functions (brain.db).

These four functions are the single home for stable ids and change-detection
hashes used by ingest, usage-logging, and the remember tool. Their contract is
purely deterministic and network-free: identical input -> identical output,
and any distinct input -> a distinct output. Beyond those structural properties
we also pin the EXACT output bytes for fixed inputs (golden values below), so a
silent salt/format change — which would orphan every previously-indexed row —
fails loudly here instead of quietly re-keying the store.
"""

from __future__ import annotations

from brain import db


def test_episode_stable_id_is_deterministic_and_prefixed():
    a = db.episode_stable_id("sess-1", "uuid-1")
    b = db.episode_stable_id("sess-1", "uuid-1")
    assert a == b  # same (sessionId, uuid) -> same id, always
    assert a.startswith("ep_")
    assert len(a) == len("ep_") + 16
    assert a == "ep_f49093c94f72178a"  # golden: pins the salt + format exactly


def test_episode_stable_id_distinguishes_inputs():
    base = db.episode_stable_id("sess-1", "uuid-1")
    assert db.episode_stable_id("sess-2", "uuid-1") != base  # sessionId matters
    assert db.episode_stable_id("sess-1", "uuid-2") != base  # uuid matters


def test_episode_stable_id_fallback_key_when_uuid_missing():
    # With no uuid the id derives from (source/session, ts, user_text) instead.
    a = db.episode_stable_id("", "", source_path="/x.jsonl", ts="2026-07-05", user_text="hi")
    b = db.episode_stable_id("", "", source_path="/x.jsonl", ts="2026-07-05", user_text="hi")
    assert a == b
    assert a.startswith("ep_")
    assert a == "ep_7e0014838aadc3c2"  # golden: pins the fallback salt + format
    # A different opening user message under the same fallback key is distinct.
    c = db.episode_stable_id("", "", source_path="/x.jsonl", ts="2026-07-05", user_text="bye")
    assert c != a
    # The uuid path and the fallback path never collide for "the same" episode.
    assert db.episode_stable_id("sess-1", "uuid-1") != a


def test_memory_stable_id_keys_on_path():
    a = db.memory_stable_id("/abs/memories/foo.md")
    assert a == db.memory_stable_id("/abs/memories/foo.md")
    assert a.startswith("mem_")
    assert len(a) == len("mem_") + 16
    assert a == "mem_a6973dd8c0f98b05"  # golden: pins the salt + format exactly
    assert db.memory_stable_id("/abs/memories/bar.md") != a


def test_episode_content_hash_tracks_every_field():
    args = ("2026-07-05", "/cwd", "main", False, "body text")
    base = db.episode_content_hash(*args)
    assert base == db.episode_content_hash(*args)  # deterministic
    # golden: pins the field-join format so a silent change fails loudly
    assert base == "5798dad2339c173d3214e145ddb03953be40df294b70817b8ed4c92988cc6883"
    # Each field participates: perturbing any one changes the hash.
    assert db.episode_content_hash("2026-07-06", "/cwd", "main", False, "body text") != base
    assert db.episode_content_hash("2026-07-05", "/other", "main", False, "body text") != base
    assert db.episode_content_hash("2026-07-05", "/cwd", "dev", False, "body text") != base
    assert db.episode_content_hash("2026-07-05", "/cwd", "main", True, "body text") != base
    assert db.episode_content_hash("2026-07-05", "/cwd", "main", False, "changed") != base


def test_memory_content_hash_tracks_every_field():
    args = ("name", "desc", "note", "text", "2026-07-05")
    base = db.memory_content_hash(*args)
    assert base == db.memory_content_hash(*args)  # deterministic
    # golden: pins the field-join format so a silent change fails loudly
    assert base == "d4d7b40fe465b0f35acde765a43c9d57443d324a6d055e552d164f8196c23e79"
    assert db.memory_content_hash("other", "desc", "note", "text", "2026-07-05") != base
    assert db.memory_content_hash("name", "other", "note", "text", "2026-07-05") != base
    assert db.memory_content_hash("name", "desc", "fact", "text", "2026-07-05") != base
    assert db.memory_content_hash("name", "desc", "note", "other", "2026-07-05") != base
    # The authored date participates so editing it refreshes recency.
    assert db.memory_content_hash("name", "desc", "note", "text", "2026-07-06") != base
