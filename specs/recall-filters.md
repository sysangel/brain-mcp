# recall filters: project + date range

## Problem

`recall(query, k, scope)` searches the whole index. Queries like "what did we
do in the mobile repo last week" can't be expressed: the query text has to
carry the project and time constraints, which BM25/embeddings honor only
loosely. The caller needs hard metadata filters — **project** and **date
range** — applied to *both* legs of the hybrid search (FTS5 and sqlite-vec
KNN) *before* RRF fusion, with honest results when the filter is highly
selective (no silent fallback to unfiltered).

Constraints from the design rules: markdown+git stays the source of truth
(`cache/brain.db` rebuildable), the MCP tool surface stays token-cheap (tight
signature, tiny docstring), uv venv only.

## Design (chosen approach, plus alternatives rejected and why)

### Chosen: candidate-id prefilter pushed into both legs via `rowid IN (json_each(?))`

One new helper in `db.py` computes the filtered candidate set per kind, then
both search legs restrict to it:

1. **`_filter_ids(conn, kind, project, since, until) -> list[int] | None`**
   Returns `None` when no filter is active (fast path — behavior identical to
   today, zero regression). Otherwise returns the ids of rows passing the
   filters:
   - *Dates* (day granularity): plain SQL — `substr(ts,1,10) >= :since AND
     substr(ts,1,10) <= :until` (inclusive). Works for both stored formats:
     episodes `2026-07-02T18:26:40.605Z` and memories
     `2026-07-02T17:14:59.…+00:00` — both open with `YYYY-MM-DD`, so the
     10-char prefix compares lexicographically. **NULL `ts` is excluded when a
     date bound is active** (intentional): `substr(NULL,1,10) >= :since`
     evaluates to NULL (falsy), so a row with no timestamp cannot satisfy a
     bounded range. An untimestamped episode (ingest.py:125 passes the raw
     jsonl `timestamp` through, which is occasionally absent) has no date to
     place in a range, so a date-filtered recall correctly omits it. With no
     date bound active, NULL-`ts` rows are untouched (only project filtering,
     if any, applies) — so the fast/no-date path never drops them.
   - *Project*: fuzzy matching happens in **Python over the distinct
     project values** (small: `SELECT DISTINCT cwd FROM episodes` is the
     number of working dirs, `SELECT DISTINCT path FROM memories` is the
     number of memory files), producing an exact `cwd IN (...)` /
     `path IN (...)` SQL clause. This keeps the matching contract in one
     readable Python function instead of SQL string gymnastics, and reuses
     the same project-derivation logic as the existing `_project_of()`.

2. **Both legs take an optional `ids` list** and append
   `AND rowid IN (SELECT value FROM json_each(?))` (single bound param,
   `json.dumps(ids)`); `ids == []` short-circuits to `[]` without querying.
   - FTS5 leg: `... WHERE {table} MATCH ? AND rowid IN (SELECT value FROM
     json_each(?)) ORDER BY rank LIMIT ?` — verified working, rank order
     preserved.
   - vec leg: `... WHERE embedding MATCH ? AND k = ? AND rowid IN (SELECT
     value FROM json_each(?))` — **verified against the installed sqlite-vec
     v0.1.9** (from `uv.lock`): literal lists, `json_each(?)` subqueries, and
     table subqueries all work as true KNN pre-filters; an empty set returns
     empty cleanly. No over-fetch needed: the KNN runs *over the filtered
     rows only*, so the top-`limit` filtered neighbors are exact, even for a
     1-row candidate set. (vec0 in 0.1.x is brute-force scan anyway, so a
     restricted scan is also strictly faster.)

3. **RRF is untouched** — it fuses whatever rank lists the legs return, and
   both lists are now pre-filtered, so fusion happens over the filtered
   candidate sets by construction (requirement: filter-then-fuse, never
   fuse-then-filter).

4. **Honesty**: when any filter is active the response gains
   `"filters": {…resolved values…}` and `"filter_candidates": <int>` so an
   empty or short hit list is visibly "filtered down to N candidates", never
   a silent unfiltered fallback.

### Project matching contract

Normalize both the filter and the row's project path to a hyphen-bounded,
lowercase, hyphen-separated segment string, then substring-match:

```
norm(s) = "-" + lower(s).replace("/", "-").strip("-") + "-"
match   = norm(filter) in norm(row_project)
```

`row_project` is:
- **episodes**: the full `cwd` (e.g. `/home/you/src/webapp` →
  `-home-you-src-webapp-`);
- **memories**: the transcript dir name when `path` is under
  `CLAUDE_PROJECTS` (e.g. `-home-you-src` → `-home-you-src-`
  — the munged name is already hyphen-separated, so it normalizes to the
  same form as a cwd), else the literal `brain` (→ `-brain-`) for
  `<BRAIN_DIR>/memories/*.md`, mirroring `_project_of()` at db.py:217.

Examples against the current index (60 episodes, 9 memories); rows marked
*illustrative* are not present in that index but show the intended semantics:

| filter            | norm            | matches |
|-------------------|-----------------|---------|
| `mobile`          | `-mobile-`      | *illustrative* — would match any session under `~/src/mobile`; **no such episode is in the current index** (only `…/src` ×58 and `…/webapp` ×2 exist) |
| `webapp`            | `-webapp-`        | the 2 episodes with cwd `/home/you/src/webapp` |
| `src`             | `-src-`         | all 58 `/home/you/src` episodes **and** the webapp ones (parent segment matches — documented semantics: "anything under src") |
| `src/webapp`        | `-src-webapp-`    | just webapp (multi-segment) |
| `brain`           | `-brain-`       | the 4 memories in `~/src/brain/memories/` (+ any future episodes with cwd `…/brain`) |
| `MOBILE`          | `-mobile-`      | same as `mobile` — case-insensitive |

Deliberate properties: matching is **segment-bounded** (`photos` does not
match `you`), **parent segments match** (a feature: "the src dir"),
and it matches only the *project path*, never memory file names (a memory
named `mobile-cheatsheet.md` is NOT hit by `project="mobile"` —
only its containing project dir is considered). Known fuzz: because `norm()`
rewrites `/` to `-`, a segment of a **hyphen-named repo** matches as if it
were its own path segment (`repo` matches `my-repo`). This is a property of
the normalization, **not** of the munged transcript-dir format, so it applies
equally to an episode's real `cwd` (`…/src/my-repo` → `-…-src-my-repo-`, which
`repo` matches) and to a munged memory dir. Harmless in practice.

### Date syntax

`since` / `until` accept exactly two forms:
- ISO date `YYYY-MM-DD` (absolute), and
- `<N>d` = N days before today, UTC (relative; `"7d"` = last week,
  `"0d"` = today).

Both bounds inclusive, day granularity (episode `ts` is compared on its date
prefix). Anything else raises `ValueError` (same style as the existing scope
validation at db.py:239). Two forms only — every extra documented format
costs tokens in every session; hours/weeks/"yesterday" add nothing that
`Nd`+ISO can't express at day granularity.

"What did we do in the mobile repo last week" ⇒
`recall("what we did", project="mobile", since="7d")`.

### Alternatives rejected

1. **vec0 metadata columns** (`embedding float[256], project text, day int`):
   supported by v0.1.9 and properly pushed into KNN, but only with
   `=, !=, <, <=, >, >=` — `LIKE` on a metadata column **errors**
   ("illegal WHERE constraint… in a KNN query", verified), so segment
   matching (`mobile` ⇒ `/home/you/src/mobile`) is impossible
   without pre-resolving to exact values anyway. Also requires a vec-table
   schema change + `brain-ingest --full` for every user, duplicates
   cwd/ts into the index twice, and still special-cases memories. All cost,
   no capability over `rowid IN`.
2. **Over-fetch k×N then post-filter in Python** (the standard fallback when
   a vector store can't pre-filter): strictly worse here because pre-filter
   IS supported. Failure mode: a selective filter (say 1% of rows) with
   N=10 over-fetch finds ~0.4 relevant rows in a 40-candidate fetch —
   silently misses hits that exist. Bounding the miss requires N ≈ 1/selectivity,
   unbounded in the worst case. Kept only as the documented escape hatch if
   sqlite-vec is ever replaced by an engine without pre-filtering.
3. **Filter after RRF** (filter the final scored list): violates
   filter-then-fuse — starves the result below k whenever the top of the
   fused list is dominated by out-of-filter rows, and re-ranks nothing.
4. **Expression filters (`instr`, `LIKE` on cwd) inline in the vec KNN
   query**: SQLite applies non-pushable expressions *after* the KNN returns
   k rows — verified: `k=3 AND instr(proj,'kill')` silently returned 2 rows.
   Same starvation trap as (3), just hidden in the query planner.
5. **Stored normalized `project` column on episodes/memories** (ingest-time):
   cleanest SQL, but a schema migration (ALTER + backfill or forced full
   rebuild) for something the DISTINCT-scan does in microseconds at this
   scale. Revisit only if the DISTINCT scan ever shows up in profiles (see
   Risks).

## API / schema changes

**Schema: none.** No new tables, columns, or ingest changes; no rebuild
required. (Everything needed — `episodes.cwd`, `episodes.ts`,
`memories.path`, `memories.ts` — is already stored per row: db.py:29-50,
populated at ingest.py:308-323 and ingest.py:213-223.)

**MCP tool signature** (server.py):

```python
@mcp.tool
def recall(
    query: str,
    k: int = 6,
    scope: str = "all",
    project: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    """Search past Claude Code sessions (episodes) and memory notes (memories).

    Hybrid full-text + semantic search. scope: "all" | "episodes" | "memories".
    Filters: project matches path segments of the session dir (e.g. "mobile"
    => ~/src/mobile); since/until are "YYYY-MM-DD" or "Nd" (N days ago), inclusive.
    Returns compact hits: kind, id, date, project, snippet, and session_id
    (episodes) or name (memories). Use get_episode(id, kind) for full text.
    """
```

(+2 docstring lines, +3 optional params — the whole token cost of the feature.)

**`db.recall` signature**: `recall(conn, query, k=6, scope="all",
project=None, since=None, until=None)`.

**Response shape**: unchanged when no filter is given. When any filter is
active, two keys are added:

```json
{
  "fts_only": false,
  "filters": {"project": "mobile", "since": "2026-06-26"},
  "filter_candidates": 12,
  "hits": [ ...as today, possibly fewer than k, possibly empty... ]
}
```

`filters` echoes *resolved* values (`"7d"` → the actual `YYYY-MM-DD` cutoff)
so the caller can see exactly what was applied. `filter_candidates` is the
total row count passing the filters (pre-search, summed over the kinds in
scope) — `0` makes "nothing matches your filter" unambiguous versus "matches
exist but none are relevant to the query". Invalid `since`/`until`/`project`
values raise `ValueError` (FastMCP surfaces it as a tool error), matching the
existing scope validation.

## Files touched (file:line anchors into current code)

- **`/home/you/src/brain/src/brain/db.py`** (all line refs to current HEAD):
  - db.py:1-7 — module docstring: one line noting filtered recall mechanics.
  - New `_parse_date_bound(s: str) -> str` (~10 LOC) near the search section
    (db.py:139).
  - db.py:217-227 `_project_of` — extract the shared normalization into new
    helpers `_norm_segments(s: str) -> str` and
    `_project_matches(filter: str, row_project: str) -> bool`; new
    `_memory_project(path: str) -> str` reusing the CLAUDE_PROJECTS-relative
    logic currently inline at db.py:222-227 (so display and filtering can't
    drift apart).
  - New `_filter_ids(conn, kind, project, since, until) -> list[int] | None`
    (~30 LOC): DISTINCT scan + Python match → `cwd IN`/`path IN` clause +
    date clauses → id list.
  - db.py:150-165 `fts_search` — add `ids: list[int] | None = None` param;
    build the optional `AND rowid IN (SELECT value FROM json_each(?))` into
    `sql` at db.py:155 once, so the phrase-fallback retry at db.py:158-164
    inherits it; `ids == []` → return `[]` early.
  - db.py:176-185 `vec_search` — same `ids` param; extend the KNN query at
    db.py:180.
  - db.py:230-264 `recall` —
    - db.py:235-239: after scope validation, parse bounds and compute
      `ids_by_kind = {kind: _filter_ids(...)}`; validate here so errors beat
      the embedding call.
    - db.py:257-263 **(the RRF plug point)**: the loop currently builds
      `lists = [fts_search(conn, kind, query, limit=40)]` then appends
      `vec_search(...)` and fuses via `_rrf(lists, k=60)` — pass
      `ids=ids_by_kind[kind]` into *both* calls; `_rrf` (db.py:188-193)
      itself is untouched and now fuses filtered rank lists.
    - db.py:298 return: add `filters` echo + `filter_candidates` when any
      filter is active.
- **`/home/you/src/brain/src/brain/server.py`**:
  - server.py:23-36 `recall` tool — signature + docstring as above; pass
    the three params through to `db.recall` at server.py:33.
- **`/home/you/src/brain/README.md`** — one line under the MCP tools
  description (README.md:3-6) noting the optional filters.

Not touched: `ingest.py`, `config.py`, `embed.py`, schema (db.py:19-83),
`get_item`.

## Implementation steps (ordered, each independently verifiable)

**Build order (this group):** recall-filters → ranking-weights →
auto-recall-hook. This spec lands **first**: it is purely additive (new
helpers, two threaded `ids` params, two response keys), and its "no-filter
response byte-identical to today" regression check (step 5) is only meaningful
while nothing has yet rewritten the scoring block. It edits the leg calls at
db.py:259-261 and the return at db.py:298; **ranking-weights lands second over
these same lines and re-anchors to them** (see its Files-touched re-anchor
note). No re-anchoring is needed here.

1. **Date parsing** — add `_parse_date_bound` to db.py.
   Verify: `uv run python -c "from brain.db import _parse_date_bound as p;
   print(p('2026-07-01'), p('7d'))"` → `2026-07-01 2026-06-26`; `p('lastweek')`
   raises ValueError.
2. **Project normalization/matching helpers** — `_norm_segments`,
   `_project_matches`, `_memory_project`; refactor `_project_of`
   (db.py:217-227) onto `_memory_project`. Verify with a REPL table:
   `("mobile", "/home/you/src/mobile") → True`,
   `("photos", "/home/you/src") → False`,
   `("src", "-home-you-src") → True`, `("brain", "brain") → True`;
   plus `db.recall(...)` output `project` fields unchanged vs. before the
   refactor.
3. **`_filter_ids`** — verify against the live cache DB:
   `_filter_ids(conn, "episode", "webapp", None, None)` → exactly the 2 ids
   with cwd `/home/you/src/webapp`; `("episode", None, "1d", None)` →
   only episodes dated yesterday-or-later, and any **NULL-`ts` episode is
   excluded** (null a row's `ts` in a scratch copy: it must drop out under a
   date bound but still survive a project-only filter);
   `("memory", "brain", None, None)` → the 4 `~/src/brain/memories` ids; no
   filters → `None`.
4. **Thread `ids` through `fts_search` / `vec_search`** — verify each leg in
   isolation on the live DB: leg results with `ids=None` byte-identical to
   today; with a 2-id list, results ⊆ that list; with `ids=[]`, `[]`; FTS
   phrase-fallback path (punctuation-heavy query) also respects `ids`.
5. **Wire `db.recall`** (parse → `ids_by_kind` → legs → response keys).
   Verify: `db.recall(conn, "ingest", project="webapp")` returns only
   `project == "webapp"` hits with the `filters`/`filter_candidates` keys;
   `db.recall(conn, "ingest")` returns exactly today's unfiltered response
   (no new keys); `project="nosuchproj"` → `hits: []`,
   `filter_candidates: 0`; `since="2100-01-01"` → same; `scope`/filter
   combinations (`scope="memories", project="brain"`) behave.
6. **Expose in `server.py`** + README line. Verify end-to-end over MCP:
   `uv run brain-server` and call `recall` with `{"query":"mobile repo",
   "project":"mobile","since":"7d"}` via a Claude Code session (or FastMCP's
   in-memory client) — filtered hits round-trip, invalid dates surface as a
   tool error, and the no-filter call is unchanged (token cost check: tool
   schema grows by only the 3 optional params).

Each step lands as its own small commit-sized change; steps 1-3 are pure
additions verifiable before any existing code path changes.

## Risks & open questions

- **Candidate-list size at scale.** `_filter_ids` materializes matching ids
  and ships them as one JSON param per leg. At the current 60 episodes this
  is nothing; at ~50k episodes a broad filter (`project="src"`) is a ~350 KB
  JSON string built twice per kind per call — fine for a personal corpus,
  but the ceiling exists. Escape hatch (no API change): swap the JSON param
  for a temp-table join, or promote to a stored normalized `project` column
  (rejected alternative 5).
- **Day granularity + UTC.** `ts` is UTC; "7d" and date-prefix comparison use
  UTC days, so late-evening local sessions (UTC-4/5) can land on the
  "wrong" day near boundaries. Accepted for a recall tool; not worth
  timezone plumbing in the signature.
- **Memory dates are file mtimes** (ingest.py:213-215), not content dates —
  editing a memory refreshes its date, so `since` on memories means
  "recently touched". Honest to how the index works, but worth a docstring
  word if it surprises.
- **Hyphen-in-segment ambiguity.** `norm()` rewrites `/`→`-`, so a
  hyphen-named path segment reads as two segments: suffix-segment matches
  (`repo` ⇒ `my-repo`) are accepted fuzz. This affects **both** memory dirs
  under CLAUDE_PROJECTS **and** episode `cwd`s (a real repo literally named
  `my-repo` matches `repo` too) — it is a normalization property, not a
  munged-transcript-dir one. Eliminating it for episodes would require
  matching cwds on real (unmunged) path segments; deferred as harmless.
- **Parallel-feature assumptions** (one line each, not designed here):
  *ranking-weights* — adjusts fusion weighting over the same per-leg rank
  lists. **Not orthogonal:** filtering the legs promotes survivors' per-leg
  ranks, so a filtered recall inflates `rrf_norm` (and thus the `score`
  ranking-weights attaches) relative to the same hit in an unfiltered recall.
  Filtered-recall scores are therefore **not threshold-comparable** with
  unfiltered ones (see the Group-integration note in ranking-weights.md).
  *session-summaries* — `kind="summary"` mirrors the episode layout with
  cwd/ts, so `_filter_ids` extends with one kind→(table, project-column)
  mapping entry. *stable-ids* — ids remain SQLite rowids in the index
  (stability is an ingest concern); `json_each` id-list mechanics unaffected.
- **Open question (user):** should `project` also match against memory
  *names* as a convenience (e.g. `project="webapp"` hitting the
  `webapp-state` memory even if it lived elsewhere)? Current answer:
  no — project means "where the work happened", name matching is the query
  text's job. Flag if the stricter contract feels wrong in use.

## Size estimate (S/M/L and rough LOC)

**S.** ~100 LOC total: db.py ≈ +80 (three small helpers, two threaded
params, response keys), server.py ≈ +6/-2 (signature + docstring), README
+1. No schema change, no migration, no rebuild, no new dependencies. The
risky primitives (`rowid IN (json_each(?))` on vec0 v0.1.9 and FTS5, empty
sets, metadata-column limitations) were verified against the installed stack
before this spec was written.

## Adversarial review (verifier)

**Verdict: SOUND WITH FIXES**

Independently re-verified the load-bearing primitives against the installed
sqlite-vec v0.1.9 (uv.lock:1720): `rowid IN (SELECT value FROM json_each(?))`
acts as a true KNN pre-filter (exact result on a 1-row candidate set; empty
set returns `[]` cleanly), and the FTS5 external-content leg honors
`rowid IN` with `ORDER BY rank`. Live-DB claims all check out: 60 episodes /
9 memories, 2 webapp-cwd episodes, 58 under `/home/you/src`, 4 memories
in `~/src/brain/memories/`, ts formats exactly as stated. db.py / server.py /
ingest.py anchors verified; `7d → 2026-06-26` date math correct.

1. **RESOLVED:** Date-filter design now states NULL-`ts` rows are
   intentionally excluded when a date bound is active (and untouched when none
   is), with a matching step-3 verification.
   **NULL-`ts` rows are silently dropped by any date filter.**
   `substr(ts,1,10) >= :since` evaluates to NULL when `ts IS NULL` — episodes
   can store a missing timestamp (ingest.py:125 passes the raw jsonl value
   through; the ranking spec documents the None case; today's DB just happens
   to have zero). Fix: state the intended NULL behavior (excluding is
   reasonable — say so) and cover it in step 3's verification.
2. **RESOLVED:** The "Deliberate properties" and Risks notes now attribute the
   suffix-segment fuzz to the `/`→`-` normalization and state it applies to
   episode cwds and munged memory dirs alike (no false "episodes use true cwd"
   claim).
   **The Risks claim "Only affects memories… episode matching uses the true
   `cwd`" is wrong.** `_norm_segments` applies `replace("/", "-")` to episode
   cwds too, so `project="repo"` matches an episode cwd
   `/Users/x/src/my-repo` exactly as it matches munged memory dirs — the fuzz
   is a property of the normalization, not of the munged transcript-dir
   format. Fix: correct the risk note (or match episode cwds on real path
   segments, which eliminates the fuzz for episodes).
3. **RESOLVED:** The table intro now says rows marked *illustrative* are not
   in the current index, and the `mobile` row is so marked.
   Minor: the "Real examples from the current index" table's `mobile` row
   matches nothing in the current index — no episode has a cwd under
   `~/src/mobile` (only `/home/you/src` ×58 and `…/webapp` ×2 exist).
   Fix: mark that row as illustrative, not current-index.
4. **RESOLVED:** The ranking-weights parallel-feature note now drops
   "orthogonal" and states filtered-recall scores are inflated (survivors'
   ranks promoted) and not threshold-comparable with unfiltered ones.
   Cross-spec: calling ranking-weights "orthogonal" understates the
   interaction — filtering the legs changes survivors' per-leg ranks and
   therefore their future `score` values (see "Group integration" appended to
   ranking-weights.md). Fix: replace "orthogonal" with a note that scores
   from filtered recalls are inflated relative to unfiltered ones.
