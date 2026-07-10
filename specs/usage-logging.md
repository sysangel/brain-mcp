# Spec: recall-usage logging that feeds reflect

Status: proposed · Depends on: stable content-derived ids (spec'd separately)

> **Stated assumption (dependency):** this spec assumes the stable-ids feature
> has landed first. Per its §Id contract, `recall` hits and `get_episode`
> results expose opaque, content-derived stable ids instead of rowids:
> episodes carry `ep_`+16hex keyed on `(sessionId, uuid)`; memories carry
> `mem_`+16hex keyed on absolute **path** (frontmatter `name` was explicitly
> rejected there). Every log record below stores **those stable ids only,
> never rowids**. If stable ids are not yet merged, do not implement this
> spec; rowid-keyed usage logs are worthless after the first re-ingest.

## Problem

The brain accumulates memories but has no feedback loop: nothing records
which memories actually get recalled and opened, and which queries came back
with nothing good. So reflect (the nightly consolidation pass) is flying
blind on three questions it is uniquely positioned to act on:

1. **Which memories earn their keep?** Memories never surfaced by recall in
   weeks are demotion/merge candidates for the Compact step.
2. **Which memories should exist but don't?** A recall whose best hit was
   weak (or empty) is a *gap* — evidence someone needed a fact the store
   doesn't hold. The gap list is a ranked shopping list for new memories.
3. **What deserves promotion?** A `get_episode` call is the strongest
   usefulness signal we have — the caller liked the snippet enough to pull
   full text. Repeatedly-opened episodes deserve distillation into a memory.

Two constraints make this non-trivial:

- **Usage logs are not rebuildable.** Rule (a) says `cache/` holds only
  derived, freely-deletable state. Telemetry derived from *tool calls* (not
  from the markdown corpus) cannot be regenerated, so it cannot honestly live
  under the `cache/` contract.
- **Writes must be nearly free.** The auto-recall hook (spec'd separately)
  will call recall on many/most user prompts. Logging must add ~zero latency
  to `recall`, never block on locks, and never turn a telemetry failure into
  a recall failure. Ingest writes `cache/brain.db` concurrently; multiple
  Claude Code sessions each run their own `brain-server` process, so several
  writers may hit the usage store at once.

## Design (chosen approach, plus alternatives rejected and why)

### Chosen: `local/usage.db` — a separate SQLite (WAL) telemetry DB in a new gitignored `local/` directory, written by an in-process queue + daemon writer thread, read by a new `brain-usage-report` CLI

**Storage location.** A new top-level directory `local/` (gitignored) with a
contract distinct from `cache/`:

- `cache/` — derived index. Rebuildable. Delete freely; `brain-ingest --full`
  recreates it. (Unchanged.)
- `local/` — machine-local, non-rebuildable, never-committed **lossy
  telemetry**. Losing it costs nothing but usage history (one degraded
  nightly report); no brain command ever deletes it, but the user may.

Working the tension in rule (a) through honestly: usage logs break the "not
in git ⇒ rebuildable" invariant no matter where they live locally. The
resolution is to make *lossiness part of the data's contract* — usage
telemetry is a heuristic signal, and losing 90 days of it merely weakens one
report. What we must NOT do is park it in `cache/`, because
"`rm -rf cache/ && brain-ingest --full`" is the documented recovery move for
a misbehaving index, and telemetry should not be collateral damage of an
unrelated rebuild. One new gitignore line buys crisp, separate contracts.

**Format: SQLite over JSONL.** Reflect's report needs joins (memory universe
in `brain.db` × usage hits), grouping, and windowed aggregation — native SQL.
sqlite3 is already the stack; retention is one `DELETE`. Concurrent writers
(multiple `brain-server` processes, one per Claude session) are handled by
WAL + `busy_timeout`, vs. hand-rolled append/rotation semantics for JSONL.
Crucially `usage.db` is a *different file* from `brain.db`, so ingest's write
bursts never contend with usage writes.

**Write path: bounded queue + single daemon writer thread per process.** Tool
handlers call `usagelog.log_recall(...)` / `log_open(...)`, which do a
`queue.put_nowait` (sub-microsecond) and return. A daemon thread owns the
only connection, drains the queue, and inserts in small batches
(`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=2000`). Rules:

- Queue full (cap 1000) → drop the event silently. Lossy contract.
- Any exception in the writer (locked DB, disk full, missing dir) → log once
  to stderr, keep serving; **recall never fails or slows because of
  telemetry**.
- Process exit may drop the last few queued events — acceptable; a
  `flush(timeout)` helper exists for short-lived CLI callers (future
  `brain-recall` hook entry point).

**Weakness signal.** RRF scores are rank-based, not relevance-based (with a
small corpus, *something* is always rank 0), so the log captures two scores
per recall and "weak" is defined over both — see the schema section for the
concrete thresholds. This requires `db.vec_search` to return cosine
distances (one-line change) and `db.recall` to return a `diag` block that
`server.py` pops before returning to the MCP client (tool output unchanged).

**Retention: 90 days** (`USAGE_RETENTION_DAYS = 90`). Enforced by one
windowed `DELETE` (a) when the writer thread initializes at server startup
and (b) at the start of every `brain-usage-report` run. Scale check: even a
pathological 1,000 recalls/day × 90 days ≈ 90k recall rows + ~500k hit rows
≈ 50–80 MB worst case; realistic usage is 1–2 orders of magnitude smaller.
No vacuum scheduling needed; `PRAGMA journal_size_limit=4194304` keeps the
WAL bounded.

**Source tagging.** Every event carries `source ∈ {mcp, hook, cli}`. The MCP
tool handlers hardcode `"mcp"` today. The auto-recall hook (separate spec)
must arrive at `log_recall` with `source="hook"` — the interface here is
transport-agnostic; see Risks for the contingency per hook transport. This
matters twice: gap analysis wants hook queries included (auto-recall over
real prompts is exactly where gaps surface), while "most-opened" promotion
signals should weight explicit (`mcp`) opens above any hook-automated ones.

### Alternatives rejected

1. **Tables inside `cache/brain.db`.** Simplest wiring, but directly violates
   rule (a): `brain-ingest --full` unlinks the DB file (`ingest.py:247-252`),
   and the README instructs "delete it freely". Non-rebuildable data in a
   deliberately disposable file is a contract violation waiting to fire.
   Also couples telemetry writes to ingest's write bursts on the same file.
2. **Git-tracked log (e.g. `usage/` JSONL committed by reflect).** Puts
   high-frequency machine churn into the human-meaningful git history, bloats
   the repo forever (usage is append-heavy, low-value-per-byte), and creates
   commit noise/conflicts across future multi-machine use. Git is for the
   *distilled* output (memories), not the raw signal.
3. **JSONL in `local/` + rotation.** Viable, but reflect's queries become a
   parse-then-aggregate script instead of four SQL statements; concurrent
   appenders from multiple server processes rely on `O_APPEND` write
   atomicity; retention means file-juggling. SQLite gives all of it for free.
4. **Synchronous inserts in the tool handler.** A WAL insert is typically
   <1 ms, but a lock collision with another server process could stall up to
   `busy_timeout` inside a recall call. The queue makes worst-case handler
   overhead a constant sub-microsecond regardless of DB state.
5. **Log inside `db.recall()`.** Would tag every future caller identically
   and hide the `source` distinction. Logging stays at the entry points
   (server tool handlers, future hook/CLI entry), keeping `db.py` pure.

## API / schema changes (exact tables/records)

### New DB: `local/usage.db`

Connection pragmas (writer): `journal_mode=WAL`, `synchronous=NORMAL`,
`busy_timeout=2000`, `foreign_keys=ON`, `journal_size_limit=4194304`.

```sql
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);  -- holds schema_version='1'

CREATE TABLE IF NOT EXISTS recalls(
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,     -- UTC ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' (string-comparable, matches episodes.ts)
    source       TEXT NOT NULL CHECK (source IN ('mcp','hook','cli')),
    query        TEXT NOT NULL,     -- verbatim query text (see Privacy)
    scope        TEXT NOT NULL,     -- 'all' | 'episodes' | 'memories'
    k            INTEGER NOT NULL,
    fts_only     INTEGER NOT NULL,  -- 1 = vector path unavailable this call
    n_hits       INTEGER NOT NULL,
    top_score    REAL,              -- RRF score of best returned hit; NULL when n_hits=0 (logged, but never used to define "weak")
    top_vec_cos  REAL               -- cosine SIMILARITY (1 − d²/2) of the best vector hit across searched kinds; higher = better; NULL when fts_only=1 or no vec hits
);
CREATE INDEX IF NOT EXISTS recalls_ts ON recalls(ts);

CREATE TABLE IF NOT EXISTS recall_hits(
    recall_id INTEGER NOT NULL REFERENCES recalls(id) ON DELETE CASCADE,
    pos       INTEGER NOT NULL,    -- 0-based rank among returned hits; only pos 0..7 stored (USAGE_MAX_HITS_LOGGED=8)
    kind      TEXT NOT NULL,       -- 'episode' | 'memory'
    stable_id TEXT NOT NULL,       -- opaque stable id: episode 'ep_…' ((sessionId,uuid)-derived) or memory 'mem_…' (path-derived) — NEVER a rowid
    score     REAL NOT NULL,       -- RRF score of this hit
    PRIMARY KEY (recall_id, pos)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS recall_hits_sid ON recall_hits(kind, stable_id);

CREATE TABLE IF NOT EXISTS opens(
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL CHECK (source IN ('mcp','hook','cli')),
    kind      TEXT NOT NULL,       -- 'episode' | 'memory'
    stable_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS opens_sid ON opens(kind, stable_id);
CREATE INDEX IF NOT EXISTS opens_ts  ON opens(ts);
```

Logging rules:

- One `recalls` row + up to 8 `recall_hits` rows per successful `recall`
  call, in one transaction. The empty-query early return
  (`db.py:241-242`, no `\w` in query) is **not** logged.
- One `opens` row per successful `get_episode` (skip when the result carries
  an `"error"` key). An "open" is the strongest usefulness signal.
- Failed/raising tool calls are not logged (telemetry, not an audit log).

### New module API: `src/brain/usagelog.py`

```python
log_recall(*, query: str, scope: str, k: int, source: str,
           hits: list[dict], diag: dict) -> None   # put_nowait; never raises
log_open(*, kind: str, stable_id: str, source: str) -> None
flush(timeout: float = 1.0) -> None   # for short-lived CLI callers
# writer thread starts lazily on first log_* call; prunes >90d rows once at init
```

### Changed internal returns (MCP tool output unchanged)

- `db.vec_search` returns `list[tuple[int, float]]` (rowid, **L2 distance**)
  instead of `list[int]`. vec0's default metric is L2 (verified, sqlite-vec
  v0.1.9) and embed.py L2-normalizes vectors (embed.py:50-57), so recall
  converts the best distance to cosine similarity `cos = 1 − d²/2` for the
  diag — the interpretable, absolute-scale signal.
- `db.recall` result gains a `"diag"` key:
  `{"top_score": float|None, "top_vec_cos": float|None, "n_hits": int}`.
  `server.py` pops `"diag"` (and feeds it to `log_recall`) before returning,
  so the MCP tool surface and output schema are byte-identical to today.

### "Weak" — the concrete definition

A recall is **weak** (a gap-list candidate) when either of:

| condition | rationale |
|---|---|
| `n_hits = 0` | nothing matched at all |
| `top_vec_cos < 0.55` (when non-NULL) | cosine similarity of the best vector hit. Measured on the live index (L2-normalized nomic, query→doc): clearly-good matches score cos ≈ 0.64–0.75 (d ≈ 0.71–0.85), a nonsense query cos ≈ 0.51–0.54 (d ≈ 0.96–0.99); 0.55 sits just under the good band. Initial calibration value, exposed as `config.WEAK_VEC_COS` |

There is **no FTS-only weak branch.** In `fts_only` mode each kind fuses a
single rank list, so the top fused hit is *always* rank 0 and `top_score` is
the constant 1/61 ≈ 0.0164 — it carries zero information and cannot
discriminate at any threshold (review item 2). In that degraded mode only
`n_hits = 0` counts as weak; `top_score` is still logged (informational,
shown in the gap table) but never thresholded. FTS-only is a rare fallback
(embedder/vec unavailable), and the calibration block reports its share so it
stays visible.

The vector threshold is a declared calibration value, not a truth: the
report's `calibration` block (below) prints the window's `top_vec_cos`
distribution (min/p25/p50/p75/max) so it can be tuned after a couple of weeks
of data.

### New CLI: `brain-usage-report`

`pyproject.toml` script → `brain.usage_report:main`. Reflect runs exactly one
cheap command:

```sh
uv --directory ~/src/brain run brain-usage-report --days 30        # compact markdown (default)
uv --directory ~/src/brain run brain-usage-report --since 2026-06-03T00:00:00Z --json
```

Behavior: prune rows older than 90 days; open `local/usage.db` read-write
(prune) and ATTACH `cache/brain.db` **read-only** (`file:...?mode=ro`) for
the memory universe; emit four sections, output capped (~150 lines):

1. **never-recalled** — memories older than the window with zero recall hits
   and zero opens inside it:

   ```sql
   SELECT m.stable_id, m.mtype, m.path, substr(m.ts,1,10) AS updated
   FROM brain.memories m
   WHERE m.ts < :since
     AND NOT EXISTS (SELECT 1 FROM recall_hits h JOIN recalls r ON r.id = h.recall_id
                     WHERE h.kind='memory' AND h.stable_id = m.stable_id AND r.ts >= :since)
     AND NOT EXISTS (SELECT 1 FROM opens o
                     WHERE o.kind='memory' AND o.stable_id = m.stable_id AND o.ts >= :since)
   ORDER BY m.ts;
   ```

   (`m.stable_id` is the `mem_…` path-keyed id stable-ids adds. `m.ts` is the
   *authored* recency stable-ids' §Memory recency defines — frontmatter
   `date` → git-log date → mtime — so the `m.ts < :since` guard keys on when
   the memory's content was established, not disk mtime, and stops week-old
   memories being flagged by a 30-day window.)

2. **gap queries** — weak recalls, deduped on normalized query text, top 20:

   ```sql
   SELECT count(*) AS times, max(r.ts) AS last, r.source, r.query,
          max(r.top_vec_cos) AS best_cos, max(coalesce(r.top_score,0)) AS best_rrf
   FROM recalls r
   WHERE r.ts >= :since
     AND (r.n_hits = 0
          OR (r.top_vec_cos IS NOT NULL AND r.top_vec_cos < 0.55))
   GROUP BY lower(trim(r.query))
   ORDER BY times DESC, last DESC LIMIT 20;
   ```

   Hook-originated queries are included (that's where gaps surface); the
   `source` column lets reflect weigh them. Semantic clustering of similar
   gap queries is left to reflect — it's an LLM; that's the cheap part.

3. **most-opened** — promotion candidates, explicit opens broken out:

   ```sql
   SELECT kind, stable_id, count(*) AS opens,
          sum(source='mcp') AS explicit_opens, max(ts) AS last
   FROM opens WHERE ts >= :since
   GROUP BY kind, stable_id ORDER BY explicit_opens DESC, opens DESC LIMIT 10;
   ```

4. **calibration** — recall volume by source (incl. the fts_only share),
   weak-rate, and the `top_vec_cos` distribution (min/p25/p50/p75/max) for
   threshold tuning.

Markdown output (default) is compact tables under the four headers; `--json`
emits `{"since": ..., "never_recalled": [...], "gap_queries": [...],
"most_opened": [...], "calibration": {...}}`. Missing `usage.db` or missing
`brain.db` → print a one-line notice, exit 0 (reflect must not die on absent
telemetry).

### New step text for `skills/reflect/SKILL.md`

Insert as **step 4** after line 29 — the blank line after step 3 ("Pull the
time slice") which ends at :28; step 4 ("Distill.") begins at :30, so
inserting after 29 does not split it — and renumber current steps 4–8 to 5–9:

```markdown
4. **Review usage.** Run
   `uv --directory ~/src/brain run brain-usage-report --days 30`.
   One command, compact output; if it fails or reports no usage DB, note
   that in the final summary and continue — usage telemetry is optional,
   lossy signal. Treat its three lists as leads, not orders:
   - *never-recalled*: memories with zero recall hits and zero opens in the
     window → merge/demotion candidates for the Compact step. Delete only
     if the content is also judged dead — low traffic alone is not a death
     sentence for rarely-needed reference facts.
   - *gap queries*: recalls whose best hit was weak or empty → while
     distilling, check whether the reviewed sessions contain a durable fact
     that would have answered them; if so, that's a NEW memory candidate
     (cluster near-duplicate queries yourself before deciding).
   - *most-opened*: items whose full text was fetched most often (explicit
     opens count double) → promotion candidates: ensure each has a crisp,
     current memory; an episode opened repeatedly deserves distillation.
```

And under **Guardrails** add:

```markdown
- Never write to `local/usage.db`; read it only via `brain-usage-report`.
```

### Privacy / scope

All local. `recalls.query` stores verbatim query text, which — especially on
the hook path — contains fragments of raw user prompts. That is fine on this
machine (the transcripts themselves live in plaintext next door), but flag
for the future DigitalOcean port: `local/usage.db` must be treated with the
same sensitivity as transcripts — never synced or backed up unencrypted, and
the port should reconsider storing query text at all (truncate, hash, or
make it opt-in) if the DB leaves the laptop.

## Files touched (file:line anchors into current code)

| File | Anchor | Change |
|---|---|---|
| `.gitignore` | after line 5 (`*.db-shm`) | add `local/` |
| `src/brain/config.py` | after line 36 (`INGEST_STALE_SECONDS`) | add `LOCAL_DIR = BRAIN_DIR / "local"`, `USAGE_DB_PATH = LOCAL_DIR / "usage.db"`, `USAGE_RETENTION_DAYS = 90`, `USAGE_MAX_HITS_LOGGED = 8`, `USAGE_QUEUE_MAX = 1000`, `WEAK_VEC_COS = 0.55` |
| `src/brain/db.py` | `vec_search`, lines 176-185 | return `(rowid, distance)` tuples (L2 distance) |
| `src/brain/db.py` | `recall`, lines 257-264 (RRF fusion loop) | feed id-lists to `_rrf` as before (strip distances); track the **min** vec distance across kinds |
| `src/brain/db.py` | `recall`, line 298 (return) | add `"diag": {"top_score", "top_vec_cos", "n_hits"}` (convert min distance → `cos = 1 − d²/2`) |
| `src/brain/usagelog.py` | new file | queue, daemon writer thread, schema init, `log_recall`/`log_open`/`flush`, startup prune |
| `src/brain/server.py` | recall tool, lines 23-36 | after `db.recall(...)`: `diag = result.pop("diag", None)`; if `diag is not None`: `usagelog.log_recall(query=query, scope=scope, k=k, source="mcp", hits=result["hits"], diag=diag)`; return result |
| `src/brain/server.py` | get_episode tool, lines 38-49 | on success (`"error" not in out`): `usagelog.log_open(kind=out["kind"], stable_id=out["id"], source="mcp")` — `kind` comes from the result, since stable-ids removes the `kind` parameter (`get_episode(id: str)`) |
| `src/brain/usage_report.py` | new file | CLI: prune, ATTACH brain.db read-only, four report sections, `--days/--since/--json` |
| `pyproject.toml` | lines 11-13 (`[project.scripts]`) | add `brain-usage-report = "brain.usage_report:main"` |
| `skills/reflect/SKILL.md` | after line 29 (blank line; step 3 ends at :28, step 4 "Distill" begins at :30); guardrails lines 63-69; step 7 line 57 | insert step 4 text above; renumber current 4-8 → 5-9; add guardrail line; **also fix step 7's Session trailer `$CLAUDE_SESSION_ID` → `$CLAUDE_CODE_SESSION_ID`** (the old var never resolves — group provenance alignment) |
| `README.md` | lines 8-16 (rule section) | document the `local/` contract (non-rebuildable lossy telemetry, gitignored, never committed); mention `brain-usage-report` under Commands |

Not touched: `ingest.py`, `embed.py` (ingest never writes `usage.db`; no
contention by construction).

## Implementation steps (ordered, each independently verifiable)

0. **Precondition check** — stable-ids feature merged: `recall` hits and
   `get_episode` output expose stable ids. *Verify:* call `recall` twice
   around a `brain-ingest --full`; hit ids are unchanged.
1. **Plumbing** — `.gitignore` `local/` line; config constants. *Verify:*
   `uv run python -c "from brain import config; print(config.USAGE_DB_PATH)"`;
   `git check-ignore local/usage.db` succeeds.
2. **`usagelog.py`** — schema, queue, writer thread, prune, flush. *Verify:*
   `uv run python -c` script that logs 100 fake events + `flush()`, then
   `sqlite3 local/usage.db 'SELECT count(*) FROM recalls'` shows them;
   insert a row dated 100 days back, re-init, confirm it is pruned.
3. **`db.py` diag** — vec_search distances + recall diag block (min distance
   → cosine). *Verify:* REPL: `db.recall(conn, "some query")["diag"]` has
   `top_score`/`top_vec_cos`/`n_hits`; a clearly-good query yields
   `top_vec_cos` well above 0.55 and a nonsense query below it; `top_vec_cos`
   is NULL when scope hits an empty vec table.
4. **`server.py` instrumentation** — both handlers + import. *Verify:* run
   the server, drive one `recall` and one `get_episode` through a client
   (`claude` or a fastmcp test client); `sqlite3 local/usage.db` shows one
   `recalls` row (+hits, source='mcp') and one `opens` row; confirm the MCP
   `recall` response contains no `diag` key; time 50 recalls before/after
   the change — median latency delta within noise.
5. **`usage_report.py` + script entry** — *Verify:*
   `uv run brain-usage-report --days 30` prints the four sections;
   `--json | python -m json.tool` parses; with `local/usage.db` deleted it
   prints the one-line notice and exits 0.
6. **Docs + reflect step** — README `local/` contract; SKILL.md step 4 +
   renumber + guardrail. *Verify:* run `/reflect` interactively; it executes
   the report command and references its lists in the summary.
7. **(Deferred to hook spec)** wire `source="hook"` at the hook's entry
   point; if the hook transport is the MCP tool itself, add the optional
   `source: str = "mcp"` param then (see Risks).

Steps 1–3 are safe to land with no behavior change visible to MCP clients;
step 4 is the only change on the hot path.

## Risks & open questions

**Risks**

- **Threshold miscalibration.** `WEAK_VEC_COS = 0.55` is an informed guess
  from a small live sample, and the good/nonsense cosine bands (≈0.64–0.75
  vs ≈0.51–0.54) sit close together, so a bad threshold makes the gap list
  noisy or empty. Mitigated by the report's calibration block (the
  `top_vec_cos` distribution) — tune after ~2 weeks of real data. Gap-list
  quality is degraded, never wrong-in-a-dangerous-way.
- **Hook transport unknown (cross-spec).** If the auto-recall hook invokes
  the MCP `recall` tool, distinguishing hook from explicit calls needs an
  optional `source` param on the tool (a minimal, documented-as-internal
  growth of the tool surface). If the hook uses a CLI entry point instead,
  no MCP change is needed but the CLI must call `flush()` before exit and
  pass `source="hook"`. Interface here supports both; decision belongs to
  the hook spec.
- **Renamed/moved memory files orphan their usage history.** Memory stable
  ids are path-derived (stable-ids §Id contract), so moving or renaming a
  memory file mints a new `mem_…` id; its prior recall_hits/opens rows keep
  the old id and silently detach (a history gap, never corruption). Inherited
  from stable-ids; low-frequency and benign. (Names are *not* the key, so the
  earlier name-collision worry is moot.)
- **Signal dilution by the hook.** If the hook ever auto-opens items,
  `opens` stops meaning "a human/agent chose this". Guarded by `source` on
  every open + the report sorting on `explicit_opens` first.
- **Multi-process write contention.** One writer thread per `brain-server`
  process, several processes at once: WAL serializes single-row transactions
  in microseconds; a timeout drops the event (lossy contract). No risk to
  recall latency because writes are off the handler thread entirely.
- **Telemetry loss.** `local/` is not backed up and not in git; a disk wipe
  loses the history. Accepted by design; documented in README.

**Open questions**

1. Hook transport (above) — MCP-tool-with-`source`-param vs. separate CLI.
   Needs a joint decision with the auto-recall-hook spec.
2. Should the 30-day report window and the 90-day retention be user-tunable
   flags on day one, or constants until someone actually needs to change
   them? (Spec says: constants + `--days/--since` on the CLI only.)

## Size estimate (S/M/L and rough LOC)

**M.** Roughly 360 LOC touched/added:

- `usagelog.py` ~120 (schema, queue, writer thread, prune, flush)
- `usage_report.py` ~150 (4 queries, md + json rendering, arg parsing)
- `db.py` ~25 changed (vec_search tuples, diag tracking)
- `server.py` ~15, `config.py` ~10, `pyproject.toml` ~1, `.gitignore` ~1
- README + SKILL.md ~40 of prose

No new dependencies (stdlib `queue`/`threading`/`sqlite3` only). The risky
surface is small: only step 4 touches the recall hot path, and its failure
mode is "no telemetry", not "no recall".

## Adversarial review (verifier)

Verdict: **FLAWED** — the storage contract (`local/`), queue/writer design,
and schema are sound, but the "weak" definition — the heart of the gap-list
deliverable — is broken on *both* of its scoring branches, and two anchors
break against the dependency spec. Fixable, but not implementable as written.

1. **RESOLVED:** recall now converts the best L2 distance to cosine
   similarity `cos = 1 − d²/2` and stores it as `top_vec_cos`; "weak" is
   `top_vec_cos < WEAK_VEC_COS` (0.55), calibrated in cosine space against
   the measured good (≈0.64–0.75) vs nonsense (≈0.51–0.54) bands, with the
   distribution printed for tuning.

   **`top_vec_dist` is L2 distance, not cosine, and 0.65 flags essentially
   everything.** The vec0 tables are declared without `distance_metric`
   (db.py:76-83) and sqlite-vec v0.1.9 defaults to L2 — verified: orthogonal
   unit vectors return distance 1.4142, not 1.0. Vectors are L2-normalized
   (embed.py:50-57), so d = √(2−2·cos). Measured on the live index:
   clearly-good query→doc matches score d ≈ 0.71–0.85 (best memory hit for
   "nightly backup cron schedule" = 0.709), while a nonsense query scores
   0.96–0.99. `WEAK_VEC_DIST = 0.65`, documented as "nomic-embed cosine
   distance", therefore marks ~100% of recalls as gap candidates. Fix: store
   cosine explicitly (`1 − d²/2`) and recalibrate in cosine space (weak ≈
   cos < ~0.55, i.e. d > ~0.95), or keep L2 and fix name + threshold.
2. **RESOLVED:** the FTS-only weak branch and `WEAK_RRF_SCORE` are dropped —
   the constant 1/61 `top_score` can't discriminate. In `fts_only` mode only
   `n_hits = 0` is weak; `top_score` is still logged for information but never
   thresholded, and the calibration block reports the fts_only share.

   **The FTS-only weak threshold is mathematically degenerate.** In
   `fts_only` mode each kind fuses exactly one rank list, so the top fused
   hit is always rank 0 and `top_score` is *exactly* 1/61 ≈ 0.016393
   whenever `n_hits > 0` — it can take no other value (db.py:188-193,
   :257-264). `WEAK_RRF_SCORE = 0.0165` sits above 1/61, so every FTS-only
   recall with hits is "weak"; any threshold below 1/61 flags none. The
   signal carries zero information at either setting — the spec's own
   rationale ("below ~0.0165 the best hit wasn't even rank 0") is wrong,
   since rank 0 itself scores below 0.0165. Fix: in `fts_only` mode use
   `n_hits = 0` as the only weak condition, or log the top hit's raw bm25
   instead of an RRF score.
3. **RESOLVED:** the Files-touched get_episode row now reads
   `usagelog.log_open(kind=out["kind"], stable_id=out["id"], source="mcp")` —
   `kind` is read from the result, not the removed parameter.

   **`log_open(kind=kind, …)` references a parameter the dependency
   removes.** stable-ids changes the tool to `get_episode(id: str)` (kind
   inferred from the id prefix), so the Files-touched row for
   server.py:38-49 doesn't compile as written. Fix:
   `usagelog.log_open(kind=out["kind"], stable_id=out["id"], source="mcp")`.
4. **RESOLVED:** the header, the `recall_hits.stable_id` comment, and the
   never-recalled query note are restated against stable-ids' §Id contract
   (episodes `ep_`+16hex on `(sessionId, uuid)`; memories `mem_`+16hex on
   absolute path), and the moot name-collision Risk is swapped for the real
   inherited caveat — a renamed/moved memory mints a new id and orphans its
   usage history.

   **The dependency contract is mis-stated (twice).** Header: "episodes are
   hash-keyed … memories are name-keyed"; `recall_hits.stable_id` comment:
   "memory stable name"; Risks: "Name-keyed memory ids assume names are
   unique". stable-ids.md actually specifies opaque `ep_`/`mem_` + 16-hex
   ids: episodes keyed on `(sessionId, uuid)` (content-hash identity
   explicitly rejected there), memories keyed on absolute *path* (name
   explicitly rejected). The queries still work (the column really is
   `stable_id`), but the stated name-collision risk is moot while the real
   inherited caveat — a renamed/moved memory file mints a new id and orphans
   its usage history — goes unmentioned. Fix: restate against stable-ids'
   "Id contract" section and swap the risk.
5. **RESOLVED:** the step-text heading and Files-touched row now anchor the
   insertion after line 29 (the blank line; step 3 ends at :28, step 4
   "Distill" begins at :30), so it no longer splits the Distill step.

   **SKILL.md insertion anchor lands inside step 4.** "Insert after line 30
   (end of step 3)" — skills/reflect/SKILL.md:30 is the *first* line of
   step 4 ("**Distill.**"); step 3 ends at line 28 (line 29 is blank).
   Inserting after 30 splits the Distill step. Fix: insert after line 29.
