# recall-cli — `brain-recall`: search + full-text fetch from Bash without MCP

Status: proposed, not implemented. No dependency on any unlanded spec — it
composes only already-shipped surface (`db.recall`, `db.get_item`,
`usagelog.log_recall`/`flush`, the `source='cli'` column the usage-logging
schema already declares).

Build-queue note: **this spec builds FIRST.** The resume-skill spec depends on
it (subagents will reach the brain over Bash through `brain-recall`, not MCP),
so land this before starting that one.

Related surface (reused verbatim, not redesigned here):

- **usage-logging**: the `recalls.source` CHECK already admits `'cli'`
  (`usagelog.py:54`), and `log_recall`/`flush` were written anticipating "a
  future `brain-recall` hook entry point" (`usagelog.py:262`). This spec is
  that caller; it adds no schema and no new `usagelog` API.
- **stable-ids**: search hits and `get_item` results already expose opaque
  `ep_`/`mem_`/`sum_` ids. `--get` takes exactly those strings; no id logic
  is duplicated in the CLI.

## Problem

Subagents and shell scripts cannot talk to the brain. Recall and full-text
fetch are reachable **only** through the MCP `recall`/`get_episode` tools
(`server.py:24-78`), i.e. only from a Claude Code session that has the `brain`
MCP server registered. A subagent with a Bash tool, a cron script, or any
non-MCP caller has no entry point at all.

Two forces make a CLI the right answer, not a workaround:

1. **MCP server processes go stale.** Each `brain-server` lives exactly as
   long as the Claude Code session that spawned it (`claude mcp add brain --
   uv run … brain-server`). Its code is pinned at spawn time; edits to
   `src/brain/*` after that session started are invisible to it until the next
   session. `uv run --directory ~/src/brain brain-recall …` resolves and runs
   **current source every invocation** — the same property that makes
   `brain-ingest`/`brain-sync` trustworthy from a headless job.
2. **Groundwork for a model-agnostic v2.** A stable, transport-free CLI surface
   over recall is the seam a future non-Claude harness plugs into without
   reimplementing ranking, filters, or id handling.

The constraint that shapes everything: **do not fork behavior.** A second
implementation of ranking, filtering, FTS-only degradation, or 8k truncation
would drift from the MCP path the moment either side changes. The CLI must be
a thin transport shim over the exact functions the MCP tools call.

## Design (chosen approach, plus alternatives rejected and why)

### Overview

One new module `src/brain/cli.py` (~90 LOC) and one console script
`brain-recall = "brain.cli:main"`. It has two modes over the same two
functions the MCP tools already delegate to:

| Mode | CLI | Reuses (unchanged) | MCP twin |
|---|---|---|---|
| search | `brain-recall "query" [flags]` | `db.recall(conn, query, k, scope, project, since, until)` | `server.py:recall` |
| fetch | `brain-recall --get <id>` | `db.get_item(conn, id)` | `server.py:get_episode` |

`cli.py` owns **only** argument parsing, output rendering, telemetry tagging,
and exit codes. Every behavior a caller could observe as "search quality" or
"fetch content" comes from the reused functions, so the CLI and the MCP tools
cannot diverge.

### 1. Search mode — exact reuse of `db.recall`

```
brain-recall "query" [--k N] [--scope all|episodes|memories|summaries]
                     [--project P] [--since D] [--until D] [--json]
```

The flags are a 1:1 pass-through to `db.recall`'s keyword params
(`db.py:507-510`): `--k → k` (default 6, matching the MCP tool's `k=6`),
`--scope → scope` (default `all`), `--project → project`, `--since → since`,
`--until → until`. Dates accept the same `YYYY-MM-DD` / `Nd` forms `db.recall`
already parses via `_parse_date_bound`; scope validation, filter resolution,
and FTS-only degradation all happen inside `db.recall` exactly as they do for
the MCP tool. The CLI never inspects the corpus itself.

**Connection.** `db.connect()` (`db.py:182`), then `db.recall(conn, …)`, then
`conn.close()` in a `finally` — the same three lines as `server.py:46-57`.
Guard first: if `config.DB_PATH` is absent, print
`brain-recall: no index yet ({DB_PATH}); run brain-ingest --full` to stderr and
exit non-zero **instead of** calling `db.connect()` — because `connect()`
materializes an empty DB (mkdir + schema DDL, `db.py:183-206`) and would
otherwise silently answer "0 hits" against a freshly-created empty index. A
missing index is an operator error a script should see, not a silent empty
result. (This mirrors the `connect_ro` philosophy the hook uses: never
materialize a DB as a side effect of a read.)

**Output — default (compact, human/agent-readable).** One line per hit, in the
score-descending order `db.recall` returns:

```
<id>  <kind>  <date>  <project>  score=<0.xxxx>  <snippet>
```

`id` is the persist-safe `ep_`/`mem_`/`sum_` string (or `sum_…` ephemeral);
`snippet` is `db.recall`'s already-built `make_snippet` output, printed on the
same line (single-line; a hit dict carries no newlines in `snippet`). A header
line reports `scope`, resolved `filters`/`filter_candidates` when present, and
`fts-only` when `result["fts_only"]` is true, so a degraded or filtered result
reads as such rather than as a thin corpus. Zero hits prints one
`(no hits)` line and still exits 0 — an empty result for a valid query is not
an error.

**Output — `--json`.** Emits the recall dict as `db.recall` returns it, minus
the internal `diag` block (popped for telemetry; see §3), i.e. **structural
parity with the MCP `recall` tool's JSON** (`server.py:49` pops `diag` for the
exact same reason; serializer whitespace/separators may differ). So a v2 harness gets identical structure from CLI and MCP:
`{"fts_only": bool, "hits": [...], ["filters": {...}, "filter_candidates": N]}`.

### 2. Fetch mode — exact reuse of `db.get_item`

```
brain-recall --get <ep_…|mem_…|sum_…>
```

Calls `db.get_item(conn, id)` — the identical function `server.py`'s
`get_episode` tool delegates to (`server.py:73`). That function already
selects the kind by id prefix, returns full text + metadata, and applies the
**8k `RETURN_TEXT_CAP` truncation with the `…[truncated]` marker**
(`db.py:740-742`). The CLI adds nothing to this path — reusing it is exactly
how the truncation behavior is "respected".

- On success (`"error" not in out`): default output prints a short metadata
  block (the `out` keys except `text` — e.g. `kind`, `id`, `session_id`/`name`,
  `date`/`ts`, `project`/`path`), a blank line, then the full (possibly
  truncated) `text`. `--json` emits `json.dumps(out)`.
- On failure (`out` has an `"error"` key — unknown prefix, no such row,
  invalid `sum_` id; `db.py:685/703/722/724/738`): print
  `brain-recall: {out['error']}` to **stderr** and exit non-zero. The error
  strings are `db.get_item`'s own, so they stay consistent with the MCP tool.

**Fetch does NOT log.** Unlike `server.py:74-75`, the CLI's `--get` path does
**not** call `usagelog.log_open`. Rationale: an "open" is defined as the
strongest *human/agent chose this* usefulness signal (usage-logging §Problem);
a script or subagent fetching by id over Bash is not that signal, and letting
automated fetches inflate the `opens` table would dilute reflect's promotion
ranking (the same dilution the `source` column exists to guard against). So
`--get` is deliberately telemetry-silent. (If a real need for CLI opens
appears later, the `opens.source` CHECK already admits `'cli'`; it's a one-line
change then, out of scope now.)

### 3. Telemetry — search logs as `source="cli"`, `--get` does not

Search mirrors `server.py:49-54` exactly, differing only in the `source` tag:

```python
result = db.recall(conn, query, k=k, scope=scope, project=project, since=since, until=until)
diag = result.pop("diag", None)          # internal-only; keep JSON == MCP surface
if diag is not None:
    usagelog.log_recall(query=query, scope=scope, k=k, source="cli",
                        hits=result["hits"], diag=diag)
```

`log_recall` is `put_nowait` + return; it never raises and never blocks recall
(`usagelog.py:211-241`). Because the CLI is a **short-lived process** and the
usage writer is a daemon thread that dies at process exit, the search path must
call `usagelog.flush(timeout=1.0)` before returning — this is the exact caller
`flush` was written for ("short-lived CLI callers … that would otherwise exit
before the daemon writes", `usagelog.py:260-263`). Flush is issued *after*
rendering stdout, so the ~ms DB write overlaps output and adds no perceptible
latency. `--get` logs nothing and calls no `flush`.

The empty/punctuation-query early return in `db.recall`
(`db.py:554-555`) returns with **no `diag` block at all** (`diag` is only
attached at the end of the full search path, `db.py:670`), so the
`if diag is not None` guard would skip logging it anyway; the CLI additionally
rejects such queries before connecting (see §4), so no empty-query telemetry is
written — matching usage-logging's rule that the empty-query early return is
not logged.

### 4. Arg parsing, exit codes, hostile inputs

`argparse`, `main(argv: list[str] | None = None) -> int`, `sys.exit(main())` —
the same shape as `usage_report.py:214` / `sync.py:379`. A positional `query`
(`nargs="?"`) and `--get ID` are **mutually exclusive and exactly one is
required**; neither or both is an argparse usage error.

| Input | Handling | Exit |
|---|---|---|
| valid search, ≥0 hits | render, log, flush | 0 |
| valid `--get`, row found | render full text | 0 |
| no operand / both operand+`--get` / unknown flag | argparse usage message on stderr | 2 |
| `--scope bogus` | `db.recall` raises `ValueError` (`db.py:524-527`) → catch, `brain-recall: {e}` on stderr | 1 |
| `--project ""` (empty) | `db.recall` raises `ValueError` (`db.py:531-532`) → same handler | 1 |
| empty / whitespace / punctuation-only query | CLI guards with `re.search(r"\w", query)` (mirrors `db.py:554`) → `brain-recall: empty query` on stderr, **before** connecting | 1 |
| `--get bogus` / unknown id / invalid `sum_` | `db.get_item` returns `{"error": …}` → `brain-recall: {error}` on stderr | 1 |
| missing `cache/brain.db` | stderr notice (see §1), no `connect()` | 1 |

So every bad invocation exits non-zero with one `brain-recall: <reason>` line
on stderr (arg errors use argparse's own exit-2 convention); only a valid query
that legitimately found nothing exits 0.

### Alternatives rejected

1. **A thin client that speaks MCP to a running `brain-server`.** Reintroduces
   the exact staleness problem this feature exists to kill (the server is
   pinned at spawn time) and needs a server to be running at all. `uv run`
   against source is simpler and always current.
2. **Duplicate the recall/fetch logic in `cli.py`.** Guarantees drift from the
   MCP path on the next ranking/filter/truncation change. The whole design is
   "reuse `db.recall`/`db.get_item`, add only transport."
3. **Log opens on `--get` (mirror `server.py` fully).** Pollutes the `opens`
   usefulness signal with automated fetches, degrading reflect's promotion
   ranking. Deliberately omitted (§2).
4. **Emit `diag` in `--json`.** Would make the CLI's JSON diverge from the MCP
   tool's (which pops it) and leak an internal-only block. Popped identically.
5. **Let `db.connect()` materialize a missing DB.** A silent empty-index "0
   hits" hides operator error from a script. Guard on `DB_PATH.exists()` and
   fail loud instead.

## CLI surface / API changes (exact)

- **New module** `src/brain/cli.py`:
  - `main(argv: list[str] | None = None) -> int` — parse, dispatch to one of
    the two helpers, return an exit code. No other public surface.
  - `_search(args) -> int` / `_get(args) -> int` — private; the two modes.
  - Renderers `_render_hits(result)` / `_render_item(out)` and their `--json`
    counterparts are private helpers.
- **New console script** in `pyproject.toml`: `brain-recall = "brain.cli:main"`.
- **No new module API elsewhere.** `db.recall`, `db.get_item`,
  `usagelog.log_recall`, `usagelog.flush` are reused as-is — **no signature
  changes to any existing function**, no new `config` constants, no schema
  change (search rows land in the existing `recalls`/`recall_hits` tables with
  `source='cli'`).
- **No new dependencies** (stdlib `argparse`/`json`/`re`/`sys` only).

## Files touched (file:line anchors into current code)

| File | Anchor | Change |
|---|---|---|
| `/home/you/src/brain/src/brain/cli.py` | new | the module above (~90 LOC): argparse, `_search`/`_get`, renderers, exit codes |
| `/home/you/src/brain/pyproject.toml` | `[project.scripts]`, after line 17 (`brain-usage-report`) | `+ brain-recall = "brain.cli:main"` |
| `/home/you/src/brain/README.md` | Commands fence :33-39 (after the `brain-usage-report` line :38) | add one line: `uv run brain-recall "query"    # search from Bash (subagents/scripts); --get <id> for full text` |
| `/home/you/src/brain/README.md` | new section after the auto-recall-hook section (before :112 "Memory file format") | short "## Recall from Bash (`brain-recall`)" section: two modes, the staleness/`uv run` rationale, `source='cli'` telemetry, `--get` does-not-log note, exit-code summary |

Not touched: `db.py`, `usagelog.py`, `server.py`, `config.py` (all reused
verbatim). No SQLite schema change. No `.gitignore` change (telemetry already
lands in the gitignored `local/usage.db`).

Tests: the house spec format (see `git-sync.md`, `usage-logging.md`) verifies
via the ordered, Bash-runnable smoke plan below rather than a committed pytest
module (`tests/` holds only `test_ranking.py`). A `tests/test_cli.py` is
**optional, not required** by this spec — out of scope unless the builder wants
belt-and-suspenders; the smoke plan is the acceptance gate.

## Implementation steps (ordered, each independently verifiable)

All verification runs headless via Bash against the **live** `cache/brain.db`.

1. **Write `src/brain/cli.py`** (argparse: positional `query` xor `--get`;
   `--k/--scope/--project/--since/--until/--json`; the two helpers; exit codes
   per §4). Register `brain-recall` in `pyproject.toml`; `uv sync`.
   *Verify:* `uv run --directory ~/src/brain brain-recall --help` prints both
   modes and exits 0.
2. **Search happy path.**
   *Verify:*
   `uv run --directory ~/src/brain brain-recall "nightly backup cron schedule" --k 3`
   prints ≤3 compact lines (id/kind/date/project/score/snippet), exit 0;
   `… --json | python -m json.tool` parses and has `hits`/`fts_only` and **no
   `diag` key**.
3. **Filters + scope pass-through.**
   *Verify:*
   `uv run --directory ~/src/brain brain-recall "postgres backup" --scope memories --project brain --since 30d`
   returns only memory-kind hits within the window; drop `--project`/`--since`
   and confirm the hit set widens (proves the flags reach `db.recall`).
4. **Fetch happy path + 8k truncation reuse.**
   *Verify:* take an `ep_…` id from step 2's output and run
   `uv run --directory ~/src/brain brain-recall --get ep_…`; it prints
   metadata + full text; for a known-long episode confirm the tail is
   `…[truncated]` and the body is ≤ ~8k chars (RETURN_TEXT_CAP reuse).
   `--get mem_… --json | python -m json.tool` parses.
5. **Telemetry: search logs `source='cli'`, `--get` does not.**
   *Verify:* run one search, then
   `sqlite3 ~/src/brain/local/usage.db "SELECT source, query FROM recalls ORDER BY id DESC LIMIT 1"`
   shows `cli|<your query>` (proves `flush()` landed the row before exit); run
   one `--get` and confirm `SELECT count(*) FROM opens` is unchanged.
6. **Hostile probes (must all exit non-zero with a stderr line, no traceback).**
   *Verify each — capture `$?`:*
   - bad scope: `brain-recall "x" --scope bogus` → stderr `brain-recall: scope must be one of …`, `$? != 0`.
   - unknown id: `brain-recall --get ep_deadbeef` → stderr `brain-recall: no episode with id ep_deadbeef`, `$? != 0`.
   - malformed id: `brain-recall --get sum_notanint` and `brain-recall --get zzz_1` → stderr, `$? != 0`.
   - empty query: `brain-recall ""` and `brain-recall "   "` → stderr `brain-recall: empty query`, `$? != 0`, and `SELECT count(*) FROM recalls` is **unchanged** (rejected before logging).
   - no operand: `brain-recall` → argparse usage on stderr, `$? == 2`.
   - both operands: `brain-recall "x" --get ep_1` → argparse error, `$? != 0`.
   - empty project: `brain-recall "x" --project ""` → stderr `brain-recall: …`, `$? != 0`.
7. **Staleness proof (the motivating property).**
   *Verify:* add a temporary `print("CLI-TAG", file=sys.stderr)` to `cli.py`,
   run `brain-recall "x"` — the tag appears immediately (no server restart),
   demonstrating `uv run` picks up current source; remove the line.

Steps 1–5 exercise the happy paths; step 6 is the acceptance gate for the
"exit non-zero with a clear message" contract; step 7 demonstrates the reason
the CLI exists.

## Out of scope

- **Writing** to the brain from Bash (`remember` has no CLI here). Search +
  fetch only.
- **Logging opens on `--get`** — deliberately omitted (§2); the `opens.source`
  CHECK already admits `'cli'` if a future need arises.
- **A `--source` override / hook wiring.** `brain-recall` always tags
  `source='cli'`. The auto-recall hook keeps its own `brain-hook` entry
  (`hook.py`) and `source='hook'`; this CLI does not subsume it.
- **New output formats** beyond compact-lines and `--json` (no CSV, no
  per-field templating).
- **Paging / server round-trips / a long-lived daemon.** One-shot process per
  invocation, by design (that is the staleness fix).
- **A committed pytest module** (see Files touched) — the Bash smoke plan is
  the acceptance gate.

## Risks & open questions

**Risks**

- **`uv run` cold-start latency.** `uv run --directory …` re-resolves the env
  (~40 ms, per the hook note in README:98-100) plus fastembed import for the
  query embedding. Acceptable for a subagent/script caller (not an
  every-keystroke hot path); a v2 that needs speed can point straight at
  `.venv/bin/brain-recall`. No mitigation needed now, flagged for the
  resume-skill consumer.
- **Telemetry drop on fast exit.** If `flush(timeout=1.0)` times out under a
  contended `local/usage.db`, the search row is silently lost — the same lossy
  contract usage-logging already accepts. Never affects the printed result or
  the exit code.
- **Missing embedder → FTS-only, silently.** `db.recall` degrades to FTS-only
  when the embedder/vec is unavailable (`db.py:557-568`); the CLI surfaces this
  via the `fts-only` header marker and the `fts_only` JSON field, so a degraded
  search is visible, not hidden.

**Open questions**

1. Should the default human output be one-line-per-hit (chosen — greppable,
   subagent-friendly) or a multi-line block per hit (richer, but harder to
   pipe)? Spec picks one-line + `--json` for structure; revisit only if the
   resume-skill wants blocks.
2. Should `--k` default to 6 (the MCP tool's default, chosen for parity) or a
   larger CLI default (subagents may want a wider net in one call)? Kept at 6;
   callers pass `--k` when they want more.

## Size estimate (S/M/L and rough LOC)

**S.** ~90 LOC new Python (`cli.py`: argparse + two ~15-line helpers + two
renderers), 1 line `pyproject.toml`, ~15 lines of README. No new module API,
no schema change, no new dependency. The verification effort (step 6's probe
matrix) slightly exceeds the code, which is the point — the code is a thin,
low-risk shim whose whole job is to not diverge from the reused functions.
