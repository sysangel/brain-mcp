# Recency & kind weighting in recall ranking

## Problem

`recall` currently ranks purely by raw RRF fusion of two 40-item rank lists
per kind (FTS5 BM25 + sqlite-vec KNN, `db.py:_rrf`, k=60). Two priors are
missing:

1. **Recency** — an episode from yesterday and one from eight months ago rank
   identically if their lexical/semantic match is equal. For a personal memory
   system, recent sessions are usually the ones you mean.
2. **Kind** — a curated memory (distilled durable fact) and a raw transcript
   episode compete on equal footing, even though memories are strictly
   higher-signal per token. The upcoming session-summaries feature adds a
   third kind ("summary") that should rank between them.

Additionally, the raw RRF sum is an arbitrary-unit number in roughly
[0.0100, 0.0328] that depends on how many rank lists were fused. A **stable,
normalized final score** makes hits comparable across queries and gives
deliberate hybrid-recall consumers a magnitude to reason about; today there is
no score in the response at all — hits are returned in order with no
magnitude. (Caveat, per the Group-integration analysis: the auto-recall hook
does **not** consume this score on its FTS-only fast path — a rank-derived
score carries no BM25 magnitude there and degenerates to kind×recency, so the
hook keeps its own token-overlap gate. The normalized score is still useful
for full hybrid recall and for stable ordering.)

Constraints: markdown+git stays the source of truth (no new persisted state —
decay must be computed at query time, not ingest time); MCP tool surface stays
minimal (no new tools); ranking must stay a pure, unit-testable function.

### What ingest actually stores (date ground truth)

- **Episodes**: `episodes.ts` = the jsonl `timestamp` of the user message
  (`ingest.py:125`) — ISO-8601 UTC with millisecond precision, e.g.
  `2026-07-02T18:25:00.046Z`. Occasionally `None` (some transcript lines lack
  it) — must be handled.
- **Memories**: `memories.ts` = file **mtime** as UTC ISO with `+00:00` offset
  (`ingest.py:213-215`). This is "last edited/confirmed", not "created" —
  editing or compacting a memory refreshes it, which is actually the right
  recency signal for curated facts (see Risks for the git-clone caveat).

## Design

### Chosen approach: multiplicative priors over the *full* RRF candidate pool

Keep `_rrf` exactly as is (per-kind fusion). After fusion, instead of sorting
raw RRF sums (`db.py:257-264`), pass **every** candidate — not just top-N —
through a pure ranking function that normalizes RRF and multiplies in the two
priors. The candidate pool is already bounded at ≤ 80 per kind (union of two
40-item lists), ≤ 160 total today, ≤ 240 once summaries land, so re-ranking
everything is trivially cheap and has no truncation bias.

#### Exact scoring formula

```
rrf_raw   = Σ over rank lists containing the item of 1 / (RRF_K + rank + 1)
            # unchanged, db.py:_rrf; RRF_K = 60, rank is 0-based

rrf_norm  = rrf_raw * (RRF_K + 1) / n_lists
            # n_lists = number of rank lists actually fused for that kind
            # (1 = FTS-only, 2 = hybrid). Divides by the theoretical maximum
            # rrf_raw (rank 0 in every list = n_lists/(RRF_K+1)), so
            # rrf_norm ∈ (0, 1] and 1.0 means "ranked #1 in every list".

age_days  = max(0.0, (now_utc − ts).total_seconds() / 86400)
recency   = RECENCY_FLOOR + (1 − RECENCY_FLOOR) * 0.5 ** (age_days / HALF_LIFE_DAYS[kind])
            # ∈ [RECENCY_FLOOR, 1]. ts missing/unparseable → recency = RECENCY_FLOOR.

score     = rrf_norm * KIND_WEIGHTS[kind] * recency        # ∈ (0, 1]
```

Sort descending by `score`, tie-break `(kind, rid)` for determinism, take top
k, attach `score` (rounded to 4 decimals) to each hit.

#### Defaults (config.py, env-overridable)

| Constant | Default | Env override | Rationale |
|---|---|---|---|
| `RRF_K` | 60 | `BRAIN_RRF_K` | Hoisted from the magic `k=60` at db.py:262; standard RRF damping. |
| `KIND_WEIGHTS["memory"]` | 1.00 | `BRAIN_KIND_WEIGHT_MEMORY` | Reference point; memories are distilled, curated facts. |
| `KIND_WEIGHTS["summary"]` | 0.85 | `BRAIN_KIND_WEIGHT_SUMMARY` | Named slot reserved now; session-summaries lands later. |
| `KIND_WEIGHTS["episode"]` | 0.70 | `BRAIN_KIND_WEIGHT_EPISODE` | A memory beats an episode at equal evidence by 1.43×: a fresh top-consensus episode scores 0.70, so a fresh memory outranks it whenever its rrf_norm ≥ 0.70. In hybrid mode that means rank ≤ ~26 in *both* legs (61/(61+r) ≥ 0.7), or rank ≤ 1 in one leg plus presence in the other (a one-leg-only item caps at 0.5) — a far weaker bar than "top-2 consensus". Strong episodes still beat marginal memories. |
| `HALF_LIFE_DAYS["memory"]` | 180 | `BRAIN_HALF_LIFE_MEMORY` | Curated facts stay true; ts is mtime, so active maintenance refreshes it anyway. 4× the episode half-life. |
| `HALF_LIFE_DAYS["summary"]` | 90 | `BRAIN_HALF_LIFE_SUMMARY` | Reserved slot; per-session digests are more durable than raw episodes, less than curated facts. |
| `HALF_LIFE_DAYS["episode"]` | 45 | `BRAIN_HALF_LIFE_EPISODE` | Middle of the plausible 30–90d band: the last ~6 weeks of sessions cover an active project arc; at 45d an episode keeps 50% of its recency term, at 90d 25%. |
| `RECENCY_FLOOR` | 0.35 | `BRAIN_RECENCY_FLOOR` | Age must never erase relevance: a two-year-old exact-match episode still surfaces (score ≥ 0.35 × 0.70 × rrf_norm). Pure decay to 0 would make old content unreachable, violating the spirit of "git is the durable store". |

**Should memories decay slower than episodes? Yes (180d vs 45d).** Episodes
are events — their usefulness is dominated by "what was I doing lately", and
their ts is a true event time. Memories are facts — their truth does not
expire on a session timescale, and their ts (mtime) already self-refreshes
when the reflect skill edits or compacts them, so decay only bites memories
that have gone genuinely untouched for months, which is exactly the
staleness signal we want.

Worked example of the balance: a 200-day-old memory at perfect consensus
scores `1.0 × 1.0 × (0.35 + 0.65·2^(−200/180))` ≈ 0.65 — a *fresh*
perfect-consensus episode (0.70) edges it out, but a week-old one at rank ~5
does not. That's the intended feel: recency can reorder, not bury.

#### Score semantics

- `score ∈ (0, 1]`, where **1.0 = a memory written today that ranked #1 in
  every searched rank list**.
- **Absolute for a fixed candidate universe — but not across universes.** A
  hit's score depends only on its own per-leg ranks, kind, and age — never on
  `k` or which other hits are returned; adding/removing *returned* siblings
  does not change a surviving score. It is **not**, however, invariant across
  different candidate universes: `rrf_norm` is derived from an item's rank
  *within each leg's returned list*, so anything that changes what a leg
  returns changes the score. In particular **recall-filters filters the legs
  before fusion** (per its spec): a row at rank 30 in an unfiltered recall can
  become rank 0 under a selective filter, jumping its `rrf_norm` toward 1.0.
  Consequence: within one *unfiltered* recall, scores are stable and mutually
  comparable; **scores from a *filtered* recall are inflated and are NOT
  threshold-comparable** with unfiltered ones. Never compare a filtered
  recall's scores against thresholds calibrated on unfiltered recalls.
- Mode caveat: because RRF is compressive, `rrf_norm` for a returned candidate
  never goes below ≈ 0.305 in hybrid mode (present at rank 39 of one of two
  lists) or ≈ 0.61 in single-leg mode (one list). Scores therefore skew higher
  when fewer legs are fused. The top-level `fts_only` flag is **response-global,
  but the per-kind leg count is not**: a kind whose vec table is empty fuses
  with `n_lists=1` even when `fts_only: false` (the `_vec_nonempty` gate,
  db.py:248), so one response can mix the 0.61 and 0.305 floors. Any threshold
  keyed on `fts_only` is therefore only **approximate** for such mixed-mode
  responses; a strict consumer should treat `fts_only`-based advice as a
  heuristic (or surface per-hit `n_lists` in a later revision).
- **Not a fast-path cutoff for the auto-recall hook.** The hook does not call
  `db.recall`; it runs a raw FTS-only `LIMIT 8` query where a reconstructed
  score would have `rrf_norm = 61/(61+r) ∈ [0.884, 1.0]` — an 11.6% spread
  carrying *zero* BM25 magnitude, so a score cutoff there collapses to
  kind×recency and cannot separate strong from weak lexical matches. The hook
  keeps a **token-overlap** relevance gate and may reuse `ranking.py` only to
  *order* its hits by kind/recency. The ~0.35–0.5 threshold band below applies
  only to full **hybrid** recall — choosing it is that consumer's job.

#### Pure ranking function

New module `src/brain/ranking.py`, no sqlite or embed imports:

```python
@dataclass(frozen=True)
class Candidate:
    kind: str            # "memory" | "summary" | "episode"
    rid: int
    rrf: float           # raw RRF sum from db._rrf
    n_lists: int         # rank lists fused for this kind (1 or 2)
    ts: str | None       # ISO-8601 or None

def recency_factor(ts, now, half_life_days, floor) -> float: ...
def rank(candidates, now, *, kind_weights=KIND_WEIGHTS,
         half_lives=HALF_LIFE_DAYS, floor=RECENCY_FLOOR,
         rrf_k=RRF_K) -> list[tuple[float, Candidate]]:  # sorted desc
```

`now` is always passed in (recall passes `datetime.now(timezone.utc)`); tests
pass a fixed instant. Config values are keyword defaults so tests can pin
them. `datetime.fromisoformat` on Python ≥3.11 parses both the `Z`-suffixed
episode form and the `+00:00` memory form; parse failure → floor.

#### Alternatives rejected

1. **Additive rank fudge inside RRF** (shift ranks or inject phantom
   entries before `1/(k+rank+1)`): entangles the priors with `RRF_K`'s
   damping curve, is uninterpretable ("a memory is worth −7 rank positions?"),
   and destroys the per-item absolute-score property the hook needs.
2. **Post-RRF re-rank of only the top-N (N=k)**: truncation bias — a fresh
   memory sitting at raw position k+1 can never surface no matter how the
   priors would score it. Pointless economy: the full pool is ≤160 rows.
3. **Additive blend** (`w1·rrf_norm + w2·recency + w3·kind`): a stale,
   *irrelevant* memory would still carry `w3 + w2·floor` of score, so weak
   matches no longer collapse toward zero — exactly wrong for thresholding.
   Multiplicative keeps "no match ⇒ near-floor score".
4. **Per-kind RRF_K tuning** (smaller k for memories): opaque double-duty for
   one constant; can't express recency at all.
5. **Precomputing decay at ingest**: decay depends on *query* time; a stored
   weight is stale the moment it's written and violates "index is a dumb
   rebuildable cache". Rejected outright.

#### Parallel-feature assumptions (one line each, not designed here)

- **session-summaries**: will add `kind="summary"` candidates into the same
  scored pool with a session-end timestamp; this spec only reserves its
  `KIND_WEIGHTS` / `HALF_LIFE_DAYS` slots.
- **auto-recall-hook**: runs its own FTS-only fast path and keeps a
  token-overlap relevance gate; it does **not** threshold `db.recall`'s
  `score` (that score degenerates on the hook's path — see Score semantics and
  Group integration). It may reuse `ranking.py` purely to *order* its hits.
- **recall-filters**: constrains candidates at/before retrieval on the same
  query path; because it filters the legs it **promotes survivors' ranks and
  inflates their `score`**, so filtered-recall scores are not
  threshold-comparable with unfiltered ones (see Score semantics).

#### Performance

Added work per recall: one indexed `SELECT id, ts FROM <base> WHERE id IN
(…)` per kind (≤80 primary-key lookups each), then ≤160 iterations of one
ISO-8601 parse + one `**` + three multiplies — well under 1 ms total.
Recall latency stays dominated by `embed.embed_query` (model inference,
tens of ms) and the KNN scan. No extra per-hit I/O; the existing top-k
`SELECT *` row fetches (db.py:269, 283) are unchanged.

## API / schema changes

- **No DB schema change.** No new persisted state anywhere (design rule (a)
  upheld: decay is query-time arithmetic).
- **No new MCP tools** (design rule (b) upheld).
- **`recall` response**: each hit dict gains one field —
  `"score": float` — rounded to 4 decimals, range (0, 1], semantics above.
  Existing fields (`kind`, `id`, `date`, `project`, `snippet`,
  `session_id`/`name`) and the top-level `fts_only` flag are unchanged.
  Hit order remains score-descending, so existing consumers that ignore
  `score` see strictly-better-ordered results.
- **New env vars** (all optional): `BRAIN_RRF_K`,
  `BRAIN_KIND_WEIGHT_{MEMORY,SUMMARY,EPISODE}`,
  `BRAIN_HALF_LIFE_{MEMORY,SUMMARY,EPISODE}`, `BRAIN_RECENCY_FLOOR`.

## Files touched (file:line anchors into current code)

- `src/brain/config.py:38-40` — extend the "Recall / output" block: add a
  small `_env_float(name, default)` helper and the constants table above
  (`RRF_K`, `KIND_WEIGHTS`, `HALF_LIFE_DAYS`, `RECENCY_FLOOR`); document the
  new env names in the module docstring (`config.py:1-6`).
- `src/brain/ranking.py` — **new file** (~70 LOC): `Candidate`,
  `recency_factor`, `rank` as specified. Pure; imports only `dataclasses`,
  `datetime`, and defaults from `.config`.
- `src/brain/db.py:188-193` — `_rrf`: change default `k=60` to `k=RRF_K`
  (import from config); body unchanged.
- `src/brain/db.py:257-264` — replace the raw-score accumulation/sort:
  per kind, keep building `lists` exactly as now (db.py:259-261), record
  `n_lists = len(lists)`, collect `Candidate`s from `_rrf(lists)`, batch-fetch
  `ts` for that kind's candidate ids, then call `ranking.rank(candidates,
  now=datetime.now(timezone.utc))`.
  **Re-anchor note (this feature lands SECOND, after recall-filters):** these
  db.py:257-298 line numbers are relative to *current HEAD*, but recall-filters
  edits the exact same range first — it adds `ids=ids_by_kind[kind]` to the
  `fts_search`/`vec_search` calls at db.py:259-261 and `filters` /
  `filter_candidates` keys to the return at db.py:298. When landing, re-anchor
  to the **post-recall-filters** source: preserve its `ids=` arguments on the
  leg calls (build `Candidate`s from the already-filtered `_rrf` output) and
  keep its response keys, adding `score` alongside them (no key collision).
- `src/brain/db.py:267-297` — hit assembly loop: iterate ranked results
  instead of `scored`; add `"score": round(score, 4)` to both the episode
  (db.py:272-281) and memory (db.py:286-297) hit dicts.
- `src/brain/server.py:24-30` — one docstring line on the `recall` tool
  noting the `score` field and its (0,1] range.
- `pyproject.toml:15` (after `[project.scripts]`) — add
  `[dependency-groups] dev = ["pytest"]` (uv installs the dev group by
  default; venv stays uv-managed per design rule (c)).
- `tests/test_ranking.py` — **new file**, repo-root `tests/` dir (~120 LOC).

## Implementation steps (ordered, each independently verifiable)

**Build order (this group):** recall-filters → ranking-weights →
auto-recall-hook. Land this feature **second**, on top of recall-filters. Its
scoring rewrite sits over already-filtered legs, so re-anchor the db.py:257-298
edits to the post-recall-filters source (see the Files-touched re-anchor note),
not to current HEAD.

1. **config.py constants.** Add `_env_float` + the four constant groups.
   Verify: `uv run python -c "from brain.config import KIND_WEIGHTS,
   HALF_LIFE_DAYS, RECENCY_FLOOR, RRF_K; print(KIND_WEIGHTS)"` prints
   defaults; re-run with `BRAIN_KIND_WEIGHT_EPISODE=0.5` and see the
   override take.
2. **`src/brain/ranking.py`.** Implement `Candidate`, `recency_factor`,
   `rank` as pure functions. Verify: `uv run python -c` snippet scoring one
   hand-built candidate reproduces the formula by hand (e.g. rank-0-in-both,
   fresh memory → exactly 1.0).
3. **Tests.** Add `[dependency-groups] dev = ["pytest"]`; write
   `tests/test_ranking.py` (cases below). Verify: `uv run pytest -q` green.
   Concrete cases (fixed fake candidates, fixed `now`, explicit constants):
   1. `test_kind_prior_ordering` — memory/summary/episode, identical
      `rrf=2/61, n_lists=2, ts=now` → order memory, summary, episode with
      scores exactly 1.0, 0.85, 0.70.
   2. `test_recency_reorders_within_kind` — two episodes, equal rrf, ts=now
      vs now−90d (half-life 45) → fresh first; stale score ==
      `0.70 * (0.35 + 0.65*0.25)` = 0.358750 ± 1e-9.
   3. `test_memory_decays_slower` — one memory and one episode, both 180d
      old, equal rrf → memory recency factor `0.675` vs episode
      `0.35 + 0.65*2**-4 = 0.390625`; memory ranks first.
   4. `test_normalization_and_range` — rank-0-in-both fresh memory → 1.0;
      same but present in only one of two lists (`rrf=1/61, n_lists=2`) →
      0.5; FTS-only mode (`rrf=1/61, n_lists=1`) → 1.0.
   5. `test_missing_ts_and_absolute_stability` — `ts=None` → recency ==
      floor and item ranks below its fresh twin; removing an unrelated
      candidate leaves every other score bit-identical.
4. **Wire into `db.recall`.** Candidates + ts batch-fetch + `ranking.rank`,
   `score` on hits, `RRF_K` into `_rrf`. Verify against the real index:
   `uv run python -c "from brain import db; c=db.connect();
   [print(h['score'], h['kind'], h['date']) for h in
   db.recall(c,'postgres backup')['hits']]"` — scores in (0,1], memories/recent
   items visibly promoted, no exceptions in FTS-only mode
   (`mv` sqlite-vec aside or use a vec-less DB copy to check the
   `n_lists=1` path).
5. **Docstring + README touch-up.** One line in `server.py` recall docstring;
   optionally mention `score` where README describes the tools
   (README.md:3-6). Verify: `uv run brain-server` boots; a `recall` call via
   MCP client (or `fastmcp` dev call) shows the field.

## Risks & open questions

- **Memory ts is file mtime** (`ingest.py:213-215`): a fresh `git clone` or
  branch checkout rewrites mtimes, making every memory look brand new. The
  error direction is benign (curated facts get max recency), but it means
  memory decay is soft. Real fix — a `date:`/`updated:` frontmatter field or
  `git log -1 --format=%cI` at ingest — is an ingest change, out of scope
  here; the constants make it a drop-in later.
- **Untuned priors.** There is no eval set; 45/90/180d and 0.70/0.85/1.0 are
  argued, not measured. Mitigation: single source of truth in config.py with
  env overrides; revisit after the auto-recall hook produces real
  hit/miss feedback.
- **Score inflation in FTS-only mode** (compressive RRF): documented above;
  the hook must branch on `fts_only`. If that proves annoying, a future
  tweak could scale scores by a per-mode constant — not done now.
- **Sidechain episodes** (`is_sidechain=1`) are currently weighted like
  main-thread episodes; subagent chatter may deserve a further penalty (see
  open questions).
- **Ties**: equal scores are possible (identical rrf/kind/age); deterministic
  tie-break by `(kind, rid)` prevents flaky ordering **within a single index
  build**. `rid`s are ephemeral across re-ingest (README.md:37), so the tie
  order is not stable across rebuilds — deterministic per build, not forever
  (fine for tests, which run against a fixed build).

Open questions for the user:

1. Should sidechain episodes get an extra multiplicative down-weight (e.g.
   0.8), or is kind+recency enough for v1?
2. Is a 0.35 recency floor acceptable — i.e. an arbitrarily old exact match
   retains 35% of its recency term — or should old content decay harder
   (lower floor) now that git remains the durable fallback?

## Size estimate (S/M/L and rough LOC)

**M** overall, driven by tests. Runtime code is **S**: ~20 LOC config,
~70 LOC `ranking.py`, ~25 LOC delta in `db.recall`, ~2 LOC server docstring.
Tests ~120 LOC + 2 LOC pyproject. Total ≈ 240 LOC, no schema or migration
work, one new pure module.

## Adversarial review (verifier)

**Verdict: SOUND WITH FIXES**

Re-derived all arithmetic — it holds: raw RRF range [0.0100, 0.0328]
(db.py:188-193, k=60, two 40-item lists); rrf_norm ≤ 1 exactly (max rrf_raw =
n_lists/61); the 0.65 worked example and every test constant (0.35875,
0.390625, 0.675, 0.5) reproduce. The 45d half-life is **not** a no-op: hybrid
rrf_norm for both-list items is 61/(61+r), so ranks 0–39 span only 1.0→0.61
while recency spans 1.0→0.35 — recency genuinely reorders within a kind. All
anchors verified (db.py:188-193/257-264/267-297, ingest.py:125/213-215,
config.py:38-40, server.py:24-30); ts formats confirmed against the live DB.

1. **RESOLVED:** Score semantics restated as "absolute for a fixed candidate
   universe (unfiltered recall)"; filtered-recall scores are documented as
   inflated and not threshold-comparable, and the recall-filters
   parallel-feature note corrected to match.
   **The "per-item absolute" claim is false once recall-filters lands**
   (lines 101-105, 163-165). rrf_norm depends on an item's rank *within each
   leg's returned list*; recall-filters filters the legs, so removing
   competitors promotes survivors' ranks — a row at rank 30 unfiltered becomes
   rank 0 under a selective filter and its rrf_norm jumps toward 1.0. Fix:
   restate as "absolute for a fixed candidate universe (unfiltered recall)"
   and document that filtered-recall scores are not threshold-comparable.
2. **RESOLVED:** The KIND_WEIGHTS["episode"] rationale now says rank ≤ ~26 in
   *both* legs (or rank ≤ 1 in one plus presence in the other; one-leg caps at
   0.5), and explicitly calls it a weaker bar than "top-2 consensus".
   **"~top-2 consensus" (line 78) mischaracterizes the 0.70 bar.** In hybrid
   mode rrf_norm ≥ 0.70 means rank ≤ ~26 in *both* lists (61/(61+r) ≥ 0.7), or
   rank ≤ 1 in one list plus presence anywhere in the other; a one-list-only
   item caps at 0.5. Fix: say "rank ≤ ~26 in both lists" — 0.70 is a far
   weaker evidence bar than "top-2".
3. **RESOLVED:** Mode caveat now states `fts_only` is response-global while
   per-kind leg count is not (empty vec table → n_lists=1 under
   `fts_only: false`, db.py:248), so a response can mix the 0.61/0.305 floors
   and any `fts_only`-keyed threshold is only approximate; notes per-hit
   `n_lists` as a possible later surface.
   **Global `fts_only` flag vs per-kind n_lists.** In hybrid mode a kind
   whose vec table is empty fuses with n_lists=1 (db.py:248 `_vec_nonempty`
   gate) while the response still says `fts_only: false`, so one response can
   mix the 0.61 and 0.305 compression floors. Fix: surface n_lists (or the
   floor) per hit, or note the fts_only-based threshold advice is approximate
   for mixed-mode responses.
4. **RESOLVED:** The Ties risk now claims determinism only within a single
   index build and notes rids are ephemeral across re-ingest.
   Minor: the `(kind, rid)` tie-break is claimed deterministic across
   "re-runs" (line 277-278), but rids are ephemeral across re-ingest
   (README.md:37). Fix: claim determinism only within one index build.

### Group integration

1. **RESOLVED:** Build order fixed as recall-filters → ranking-weights (this
   spec lands second); Files-touched and Implementation-steps now carry an
   explicit re-anchor note to rebase the db.py:257-298 edits onto the
   post-recall-filters source, preserving its `ids=` args and response keys.
   **Same-lines collision.** recall-filters edits the leg calls at
   db.py:259-261 and the return at db.py:298; this spec replaces db.py:257-264
   and rewrites 267-297. Both anchor "current HEAD" — whichever lands second
   must re-anchor. Semantically composable (filters upstream of fusion,
   ranking downstream); no key collisions in the response dict.
2. **RESOLVED:** Score semantics and the auto-recall-hook parallel-feature note
   now state the hook keeps a token-overlap gate (not a score cutoff) and may
   reuse `ranking.py` only for ordering; the "gate becomes a score cutoff"
   framing is removed from both specs.
   **Score semantics do not transfer to the hook's fast path.** The hook
   never calls `db.recall` — it runs raw FTS with LIMIT 8 (hook spec step 4).
   A reconstructed FTS-only score there has rrf_norm = 61/(61+r) ∈
   [0.884, 1.0]: an 11.6% spread carrying zero BM25 magnitude, so a
   "normalized-score cutoff" (hook step 5 assumption; this spec's lines
   97-111 contract) degenerates to kind×recency gating and cannot replace the
   hook's token-overlap gate. The ~0.35–0.5 hybrid threshold band applies only
   if the hook upgrades to full hybrid recall — rejected at 0.71 s. Both specs
   should drop the "gate becomes a score cutoff" framing.
3. **RESOLVED:** Score semantics now explicitly forbid comparing filtered-recall
   scores against unfiltered thresholds; the hook passes no filters so its own
   gating is unaffected.
   **Filters × scores** (issue 1): the hook passes no filters, so its
   thresholds are safe, but scores from filtered recalls must never be
   compared against unfiltered/hook thresholds.
4. **RESOLVED:** Build order recall-filters → ranking-weights →
   auto-recall-hook is now stated in all three specs' implementation steps,
   with this spec's re-anchor note for the shared db.py:257-298 range.
   **Recommended build order: recall-filters → ranking-weights →
   auto-recall-hook.** Filters first — purely additive, and its
   "no-filter response byte-identical" regression check stays meaningful;
   ranking second — rewrites the scoring block once, over already-filtered
   legs, fixing issue 1's wording as it lands; hook last — independent code
   paths (`connect_ro` + new module), and it can then reuse `ranking.py` for
   recency/kind *ordering* of its hits while keeping the overlap gate.
