# Ambient auto-recall hook (`brain-hook`)

Status: spec, 2026-07-03. Feature: a Claude Code `UserPromptSubmit` hook that
runs every user prompt through brain recall and silently injects the top
**memory** hits as context — memory becomes ambient instead of interrogated.
**v1 scope (user decision): memories only** — distilled, high-precision facts.
Raw episodes stay behind the explicit `recall` MCP tool and land in the hook
in v2; the episode/summary machinery below is kept "v2-ready", not wired.

## Problem

Today the brain only surfaces when the model *decides* to call the `recall`
MCP tool. In practice that means memory is consulted rarely and late — the
user has to prompt in a way that nudges Claude toward the tool, or ask
outright. The goal is ambient recall: relevant past **memories** (v1) appear
alongside the prompt automatically, with zero user effort. Raw episodes are
deferred to v2 — deliberate episode search stays on the `recall` tool.

Hard constraints:

- The hook runs synchronously on **every** prompt; latency is the
  make-or-break constraint (~<1s hard ceiling, ideally <100ms).
- It must be **quiet**: no hits worth showing → emit nothing at all.
- It must **never** block, garble, or delay the prompt on any failure (DB
  missing, ingest writing, corrupt index, any exception).
- Design rules hold: markdown+git is truth, `cache/brain.db` is disposable;
  no API keys; uv venv only; MCP tool surface unchanged.

## Design (chosen approach, plus alternatives rejected and why)

### Verified hook contract (cite)

Verified 2026-07-03 against https://code.claude.com/docs/en/hooks and
https://code.claude.com/docs/en/hooks-guide:

- **stdin JSON** for `UserPromptSubmit`: `session_id`, `transcript_path`,
  `cwd`, `permission_mode`, `hook_event_name` (`"UserPromptSubmit"`),
  `prompt` (the user's text), and `prompt_id` (v2.1.196+).
- **Context injection**: on exit 0, plain stdout is added to Claude's context
  (`UserPromptSubmit` is one of the explicit exceptions where stdout reaches
  the context window). The structured alternative is exit 0 + JSON on stdout:
  `{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
  "additionalContext": "..."}}` — the text is wrapped in a system reminder
  and injected alongside the prompt.
- **Exit codes**: `0` = success, stdout parsed as JSON else injected as plain
  context. `2` = **blocks and erases the prompt** (stderr shown as feedback)
  — this hook must never exit 2. Any other code = non-blocking error; the
  prompt proceeds, first stderr line shows as a hook-error notice in the
  transcript — so we always exit 0, even on failure, and keep stderr empty
  unless a debug env var is set.
- **Timeout**: default 30s for `UserPromptSubmit` command hooks (lowered from
  the general 600s). Hooks for the same event run in parallel; duplicates
  dedup'd by command string.
- **Matchers**: `UserPromptSubmit` does not support matchers — it always
  fires on every prompt (a `matcher` field is silently ignored).

### Measured latency (this machine, warm caches, 2026-07-03; DB = 3.1 MB, 60 episodes / 9 memories)

| Path | Time |
|---|---|
| `uv run python -c pass` (venv startup) | 0.04 s |
| `import brain.db` + `db.connect()` (incl. schema DDL) | 38 ms |
| FTS5 search, both tables | 0.8 ms |
| fastembed import | 6 ms |
| fastembed ONNX **model load** | **650 ms** |
| one query embed (model warm) | 15–23 ms |
| **end-to-end process, full hybrid recall** | **0.71 s** |
| **end-to-end process, FTS-only recall** | **0.07 s** |
| **end-to-end simulated hook** (stdin JSON → tokenize → ro-connect → scored FTS on `memories_fts` → gate → JSON out) | **0.05 s** |
| quiet path (short prompt, exits before DB open) | 0.03 s |

These are warm-cache numbers. Cold start (first prompt after reboot) is
worse mainly for the hybrid path: the ONNX model is part of a 523 MB
`cache/fastembed/` tree that must be paged in, and a first-ever run would
*download* the model — unacceptable inside a hook.

### Chosen: `brain-hook` console script, FTS5-only, direct read-only SQLite

A new `brain-hook` entry point (`src/brain/hook.py`) that:

1. Reads the hook JSON from stdin; extracts `prompt` and `session_id`
   (`session_id` is used only to name the per-session dedup seen-file — there
   is no episode echo-filter in v1).
2. **Quiet gates before touching the DB**: exits empty if
   `BRAIN_HOOK_DISABLE=1`; if the prompt starts with `/` (slash command
   plumbing); or if fewer than 2 "content tokens" survive tokenization
   (`[A-Za-z0-9_]+`, length ≥ 3, minus a ~25-word stopword list, deduped,
   capped at 12 tokens — mirrors `db._fts_or_query`'s 32-token cap).
3. Opens `cache/brain.db` **read-only** via
   `sqlite3.connect("file:...?mode=ro", uri=True)` with
   `PRAGMA busy_timeout=200`. Missing/unopenable DB → exit 0, empty.
   Deliberately does **not** use `db.connect()` (db.py:107–119), which
   mkdirs, runs schema DDL (would create an empty DB), and sets a 5 s busy
   timeout — all wrong for a hook.
4. Queries **`memories_fts` only** with the OR-of-quoted-tokens expression,
   `ORDER BY rank LIMIT 8` (bm25). No episode query in v1, so the
   `is_sidechain = 0` / `session_id != <current>` echo filter is unnecessary
   (those are episode columns; memories have neither). **v2 (episodes) note:**
   that echo filter cannot be written against `episodes_fts` — it indexes only
   `text` (db.py:52-54) and exposes no `is_sidechain`/`session_id` column, so
   the episode leg must join the base table:
   `SELECT f.rowid FROM episodes_fts f JOIN episodes e ON e.id = f.rowid WHERE
   episodes_fts MATCH ? AND e.is_sidechain = 0 AND (e.session_id IS NULL OR
   e.session_id != ?) ORDER BY f.rank LIMIT 8` — NULL-safe session clause
   (plain `!=` silently drops NULL-session rows). Deferred with the rest of
   the episode path; still inside the 50 ms budget.
5. **Relevance gate (the PRIMARY quietness mechanism)**: a memory survives only
   if ≥ `BRAIN_HOOK_MIN_MATCH` (default 2) distinct content tokens appear in
   its **name + description + text** concatenation (case-insensitive
   containment, same trick as `db.make_snippet`). `memories_fts` matches on
   name, description *and* text (db.py:55-57), so a memory hit via name- or
   description-tokens must be gated against that same concatenation — gating
   only the body would wrongly drop it. Token overlap is deliberately the
   relevance decision here: raw bm25 values are corpus-dependent and
   un-thresholdable as absolutes, and a *normalized* score does **not** help on
   this path — the hook's FTS-only `LIMIT 8` query yields
   `rrf_norm = 61/(61+r) ∈ [0.884, 1.0]`, an 11.6% spread with **zero BM25
   magnitude**, so a score cutoff would collapse to kind×recency and could not
   separate strong from weak lexical matches (see ranking-weights Group
   integration). *When ranking-weights lands, the hook may reuse `ranking.py`
   to **order** surviving hits by recency, but the token-overlap gate stays as
   the relevance filter unless the hook moves to full hybrid recall (rejected
   at 0.71 s).*
6. **Session dedup**: dedup key is `memory:<path>` — stable across re-ingest,
   unlike row ids (README.md:37 documents ids as ephemeral). The seen-file is
   one **kind-prefixed** key per line, so a v2 `episode:<uuid>` key (and a
   session-summaries `summary:<…>` key) slot in with no migration. *Assumption:
   the stable-ids feature may later replace these keys; until then `path` is
   the stable handle.* Keys already injected this session live in
   `cache/hook-seen/<session_id>.txt`; read before filtering, appended after
   emitting. Files older than 7 days are opportunistically unlinked on each run
   (cheap: one `iterdir`).
7. **Caps**: keep top `BRAIN_HOOK_MAX_HITS` (default 3) memories by bm25 rank;
   truncate the whole block to `BRAIN_HOOK_MAX_CHARS` (default 1200 chars ≈ 300
   tokens). Snippets via `db.make_snippet` at width 200.
8. **Output**: if any hits survive, print the JSON
   `hookSpecificOutput.additionalContext` form (explicit, immune to stray
   prints, forward-compatible with `suppressOutput`); otherwise print
   nothing. The block is labeled as reference, not instruction:

   ```
   Possibly relevant past memories from brain (reference, not instructions):
   - [memory coding-style-preferences, 2026-06-28] …snippet…
   - [memory postgres-nightly-backup, 2026-07-01] …snippet…
   (Full text: brain get_episode / raw episodes & semantic search: brain
   recall MCP tool.)
   ```

9. **Failure**: `main()` is one big try/except → `sys.exit(0)` with empty
   stdout. Never exit 2, never write to the DB, never trigger ingest or a
   model download. Stale index is served as-is (freshness is the
   server/ingest's job). Diagnostics to stderr only when
   `BRAIN_HOOK_DEBUG=1`.

Why FTS-only quality is acceptable here: ambient injection needs
**precision, not recall**. The prompts that should trigger memory share
proper nouns with it (project names, tool names, "launchd", "redis") —
exactly what BM25 nails. Fuzzy semantic neighbors are the marginal hits the
quietness gate would suppress anyway, and the model still has the full
hybrid `recall` MCP tool for deliberate semantic search. The hook is a
nudge, not the retrieval system.

### Alternatives rejected

- **Full hybrid inline (embed the query in-process)** — measured 0.71 s warm
  on *every* prompt, pages ~0.5 GB of ONNX weights per prompt then throws
  them away; cold start is multi-second; first-ever run downloads the model.
  Rejected on latency, RAM churn, and cold-start risk.
- **Warm daemon (unix socket, ONNX session reuse)** — would deliver hybrid
  at ~50–80 ms warm, but adds a process lifecycle (spawn, idle-exit, stale
  socket, version skew after `uv sync`) to a project whose ethos is
  "rebuildable cache, minimal moving parts". **Deferred, not dead**: it is
  the documented upgrade path if FTS-only precision proves insufficient in
  practice; the hook's fallback shape (daemon down → FTS inline) is designed
  to slot in without changing the hook contract.
- **Precomputed/cached query embeddings** — prompts almost never repeat
  verbatim; near-zero hit rate. Rejected.
- **Plain-stdout injection** — works per docs, but any accidental print
  becomes injected context; the JSON form is explicit. Rejected in favor of
  `hookSpecificOutput.additionalContext`.
- **MCP round trip** — excluded by requirements; hooks can't call MCP tools
  of a running server anyway, and spawning the server per prompt is strictly
  worse than direct SQLite.
- **Transcript-scan dedup** (parse `transcript_path` for prior injections) —
  costs more per prompt than a seen-file and grows with session length.
  Rejected for the tiny per-session seen-file.

## API / schema changes

- **No MCP tool changes. No DB schema changes. No new dependencies.**
- New console script: `brain-hook = "brain.hook:main"` in
  `[project.scripts]`.
- New module `src/brain/hook.py`.
- New helper `db.connect_ro()` — read-only URI connect, 200 ms busy timeout,
  raises/returns `None` if the DB file is missing; never creates or migrates.
- `config.py` additions (env-overridable, following existing style):

  | Constant | Env var | Default |
  |---|---|---|
  | `HOOK_MAX_HITS` | `BRAIN_HOOK_MAX_HITS` | `3` |
  | `HOOK_MAX_CHARS` | `BRAIN_HOOK_MAX_CHARS` | `1200` |
  | `HOOK_MIN_MATCH` | `BRAIN_HOOK_MIN_MATCH` | `2` |
  | `HOOK_FTS_LIMIT` | `BRAIN_HOOK_FTS_LIMIT` | `8` |
  | `HOOK_SEEN_TTL_DAYS` | `BRAIN_HOOK_SEEN_TTL_DAYS` | `7` |
  | `HOOK_SEEN_DIR` | — | `CACHE_DIR / "hook-seen"` |
  | (kill switch, read in hook.py) | `BRAIN_HOOK_DISABLE` | unset |

- New cache artifact: `cache/hook-seen/<session_id>.txt` (already gitignored
  via `cache/`; rebuildable-by-deletion like everything else there).
- **User registration** (document in README; user-level
  `~/.claude/settings.json` so it applies in every project):

  ```json
  {
    "hooks": {
      "UserPromptSubmit": [
        {
          "hooks": [
            {
              "type": "command",
              "command": "uv run --directory $HOME/src/brain brain-hook",
              "timeout": 10
            }
          ]
        }
      ]
    }
  }
  ```

  Notes: no `matcher` (unsupported for this event); `timeout: 10` is
  belt-and-braces under the 30 s default; `uv run --directory` matches the
  existing MCP registration style (README.md:44) and self-heals after
  `uv sync`/lockfile changes at the cost of ~40 ms — pointing the command at
  `$HOME/src/brain/.venv/bin/brain-hook` directly shaves that but breaks
  silently if the venv is rebuilt; document both, default to `uv run`.
  Disable = remove the block, or set `BRAIN_HOOK_DISABLE=1` in `env`.

## Files touched (file:line anchors into current code)

- `/home/you/src/brain/pyproject.toml:12-14` — add `brain-hook`
  to `[project.scripts]` beside `brain-ingest`/`brain-server`.
- `/home/you/src/brain/src/brain/config.py:38-40` — append a
  `# Hook (ambient auto-recall)` constants block after the
  `# Recall / output` section.
- `/home/you/src/brain/src/brain/db.py:107-119` — add
  `connect_ro()` next to `connect()` (which the hook must NOT use — it
  creates the DB and waits 5 s on busy).
- `/home/you/src/brain/src/brain/db.py:141-147` — reuse
  `_fts_or_query` for the MATCH expression (hook passes its already-filtered
  token string).
- `/home/you/src/brain/src/brain/db.py:196-214` — reuse
  `make_snippet` for hit snippets.
- `/home/you/src/brain/src/brain/hook.py` — new module (~120–140
  LOC): stdin parse, gates, ro-query, threshold, dedup, format, output.
- `/home/you/src/brain/README.md:41-48` — add an
  "Ambient auto-recall hook" section (registration JSON, env knobs, disable
  instructions) after "MCP registration".

## Implementation steps (ordered, each independently verifiable)

**Build order (this group):** recall-filters → ranking-weights →
auto-recall-hook. This hook lands **last**, but on independent code paths
(`connect_ro` + the new `hook.py` module) and it never touches the scoring
block, so it is unaffected by the recall-filters/ranking-weights re-anchoring.
Once ranking-weights has landed, the hook may optionally reuse `ranking.py` to
order its surviving memory hits by recency (the token-overlap gate stays).

1. **config.py**: add hook constants. Verify:
   `uv run python -c "from brain.config import HOOK_MAX_HITS; print(HOOK_MAX_HITS)"` → `3`;
   `BRAIN_HOOK_MAX_HITS=5 uv run python -c ...` → `5`.
2. **db.py**: add `connect_ro()`. Verify: returns a working connection when
   `cache/brain.db` exists; with the DB renamed away it returns `None` (or
   raises the documented exception) **and creates no file**; a query during
   a running `brain-ingest --full` either succeeds (WAL readers don't block)
   or fails fast ≤200 ms.
3. **hook.py**: implement `main()` end to end. Verify without the console
   script: `echo '<sample hook JSON>' | uv run python -m brain.hook` →
   valid `hookSpecificOutput` JSON for a prompt with known-indexed nouns;
   empty output + exit 0 for `"yes do it"`, for a `/reflect` prompt, and
   with `BRAIN_HOOK_DISABLE=1`.
4. **pyproject.toml**: register `brain-hook`; `uv sync`. Verify:
   `echo '<sample JSON>' | uv run --directory ~/src/brain brain-hook`
   behaves identically to step 3, and
   `time ... brain-hook < sample.json` stays ≤ ~0.15 s.
5. **Dedup + quietness**: run the same sample JSON twice with one
   `session_id`. Verify: first run emits hits, second emits nothing;
   `cache/hook-seen/<session_id>.txt` contains the keys; a different
   `session_id` emits again; a seen-file back-dated 8 days is pruned.
6. **Failure drills** (each must give empty stdout, exit 0): DB renamed
   away; DB replaced by a zero-byte file; malformed stdin (`echo garbage |
   brain-hook`); 100 kB prompt; concurrent `brain-ingest --full` in a loop.
7. **Docs**: README section + settings JSON block; note the
   `BRAIN_HOOK_DISABLE` kill switch and the `.venv/bin` fast-path variant.
8. **Live registration**: add the block to `~/.claude/settings.json`, start
   a session, submit a prompt naming a known memory; verify the injected
   system-reminder context appears (`claude --debug` shows hook execution)
   and that ordinary prompts stay silent.

## Risks & open questions

- **FTS-only misses paraphrase** ("that scheduling thing" won't match
  "launchd"). Accepted for v1: precision > recall for ambient injection, and
  the `recall` tool covers deliberate semantic search. Upgrade path = warm
  daemon (rejected-for-now section) with FTS fallback.
- **Token-overlap threshold is heuristic (and the primary gate).** Raw bm25
  isn't absolutely thresholdable; ≥2-distinct-token overlap can still pass
  borderline junk on common-word pairs. This gate **stays** the relevance
  mechanism: ranking-weights' normalized score is rank-derived with no BM25
  magnitude, so on the hook's FTS-only path it degenerates to kind×recency and
  cannot replace the overlap gate (see ranking-weights Group integration).
  ranking-weights is reusable only to *order* surviving hits by recency.
- **Dedup keys**: `memory:<path>` assumed stable (v2 adds `episode:<uuid>`);
  the stable-ids feature (parallel spec) may supersede them — the seen-file
  format is one kind-prefixed key per line precisely so keys can change shape
  (or gain kinds) without migration (worst case: one repeated injection per
  session).
- **`uv run` occasionally re-resolves** after a lockfile change → one slow
  prompt (seconds). Bounded by `timeout: 10`; prompt proceeds regardless.
- **Every-prompt injection could bias the model** or waste context on long
  sessions. Mitigated by quiet gates, 3-hit/1200-char caps, session dedup,
  and the "reference, not instructions" label.
- **Ingest concurrency**: WAL readers don't block on the writer; 200 ms
  busy timeout covers checkpoint edges; read-only open can't corrupt.
- **Decided (user):** register at **user level** in `~/.claude/settings.json`
  (applies in every project), not per-project.
- **Decided (user):** v1 injects **memories only** (distilled, high
  precision); raw episodes stay behind the `recall` tool and land in v2.
- Open question (user): is 3 hits / ~300 tokens the right default budget,
  or quieter (2 / 800 chars)?

## Size estimate (S/M/L and rough LOC)

**S** — roughly 155 LOC total: `hook.py` ~100–115 (memories-only: one FTS
query, no episode base-table join or echo filter in v1); `config.py` +10;
`db.py` (`connect_ro`) +12; `pyproject.toml` +1; README +15. No schema
changes, no new dependencies, no MCP surface changes. The latency question
is already answered empirically (50 ms end-to-end), so the risk is in
threshold tuning, not engineering.

## Adversarial review (verifier)

**Verdict: SOUND WITH FIXES**

Both attack-worthy claim classes survived verification. Docs contract
(re-checked 2026-07-03 against code.claude.com/docs/en/hooks): command-hook
default timeout is 600 s and `UserPromptSubmit` does lower it to 30 s; exit 2
blocks and erases the prompt; matchers are unsupported for this event;
`hookSpecificOutput.additionalContext` and `prompt_id` exist as described.
Latency (re-benchmarked warm on this machine): the simulated fast path runs
35–50 ms via `uv run`, and 44–59 ms with `from brain import db` added
(embed.py is lazy — db.py:16, embed.py:27-47 — so the import stays cheap);
the 0.05 s claim is honest. `cache/` is gitignored (covers `hook-seen/`);
read-only WAL open is safe on this SQLite. Issues:

1. **RESOLVED:** v1 is memories-only, so the episode FTS query — and its
   is_sidechain/session_id echo filter — is removed from step 4 entirely. The
   verifier's base-table-join query is documented as the v2-ready pattern (with
   the NULL-safe session clause) for when episodes return.
   **Step 4's episode filter cannot be written against the FTS table.**
   `episodes_fts` indexes only `text` (db.py:52-54); `SELECT … FROM
   episodes_fts WHERE … AND is_sidechain = 0` fails with "no such column"
   (verified empirically). Fix: join the base table —
   `SELECT f.rowid FROM episodes_fts f JOIN episodes e ON e.id = f.rowid
   WHERE episodes_fts MATCH ? AND e.is_sidechain = 0 AND (e.session_id IS
   NULL OR e.session_id != ?) ORDER BY f.rank LIMIT 8` — still inside the
   50 ms budget; note the NULL-safe session_id clause (plain `!=` silently
   drops NULL-session rows).
2. **RESOLVED:** Step 5 now gates memories against the name + description +
   text concatenation (matching what `memories_fts` indexes), not just the
   body.
   **Overlap-gate scope (step 5).** `memories_fts` matches on
   name+description+text (db.py:55-57), so a memory matched via its name or
   description tokens would fail a gate that scans only "its stored text".
   Fix: gate memories against the name+description+text concatenation (the
   scoping benchmark script already did; the spec text should say so).
3. **RESOLVED:** Step 5 and the Risks note now keep token overlap as the
   primary relevance gate and explain why a normalized-score cutoff degenerates
   on the FTS-only path; `ranking.py` is reused only to *order* surviving hits
   by recency. The "gate becomes a score cutoff" framing is removed.
   **The "gate becomes a normalized-score cutoff" upgrade path (step 5
   assumption; Risks) is unsound for this hook.** ranking-weights' score is
   rank-derived RRF with no BM25 magnitude; on this hook's FTS-only LIMIT-8
   path the relevance component spans only 61/(61+r) ∈ [0.884, 1.0], so a
   score threshold reduces to kind×recency and cannot separate strong from
   weak lexical matches. Fix: reword — reuse `ranking.py` (kind weights +
   recency) to *order* hook hits, but keep token overlap as the relevance
   gate unless the hook moves to full hybrid recall (rejected at 0.71 s).
