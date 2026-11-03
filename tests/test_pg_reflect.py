"""reflect_runs row claim. Two callers cannot hold the lease at once."""

from __future__ import annotations


def test_profile_claims_do_not_block_each_other_or_clear_the_other_watermark(pg):
    from datetime import datetime, timedelta, timezone

    from brain import hosted_docs
    from brain.profile_ctx import override_profile

    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with pg.connection() as conn:
        conn.execute(
            """
            insert into brain.documents (
              stable_id, kind, body, content_hash, ts, day, session_id, scope, relpath, status
            ) values ('ep_debt', 'episode', 'text', 'h', %s, %s, 's-debt', null,
                      'episodes/x/ep_debt.md', 'active')
            """,
            (old, old.date()),
        )
        conn.commit()

    work = pg.claim_reflect("work-host", profile="work")
    personal = pg.claim_reflect("personal-host", profile="personal")
    assert work["ok"] is True and personal["ok"] is True
    assert pg.claim_reflect("work-again", profile="work")["ok"] is False
    assert pg.claim_reflect("neutral-blocked")["ok"] is False

    assert pg.complete_reflect(work["id"])["ok"] is True
    with override_profile("work"):
        status = pg.reflect_status()
    assert status["claim"] is None and status["watermark"]
    with pg.connection() as conn:
        from psycopg.rows import dict_row

        conn.row_factory = dict_row
        work_debt = hosted_docs.reflect_debt(conn, "work")
        personal_debt = hosted_docs.reflect_debt(conn, "personal")
        shared_debt = hosted_docs.reflect_debt(conn, None)
        conn.commit()
    assert work_debt["sessions"] == 0
    assert personal_debt["sessions"] == 1
    assert shared_debt["sessions"] == 1

    assert pg.complete_reflect(personal["id"])["ok"] is True
    neutral = pg.claim_reflect("neutral-host")
    assert neutral["ok"] is True
    assert pg.claim_reflect("work-while-neutral", profile="work")["ok"] is False
    assert pg.claim_reflect("personal-while-neutral", profile="personal")["ok"] is False


def test_claim_is_exclusive_until_complete(pg):
    first = pg.claim_reflect("host-a", host="a")
    assert first["ok"] is True
    second = pg.claim_reflect("host-b", host="b")
    assert second["ok"] is False
    assert second["claimed_by"] == "host-a"
    done = pg.complete_reflect(first["id"])
    assert done["ok"] is True
    third = pg.claim_reflect("host-b", host="b")
    assert third["ok"] is True
    status = pg.reflect_status()
    assert status["watermark"]
    assert status["claim"]["claimed_by"] == "host-b"
