"""Unit tests for the pure recall ranking function (brain.ranking).

Fixed fake candidates, a fixed `now`, and explicit constants so nothing here
depends on config defaults or the live index.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain.ranking import Candidate, rank, recency_factor

NOW = datetime(2026, 7, 3, tzinfo=timezone.utc)

# Explicit constants pinned per case (mirror the config defaults) so a later
# retune of config.py can't silently move these numbers.
KW = {"memory": 1.00, "summary": 0.85, "episode": 0.70}
HL = {"memory": 180.0, "summary": 90.0, "episode": 45.0}
FLOOR = 0.35
RRF_K = 60

# rank-0-in-two-lists raw RRF sum: 1/(60+0+1) twice.
RRF_TOP2 = 2 / 61
RRF_ONE = 1 / 61


def _rank(cands):
    return rank(cands, NOW, kind_weights=KW, half_lives=HL, floor=FLOOR, rrf_k=RRF_K)


def _ts(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def test_kind_prior_ordering():
    cands = [
        Candidate("episode", 3, RRF_TOP2, 2, NOW.isoformat()),
        Candidate("memory", 1, RRF_TOP2, 2, NOW.isoformat()),
        Candidate("summary", 2, RRF_TOP2, 2, NOW.isoformat()),
    ]
    ranked = _rank(cands)
    assert [c.kind for _, c in ranked] == ["memory", "summary", "episode"]
    scores = [s for s, _ in ranked]
    assert scores[0] == 1.00
    assert scores[1] == 0.85
    assert scores[2] == 0.70


def test_recency_reorders_within_kind():
    fresh = Candidate("episode", 1, RRF_TOP2, 2, NOW.isoformat())
    stale = Candidate("episode", 2, RRF_TOP2, 2, _ts(90))  # 2 half-lives at 45d
    ranked = _rank([stale, fresh])
    assert [c.rid for _, c in ranked] == [1, 2]  # fresh first
    stale_score = ranked[1][0]
    expected = 0.70 * (0.35 + 0.65 * 0.25)  # 0.35875
    assert abs(stale_score - expected) < 1e-9


def test_memory_decays_slower():
    mem = Candidate("memory", 1, RRF_TOP2, 2, _ts(180))
    ep = Candidate("episode", 2, RRF_TOP2, 2, _ts(180))
    # recency factors in isolation
    assert abs(recency_factor(mem.ts, NOW, HL["memory"], FLOOR) - 0.675) < 1e-9
    assert abs(recency_factor(ep.ts, NOW, HL["episode"], FLOOR) - 0.390625) < 1e-9
    ranked = _rank([ep, mem])
    assert [c.kind for _, c in ranked] == ["memory", "episode"]  # memory first


def test_normalization_and_range():
    # rank-0 in both of two lists, fresh memory -> exactly 1.0
    both = _rank([Candidate("memory", 1, RRF_TOP2, 2, NOW.isoformat())])
    assert both[0][0] == 1.0
    # present in only one of two lists -> caps at 0.5
    one_of_two = _rank([Candidate("memory", 1, RRF_ONE, 2, NOW.isoformat())])
    assert one_of_two[0][0] == 0.5
    # FTS-only mode (single leg): rank-0 in the one list -> 1.0
    fts_only = _rank([Candidate("memory", 1, RRF_ONE, 1, NOW.isoformat())])
    assert fts_only[0][0] == 1.0
    # every score stays in (0, 1]
    for s, _ in both + one_of_two + fts_only:
        assert 0.0 < s <= 1.0


def test_missing_ts_and_absolute_stability():
    dated = Candidate("episode", 1, RRF_TOP2, 2, NOW.isoformat())
    undated = Candidate("episode", 2, RRF_TOP2, 2, None)
    unrelated = Candidate("memory", 3, RRF_ONE, 2, _ts(10))

    # ts=None -> recency == floor, so it ranks below its fresh twin.
    assert recency_factor(None, NOW, HL["episode"], FLOOR) == FLOOR
    ranked = _rank([undated, dated])
    assert [c.rid for _, c in ranked] == [1, 2]
    assert abs(ranked[1][0] - 0.70 * FLOOR) < 1e-9  # 0.70 * 0.35

    # Absolute stability: removing an unrelated candidate leaves every other
    # score bit-identical (scoring is per-candidate independent).
    before = {c.rid: s for s, c in _rank([dated, undated, unrelated])}
    after = {c.rid: s for s, c in _rank([dated, undated])}
    for rid, s in after.items():
        assert before[rid] == s  # exact, not approx
