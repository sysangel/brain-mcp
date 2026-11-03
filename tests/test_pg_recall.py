"""Eligibility is applied before both search legs, and on direct reads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain.embedconf import embed_space
from brain.profile_ctx import override_profile

VEC = [1.0] + [0.0] * 255
OTHER = [0.0, 1.0] + [0.0] * 254


def _vec(value):
    if value is None:
        return None
    from pgvector import Vector

    return Vector(value)


def _insert(pg, **kw):
    defaults = dict(
        stable_id="mem_x",
        kind="memory",
        title="note",
        description="desc",
        body="body",
        content_hash="h",
        status="active",
        recall_enabled=True,
        project_id=None,
        scope=None,
        relpath=None,
        cwd=None,
        ts=datetime.now(timezone.utc),
        day=datetime.now(timezone.utc).date(),
        embedding=None,
        embed_space=None,
        expires_at=None,
    )
    defaults.update(kw)
    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (
              stable_id, kind, title, description, body, content_hash, status,
              recall_enabled, project_id, scope, relpath, cwd, ts, day,
              embedding, embed_space, expires_at
            ) values (
              %(stable_id)s, %(kind)s, %(title)s, %(description)s, %(body)s,
              %(content_hash)s, %(status)s, %(recall_enabled)s, %(project_id)s,
              %(scope)s, %(relpath)s, %(cwd)s, %(ts)s, %(day)s, %(embedding)s,
              %(embed_space)s, %(expires_at)s
            )
            """,
            {**defaults, "embedding": _vec(defaults["embedding"])},
        )
        conn.commit()


def test_forgotten_and_expired_are_excluded_before_fts(pg):
    _insert(pg, stable_id="mem_live", body="alpha widget in project-alpha", content_hash="a",
            relpath="memories/live.md")
    _insert(pg, stable_id="mem_gone", body="alpha widget forgotten secret", content_hash="b",
            status="forgotten", relpath="memories/gone.md")
    _insert(
        pg, stable_id="mem_old", body="alpha widget expired coupon", content_hash="c",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        relpath="memories/old.md",
    )
    result = pg.recall("alpha widget", k=10)
    ids = [hit["id"] for hit in result["hits"]]
    assert ids == ["mem_live"]
    assert "unavailable" in pg.get_item("mem_gone")["error"]
    assert "unavailable" in pg.get_item("mem_old")["error"]


def test_project_id_beats_path_and_null_project_falls_back(pg):
    _insert(pg, stable_id="mem_project-alpha", body="roster lives here", content_hash="a",
            project_id="project-alpha", relpath="memories/unrelated.md")
    _insert(pg, stable_id="mem_path", body="roster on the path", content_hash="b",
            project_id=None, relpath="memories/project-alpha-notes.md")
    _insert(pg, stable_id="mem_other", body="roster but owned elsewhere", content_hash="c",
            project_id="other", relpath="memories/project-alpha-decoy.md")
    result = pg.recall("roster", project="project-alpha", k=10)
    ids = {hit["id"] for hit in result["hits"]}
    assert ids == {"mem_project-alpha", "mem_path"}


def test_scope_filters_memories_and_summaries_keeps_unscoped_rows(pg):
    _insert(pg, stable_id="mem_work", body="payroll calendar", content_hash="a",
            scope="work", relpath="memories/work.md")
    _insert(pg, stable_id="mem_personal", body="payroll personal", content_hash="b",
            scope="personal", relpath="memories/personal.md")
    _insert(pg, stable_id="sum_work", kind="summary", body="payroll work digest",
            content_hash="c", scope="work", relpath="summaries/2026-10/work.md",
            title="work digest")
    _insert(pg, stable_id="sum_shared", kind="summary", body="payroll shared digest",
            content_hash="d", scope=None, relpath="summaries/2026-10/shared.md",
            title="shared digest")
    with override_profile("work"):
        ids = {hit["id"] for hit in pg.recall("payroll", k=10)["hits"]}
    assert "mem_work" in ids
    assert "mem_personal" not in ids
    assert "sum_work" in ids
    assert "sum_shared" in ids
    with override_profile("personal"):
        ids = {hit["id"] for hit in pg.recall("payroll", k=10)["hits"]}
    assert "sum_work" not in ids
    assert "sum_shared" in ids
    with override_profile("personal"):
        assert "unavailable" in pg.get_item("sum_work")["error"]


def test_vector_floor_drops_weak_leg(pg, monkeypatch):
    space = embed_space()
    _insert(pg, stable_id="mem_near", body="no lexical overlap qqq", content_hash="a",
            embedding=VEC, embed_space=space, relpath="memories/near.md")
    _insert(pg, stable_id="mem_far", body="still no lexical overlap zzz", content_hash="b",
            embedding=OTHER, embed_space=space, relpath="memories/far.md")
    monkeypatch.setattr(pg, "_embed_query", lambda _text: VEC)
    result = pg.recall("unrelatedquerytoken", k=10)
    ids = [hit["id"] for hit in result["hits"]]
    assert ids == ["mem_near"]
    assert result["fts_only"] is False


def test_unicode_query_uses_vector_search_without_ascii_tokens(pg, monkeypatch):
    space = embed_space()
    _insert(
        pg,
        stable_id="mem_cjk",
        body="zzzz no latin overlap",
        content_hash="a",
        embedding=VEC,
        embed_space=space,
        relpath="memories/cjk.md",
    )
    calls = []

    def _embed(text):
        calls.append(text)
        return VEC

    monkeypatch.setattr(pg, "_embed_query", _embed)
    result = pg.recall("記憶", k=5)
    assert calls == ["記憶"]
    assert result["fts_only"] is False
    assert [hit["id"] for hit in result["hits"]] == ["mem_cjk"]
    calls.clear()
    empty = pg.recall("???", k=5)
    assert calls == []
    assert empty["hits"] == []


def test_accented_query_is_an_fts_token(pg):
    _insert(
        pg,
        stable_id="mem_cafe",
        body="café roster for the office",
        content_hash="cafe",
        relpath="memories/cafe.md",
    )
    result = pg.recall("café", k=5)
    assert [hit["id"] for hit in result["hits"]] == ["mem_cafe"]


def test_remember_stamps_request_profile(pg):
    with override_profile("personal"):
        created = pg.remember(
            "Tea is kept in the blue tin.",
            type="reference",
            name="tea-tin",
        )
        project = pg.remember(
            "Project-Alpha ships on Fridays.",
            type="project",
            name="project-alpha-fridays",
        )
        shared = pg.remember(
            "The office door code is posted.",
            type="reference",
            name="door-code",
            scope="shared",
        )
    assert created["scope"] == "personal"
    assert "scope" not in project
    assert "scope" not in shared
    with pg.connection() as conn:
        rows = dict(
            conn.execute(
                "select title, scope from brain.documents where kind = 'memory'"
            ).fetchall()
        )
        conn.commit()
    assert rows["tea-tin"] == "personal"
    assert rows["project-alpha-fridays"] is None
    assert rows["door-code"] is None
    with override_profile("work"):
        ids = {hit["id"] for hit in pg.recall("tea tin", k=10)["hits"]}
    assert created["id"] not in ids
    with override_profile("work"):
        ids = {hit["id"] for hit in pg.recall("door code", k=10)["hits"]}
    assert shared["id"] in ids


def test_remember_does_not_require_git(pg):
    created = pg.remember(
        "The widget quota is forty two units.",
        type="reference",
        name="widget-quota",
        project_id="project-alpha",
    )
    assert created["status"] == "created"
    assert created["committed"] is False
    assert created["git_export"] == "queued"
    assert created["stored"] == "postgres"
    assert created["project_id"] == "project-alpha"
    got = pg.get_item(created["id"])
    assert "forty two" in got["text"]
    with pg.connection() as conn:
        queued = conn.execute("select count(*) from brain.git_exports").fetchone()[0]
        conn.commit()
    assert queued == 1
    again = pg.remember("The widget quota is forty two units.", type="reference")
    assert again["status"] == "duplicate"
    assert again["signal"] == "exact"


def test_alias_resolves_on_get(pg):
    _insert(pg, stable_id="mem_v2", body="alias me", content_hash="a", relpath="memories/a.md")
    with pg.connection() as conn:
        conn.execute(
            "insert into brain.memory_id_aliases (old_id, stable_id) values (%s, %s)",
            ("mem_deadbeefdeadbeef", "mem_v2"),
        )
        conn.commit()
    got = pg.get_item("mem_deadbeefdeadbeef")
    assert got["id"] == "mem_v2"
    assert "alias me" in got["text"]
