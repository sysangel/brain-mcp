"""Pure recall ranking: normalize RRF fusion, then multiply in recency + kind
priors. No sqlite / embed imports — unit-testable in isolation.

    score    = rrf_norm * KIND_WEIGHTS[kind] * recency          ∈ (0, 1]
    rrf_norm = rrf * (rrf_k + 1) / n_lists                      ∈ (0, 1]
               # 1.0 = ranked #1 in every fused list
    recency  = floor + (1 - floor) * 0.5 ** (age_days / half_life)  ∈ [floor, 1]
               # ts missing/unparseable → recency = floor
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .config import HALF_LIFE_DAYS, KIND_WEIGHTS, RECENCY_FLOOR, RRF_K


@dataclass(frozen=True)
class Candidate:
    kind: str            # "memory" | "summary" | "episode"
    rid: int
    rrf: float           # raw RRF sum from db._rrf
    n_lists: int         # rank lists fused for this kind (1 or 2)
    ts: str | None       # ISO-8601 or None


def recency_factor(
    ts: str | None, now: datetime, half_life_days: float, floor: float
) -> float:
    """Exponential age decay from 1 (now) toward `floor` (old). A missing or
    unparseable `ts` yields `floor` — old/undated content never scores zero."""
    if not ts:
        return floor
    try:
        parsed = datetime.fromisoformat(ts)  # py>=3.11 parses both Z and +00:00
    except ValueError:
        return floor
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - parsed).total_seconds() / 86400.0)
    return floor + (1.0 - floor) * 0.5 ** (age_days / half_life_days)


def rank(
    candidates: list[Candidate],
    now: datetime,
    *,
    kind_weights: dict[str, float] = KIND_WEIGHTS,
    half_lives: dict[str, float] = HALF_LIFE_DAYS,
    floor: float = RECENCY_FLOOR,
    rrf_k: int = RRF_K,
) -> list[tuple[float, Candidate]]:
    """Score every candidate and return `(score, candidate)` sorted descending.
    Each score depends only on the candidate's own ranks/kind/age, so removing
    an unrelated candidate leaves the others bit-identical. Tie-break by
    `(kind, rid)` for deterministic order within one index build."""
    scored: list[tuple[float, Candidate]] = []
    for c in candidates:
        rrf_norm = c.rrf * (rrf_k + 1) / c.n_lists
        recency = recency_factor(c.ts, now, half_lives[c.kind], floor)
        score = rrf_norm * kind_weights[c.kind] * recency
        scored.append((score, c))
    scored.sort(key=lambda sc: (-sc[0], sc[1].kind, sc[1].rid))
    return scored
