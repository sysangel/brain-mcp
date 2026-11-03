"""Unit tests for the embedding batch planner (brain.embed.plan_batches).

Pure function over token lengths — no model load, no download, no inference.
The invariant under test is the memory bound that keeps a full rebuild from
blowing up: transformer activation peak is O(batch * seq_len^2) and a batch is
padded to its longest member, so `len(batch) * max_len^2` is what must stay
under budget. A fixed document count does not bound anything.
"""

from __future__ import annotations

from brain.embed import plan_batches

CAP = 32
BUDGET = 1_000_000


def _plan(lengths, cap=CAP, budget=BUDGET):
    return plan_batches(lengths, cap=cap, budget=budget)


def _cost(batch, lengths):
    return len(batch) * max(lengths[i] for i in batch) ** 2


def test_every_index_appears_exactly_once():
    lengths = [10, 700, 3, 512, 1200, 64, 512, 900]
    batches = _plan(lengths)
    seen = sorted(i for b in batches for i in b)
    assert seen == list(range(len(lengths)))


def test_batches_stay_under_budget():
    # Mixed lengths, all individually affordable: no batch may bust the budget.
    lengths = [120, 700, 64, 512, 700, 300, 900, 950, 64, 800] * 5
    for batch in _plan(lengths):
        assert _cost(batch, lengths) <= BUDGET


def test_long_documents_get_small_batches():
    # 900^2 = 810k, so only one 900-token doc fits under a 1M budget.
    lengths = [900] * 8
    batches = _plan(lengths)
    assert [len(b) for b in batches] == [1] * 8


def test_short_documents_pack_up_to_the_cap():
    # 32 * 100^2 = 320k, well under budget, so the count cap is what binds.
    lengths = [100] * 70
    batches = _plan(lengths)
    assert max(len(b) for b in batches) == CAP
    assert [len(b) for b in batches] == [CAP, CAP, 6]


def test_single_oversized_document_gets_its_own_batch():
    # One doc can't be split; it must not drag others into an over-budget batch.
    lengths = [50, 4000, 50]
    batches = _plan(lengths)
    assert [4000 in [lengths[i] for i in b] and len(b) == 1 for b in batches].count(True) == 1
    for batch in batches:
        if lengths[batch[0]] != 4000:
            assert _cost(batch, lengths) <= BUDGET


def test_one_long_doc_does_not_shrink_batches_for_short_ones():
    # Sorting by length keeps the single 1000-token doc from capping the
    # batch size of the 60 short docs that ride alongside it.
    lengths = [1000] + [50] * 60
    batches = _plan(lengths)
    assert max(len(b) for b in batches) == CAP


def test_empty_input():
    assert _plan([]) == []


def test_zero_length_documents_do_not_divide_by_zero():
    # Empty text still costs one padded position; must not produce a giant batch.
    batches = _plan([0] * 5, cap=4)
    assert [len(b) for b in batches] == [4, 1]
