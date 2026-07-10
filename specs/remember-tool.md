# `remember` MCP tool — spec

Status: proposed (spec only, not implemented)
Date: 2026-07-03

## Problem

The brain can only gain memories two ways today: the nightly `/reflect`
consolidation pass, or the user hand-writing a file into `memories/`. When a
durable fact surfaces mid-session ("my new badge number is…", "never use
`--force` on this repo"), there is no way for the assistant to persist it
immediately — it has to survive until 03:30 in a transcript and hope reflect
distills it. That loses facts (reflect is a summarizer, not a stenographer),
and it means a fact learned at 9am is not recallable in the 2pm session.

`remember` is a third MCP tool that writes a properly formatted memory
markdown file into `memories/`, git-commits it with the repo's provenance
trailers, refuses near-duplicates, and makes the new memory recallable in the
same breath — all without violating the design rules: markdown+git stays the
source of truth, `cache/brain.db` stays a rebuildable index, no API keys, and
the MCP surface stays token-cheap.

## Design (chosen approach, plus alternatives rejected and why)

### Flow (chosen)

One synchronous tool call, five phases, guarded by **two** locks held across
phases 2–5: a module-level `threading.Lock` (FastMCP runs sync tools in a
threadpool; two concurrent in-process `remember` calls must not race on slug
allocation or git) **and an OS-level `fcntl.flock` on a lockfile**
(`<BRAIN_DIR>/.remember.lock`). The file lock is the one that matters for
correctness: multiple Claude sessions each run their own brain-server process
(see usage-logging.md), so the dedup→write window races *across* processes,
not just threads — a module lock alone would let the same fact remembered in
two sessions produce two files (the second slug-suffixed `-2`), defeating
dedup-first. Both locks are taken before phase 2 and released in a `finally`
after phase 5 (worst case they serialize a few sub-second writes; memories
arrive a few per day).

1. **Validate.** `type` must be one of `user|feedback|project|reference`
   (error dict otherwise). **`fact = fact.strip()` first** — the index stores
   the *stripped* body (`parse_memory` strips at ingest.py:211), so stripping
   here is what makes the dedup vector byte-identical to what re-ingest
   recomputes (review item 4). Then cap at `FACT_MAX_CHARS = 4000` chars —
   memories are distilled facts, not transcripts (reflect's own bar is ~20
   lines of body). Derive `description` = first sentence/line of the stripped
   `fact`, markdown-stripped, capped at 120 chars. Derive slug from `name`
   param if given (validated/re-slugified to `^[a-z0-9]+(-[a-z0-9]+)*$`), else
   from the first ~6 content words of the description, ≤48 chars.

2. **Dedup (before any write).** Embed the candidate once as a *document*:
   `embed.embed_documents([description + "\n" + fact])[0]` — with `fact`
   already stripped (phase 1), this is byte-for-byte the text shape the index
   stores for memories (see `ingest.py:373`), so the vector is reused later
   for the index insert with no drift. KNN over `memories_vec` only
   (`k=5`); sqlite-vec returns L2 distance over L2-normalized vectors, so
   `cos = 1 - d²/2`. Decision:
   - best `cos >= 0.90` (`DUP_COSINE`) → `status: "duplicate"`, return the
     existing memory, **do not write**.
   - best `cos >= 0.80` (`DUP_SOFT_COSINE`) → `status: "possible_duplicate"`,
     return the existing memory, do not write; caller may retry with
     `force=true`.
   - `force=true` skips both refusals (the calling model has context the
     server lacks; never hard-block it).
   - Cheap pre-checks either way: slug collision with an existing memory name
     → treat as `possible_duplicate`; exact normalized-text match → `duplicate`.
   - **Fallback when sqlite-vec/fastembed unavailable:** top-5
     `fts_search(conn, "memory", fact)` hits, Jaccard over lowercase word
     sets of `fact` vs `description + text`; ≥ 0.5 → `possible_duplicate`.

   Threshold grounding (measured against the live index, 256-dim nomic
   doc-vs-doc): near-exact paraphrase of an existing memory = **0.968**, terse
   restatement of the same fact = **0.926**, related-but-genuinely-new fact =
   **0.767**, unrelated fact = **0.550**; all pairwise cosines between the
   current distinct memories fall in **0.649–0.784**. So 0.90 catches
   restatements with headroom, and 0.80 flags the gray band without touching
   the observed distinct-pair ceiling (0.784).

   Dedup scope is the `memories` table only — which deliberately includes
   Claude auto-memory files under `~/.claude/projects/*/memory/` (they are
   ingested into the same table, `ingest.py:161-174`): if auto-memory already
   holds the fact, brain should return it, not duplicate it. Episodes are
   excluded: the source conversation always "contains" the fact and would
   false-positive every call.

3. **Write the file.** `memories/<slug>.md`, Claude auto-memory format per
   `README.md:50-54`: frontmatter `name` (kebab slug), `description`
   (double-quoted, inner quotes escaped — the parser's `_unquote` at
   `ingest.py:181-185` handles this), `metadata:`/`  type:`; body = `fact`
   verbatim. Created with `O_CREAT|O_EXCL` — on `FileExistsError` retry with
   `-2`, `-3` suffix. **Never overwrite an existing file** (git is the source
   of truth; clobbering loses facts).

4. **Index it (same call).** Call the ingest helper (`index_memory_file`,
   extracted from stable-ids' v2 upsert flow — see Files touched) on just
   this file: one transaction — upsert into `memories` keyed on
   `stable_id = db.memory_stable_id(path)` with its `content_hash` (FTS kept
   in sync by the AI/AU triggers), upsert the `files` row (`mtime`/`size`) so
   the next incremental ingest skips it. Then write the **already-computed**
   candidate vector into `memories_vec` and stamp `embedded_hash =
   content_hash` (no second embed — the dedup vector, over the stripped
   `fact`, is byte-identical to what ingest would compute). If embedding was
   unavailable, the row is still FTS-recallable and stable-ids' backfill (now
   keyed on `embedded_hash`) self-heals the vector on the next ingest.

5. **Commit.** Per-write, pathspec-limited:
   `git -C $BRAIN_DIR add memories/<slug>.md && git -C $BRAIN_DIR commit -m <msg> -- memories/<slug>.md`.
   Message: subject `remember: <slug>`, body = description, then trailers per
   `README.md:77-86` — `Session:` (only if the `session` param was passed),
   `Project:` (param, else the server's `$CLAUDE_PROJECT_DIR`, else omitted),
   `Host:` (always; `socket.gethostname()` — server and caller share the
   machine over stdio). Retry up to 3× with 300ms backoff on `index.lock`
   contention. On persistent git failure: **keep the file and the index rows**,
   return `committed: false` + the git stderr — the markdown on disk is the
   durable artifact, and the nightly reflect commit (`skills/reflect/SKILL.md:52-57`
   commits all of `memories/`) sweeps up strays. Never roll back the file.

Order rationale: dedup before write (requirement); index before commit so the
memory is recallable even if git wedges; commit last because it is the most
failure-prone step and its failure must not undo anything.

### Session/project provenance — what the server can actually know

Investigated on this machine (Claude Code 2.1.198) plus docs/issues:

- The **Bash tool environment inside a session** exposes
  `CLAUDE_CODE_SESSION_ID` (observed directly; it matched the live session's
  transcript id). So the calling model *can* learn its session id, but only by
  spending a Bash call first.
- The **MCP server process** *does* inherit `CLAUDE_CODE_SESSION_ID` in its
  environment (verified via `ps eww` on the running server) — but the value
  is **spawn-time-stale**: a stdio server is spawned once per Claude Code
  process and persists across `/clear`/session-id changes and across
  subagents, so the inherited id names *the session that spawned the server*,
  not necessarily the one calling the tool. Nor does a fresh identifier reach
  the server per-request (nothing in the MCP initialize handshake or request
  metadata — a documented gap, anthropics/claude-code#41836). So the var is
  present in the server env but **deliberately unused**: trusting it would
  attribute confidently and often wrongly. (This is the trap to resist — an
  implementer who discovers the var must not "fix" the omission by reading
  it.) The server env *does* get `CLAUDE_PROJECT_DIR` (the launch directory),
  which is per-process and a trustworthy default for the **Project** trailer.

**Decision:** `session` and `project` are optional string params; empty →
trailer omitted (Project falls back to the server's `$CLAUDE_PROJECT_DIR`
first). A trailer that is sometimes absent is better than one that is
sometimes wrong. The docstring tells the caller where the values come from.

### Alternatives rejected

- **RRF score as the dedup threshold.** RRF (`db.py:188-193`) is rank-based;
  its absolute value depends only on result positions, not similarity — a
  corpus of one memory gives the same top score as a perfect paraphrase.
  Raw cosine is the only signal here with an absolute scale. (`recall()` is
  still conceptually "run first", but the decisive signal is the vec distances
  that KNN already returns.)
- **Query-prefix embedding for dedup.** Nomic's `search_query:` prefix is for
  asymmetric query→doc retrieval; dedup is symmetric doc↔doc, so the candidate
  is embedded with `search_document:` — which is also what lets the vector be
  reused for the index insert.
- **Mark-index-stale instead of inserting.** Violates "recallable promptly":
  the server only re-ingests when >1h stale (`server.py:52-66`), so a 9am
  memory could stay invisible until 10am or the next server boot. The
  incremental insert is ~15 lines reusing existing code, and the expensive
  part (model load) was already paid by the dedup step.
- **Full `run_ingest()` call after writing.** Rescans every transcript
  (seconds, plus DB churn) to index one known file. Wasteful; also racy with
  the startup ingest thread for no benefit.
- **Batching commits (flush on N memories / on shutdown).** Memories arrive a
  few per day; batching saves nothing, leaves the source-of-truth repo dirty
  (uncommitted = un-provenance'd if the process dies), and — decisive — the
  Session/Project trailers are *per-memory*, so one batched commit would smear
  provenance across sessions, breaking the README convention.
- **SessionStart hook writing the session id to a file the server reads.**
  Breaks under concurrent sessions (this user runs two profiles plus
  subagents): the server cannot tell *which* live session invoked the tool,
  so it would attribute confidently and wrongly. Rejected in favor of honest
  omission. (Revisit if claude-code#41836 lands request-level metadata.)
- **Sniffing the newest transcript mtime to guess the session.** Same
  concurrency flaw, plus racy. Rejected.
- **`description` and date as extra params.** Cut for token cost: description
  derives from the fact's first sentence (which is also what a well-written
  fact leads with), and the format has no date field — `ts` comes from file
  mtime at ingest (`ingest.py:213-215`).

## API / schema changes

New MCP tool (the only schema change; no SQLite schema changes):

```python
@mcp.tool
def remember(
    fact: str,
    type: str = "project",
    name: str = "",
    session: str = "",
    project: str = "",
    force: bool = False,
) -> dict:
    """Persist a new memory: write memories/<slug>.md, commit, index — immediately recallable.

    fact: the durable fact (short markdown, first sentence becomes the description).
    type: user|feedback|project|reference. name: optional kebab-slug filename.
    session/project: provenance trailers ($CLAUDE_CODE_SESSION_ID from Bash env /
    workspace dir); omitted when empty. If a near-duplicate memory exists it is
    returned instead of writing; force=true writes anyway.
    """
```

Return payloads (compact dicts, same style as `recall`):

- created — `{"status": "created", "name": "<slug>", "path": "memories/<slug>.md", "id": "mem_…", "committed": true}`
  (on git failure: `"committed": false, "commit_error": "<one line>"`;
  if embedding was down: `"vec_pending": true`)
- refused — `{"status": "duplicate" | "possible_duplicate", "similarity": 0.94, "signal": "cosine" | "jaccard" | "name" | "exact", "existing": {"id": ..., "name": ..., "path": ..., "description": ...}, "hint": "pass force=true to write anyway"}`
- error — `{"error": "type must be one of user|feedback|project|reference"}` etc.

Stable-ids dependency (build order: this tool lands **after** stable-ids —
see that spec's Group integration): the return `"id"` is the `mem_…` stable
id from `db.memory_stable_id(path)`, **not** a rowid; the docstring pairs
`recall` → `get_episode(id)` (stable-ids removes the `kind` parameter — the
prefix types the id); and the index insert must supply the v2 NOT NULL
`stable_id` and `content_hash` columns via `db.memory_stable_id` /
`db.memory_content_hash`. Accordingly `index_memory_file` is extracted from
stable-ids' rewritten memory-**upsert** block, not the delete-by-path+insert
at ingest.py:346-363 (that flow is deleted by stable-ids). Because remember
commits immediately, its memories also get accurate authored recency for free
from the git-log-date fallback in stable-ids' §Memory recency — no `date`
frontmatter field is needed here.

New constants in `config.py`: `DUP_COSINE = 0.90`, `DUP_SOFT_COSINE = 0.80`,
`FACT_MAX_CHARS = 4000`.

Memory file written (format per `README.md:50-54`, parseable by
`parse_memory` at `ingest.py:188-223`):

```markdown
---
name: <slug>
description: "<derived one-liner>"
metadata:
  type: <type>
---

<fact verbatim>
```

## Files touched (file:line anchors into current code)

- **`src/brain/remember.py` (new, ~150 LOC)** — slug/description derivation,
  file formatting + `O_EXCL` write, dedup check, git commit with trailers and
  retry, `remember()` orchestration under the module `threading.Lock` +
  `fcntl.flock` lockfile (phases 2–5).
- **`src/brain/server.py`** — register the tool next to the existing two
  (`server.py:23-49`); thin wrapper opening a connection per call like
  `recall` does (`server.py:31-35`). Module docstring at `server.py:1` says
  "exactly two tools" — update.
- **`src/brain/ingest.py`** — extract the per-file memory ingest body into
  `index_memory_file(conn, path) -> str` (returns the `mem_…` stable id).
  **Re-anchor note:** stable-ids lands first and rewrites this block, so
  extract from its memory-**upsert** flow (which computes `stable_id`,
  `content_hash`, and authored `ts`), *not* from the delete-by-path+insert at
  ingest.py:346-363, which stable-ids deletes. The loop at ~ingest.py:331-363
  calls the helper; the embedding backfill (now keyed on `embedded_hash`, per
  stable-ids) is untouched and remains the self-heal path.
- **`src/brain/config.py`** — three constants appended to the recall/output
  block (`config.py:38-40`).
- **`src/brain/db.py`** — no changes; `remember.py` reuses `connect`
  (`db.py:107-119`), `serialize_f32` (`db.py:135-136`), `fts_search`
  (`db.py:150-165`), and reads distances via the same KNN shape as
  `vec_search` (`db.py:176-185`, but selecting `distance` too).
- **`src/brain/embed.py`** — no changes; uses `embed_documents`
  (`embed.py:60-64`) and `available()` (`embed.py:40-47`).
- **`README.md`** — tool list says two tools (`README.md:3-6`): add
  `remember`; one short usage paragraph; note under the trailer convention
  (`README.md:77-86`) that `remember` omits trailers it wasn't given.
- **Not touched:** `skills/reflect/SKILL.md` — reflect's compaction
  (`SKILL.md:44-47`) is what later merges/rewrites memories `remember`
  created; its pathspec-free commit of `memories/` also sweeps up any
  `committed: false` strays.

## Implementation steps (ordered, each independently verifiable)

1. **Constants.** Add `DUP_COSINE`, `DUP_SOFT_COSINE`, `FACT_MAX_CHARS` to
   `config.py`. Verify: `uv run python -c "from brain.config import DUP_COSINE"`.
2. **Ingest refactor.** Extract `index_memory_file(conn, path)` from
   `run_ingest`; loop calls it. Verify: `uv run brain-ingest --full` then
   `uv run brain-ingest` — totals identical to before the refactor, second
   run adds 0 rows.
3. **Formatting + write.** In `remember.py`: `slugify`, `derive_description`,
   `format_memory_md`, `write_memory_file` (O_EXCL, `-2` suffix on
   collision). Verify: round-trip — write to a tmp dir, `parse_memory()` it
   back, assert name/description/mtype/body match; write same slug twice,
   assert second file is `<slug>-2.md`.
4. **Dedup check.** `dedup_check(conn, description, fact) -> (status, best_hit,
   similarity, vec_or_none)` implementing cosine bands + name/exact pre-checks
   + FTS/Jaccard fallback. Verify against the live index:
   a paraphrase of `coding-style-preferences` → `duplicate` (cos ≈ 0.97);
   a genuinely new fact → `(None, ...)`; with `BRAIN_DIR` pointed at a
   vec-less copy → fallback path exercised.
5. **Git commit helper.** `commit_memory(path, slug, description, session,
   project)` — pathspec-limited add+commit, trailer assembly, 3×300ms retry
   on `index.lock`, returns `(ok, err)`. Verify in a scratch repo: commit
   contains only the memory file while another file sits staged (already
   validated manually during spec work); trailers present/absent per args;
   `touch .git/index.lock` → returns `(False, ...)` without raising, file
   intact.
6. **Orchestration.** `remember()` wiring the five phases under the module
   `threading.Lock` + `fcntl.flock` lockfile; register in `server.py`.
   Verify with an in-process FastMCP client (`fastmcp.Client(mcp)`) against a
   temp `BRAIN_DIR`: created → file exists, `git log -1` shows trailers,
   immediate `recall(fact)` returns the new memory as a top hit; repeat same
   fact → `duplicate`, no second file; `force=true` → written. Cross-process
   check: fire two separate `remember` processes on the same fact
   simultaneously → the file lock serializes them, so exactly one file is
   written and the loser returns `duplicate`.
7. **Docs.** Update `README.md` tool list + trailer note; fix `server.py:1`
   docstring. Verify: `git diff` reads clean; `claude mcp list`/tool listing
   shows three tools with the intended docstring.
8. **End-to-end.** From a real Claude Code session: ask it to remember a fact;
   confirm file, commit, and same-session recall; confirm next
   `brain-ingest` run reports `files_changed: 0` for the new file (files-table
   bookkeeping correct).

## Risks & open questions

- **Session trailer will usually be absent.** The model must spend a Bash
  call to learn `$CLAUDE_CODE_SESSION_ID` before it can pass `session`; in
  practice most `remember` commits will carry only Project + Host. Accepted:
  omission over misattribution. Revisit when claude-code#41836 lands.
- **`CLAUDE_CODE_SESSION_ID` is undocumented.** Observed in 2.1.198's Bash
  env but absent from the docs; it may be renamed/removed. Only the docstring
  hint depends on it, not the mechanism.
- **Threshold calibration is from a 4-memory corpus.** 0.90/0.80 has clean
  separation today (gap 0.784 → 0.926), but the gray band may need tuning as
  `memories/` grows and topics cluster. Constants live in `config.py` for
  exactly this reason.
- **Concurrent reflect.** A `remember` landing mid-reflect can be swept into
  reflect's commit (if reflect commits first) — provenance then lacks the
  Session trailer. Rare (03:30) and benign; the file content is identical.
- **First-call latency.** If the embedding model isn't warm (no prior recall
  or ingest in this server process), dedup pays the model load (~seconds).
  Acceptable for a write-path tool; degrade path (embedder down) already
  defined.
- **GPG-signing or exotic git config** in the repo would make commits hang or
  fail; handled by the 10s subprocess timeout → `committed: false`, but worth
  noting the file still lands safely.
- Open question (user): when dedup fires in the 0.80–0.90 band, is
  block-and-return-existing the right default, or should the tool return a
  proposed *merged* body for the caller to confirm? (Spec chose block +
  `force`; merging is left to reflect's compaction pass.)
- Open question (user): is per-memory Session provenance worth extra
  plumbing (e.g. a SessionStart hook writing session-id files) despite the
  concurrent-session ambiguity, or is opportunistic-param-else-omit enough?

## Size estimate (S/M/L and rough LOC)

**M (small M).** ~195 net LOC: `remember.py` ~150 (incl. the `fcntl` file
lock), `server.py` +20, `ingest.py` helper extracted from stable-ids' upsert
flow net ~+10, `config.py` +3, README ~+10. No schema migration of its own
(it rides stable-ids' v2 schema), no new dependencies (stdlib `fcntl`). Steps
1–5 are each mechanically verifiable in isolation; the risk concentrates in
step 6's ordering/lock logic.

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES**

Verified before the issues below: `CLAUDE_CODE_SESSION_ID` is present in the
Bash tool env and `CLAUDE_PROJECT_DIR` is present in the live brain-server
process env (checked via `ps eww` on the running server) — both env claims in
§Session/project provenance hold. The dedup cosine math is correct: vec0's
default metric is L2 (verified empirically, sqlite-vec v0.1.9) and embed.py
L2-normalizes after Matryoshka truncation (embed.py:50-57), so
`cos = 1 − d²/2` is valid. File:line anchors into ingest.py/db.py/server.py/
README all check out.

1. **RESOLVED:** §Session/project provenance now states the var *is* present
   in the server env but is spawn-time-stale (names the session that spawned
   the server, not necessarily the caller) and is therefore deliberately
   unused — with an explicit "don't 'fix' the omission" warning for future
   implementers.

   **"The MCP server process cannot know it" is factually wrong.** The
   running brain-server's env *does* contain `CLAUDE_CODE_SESSION_ID`,
   inherited at spawn — the spec asserts no session identifier reaches the
   server. The decision (omit rather than trust a spawn-time value that goes
   stale across `/clear` and is shared by concurrent subagents) still
   stands, but the claim invites a future implementer to "fix" the omission
   when they discover the var. Fix: state the var exists in the server env
   but is spawn-time-stale, hence deliberately unused.
2. **RESOLVED:** the flow now holds an OS-level `fcntl.flock` on
   `<BRAIN_DIR>/.remember.lock` across phases 2–5 (alongside the in-process
   `threading.Lock`), closing the cross-process dedup→write window; step 6
   adds a two-process concurrent-write verification.

   **The module-level `threading.Lock` gives false comfort — one lock per
   process, many processes.** The sibling usage-logging spec is premised on
   "multiple Claude Code sessions each run their own brain-server process";
   two sessions' `remember` calls race across processes. `O_EXCL` and the
   git retry survive it, but the dedup-check→write window does not: the same
   fact remembered concurrently in two sessions produces two files (the
   second slug-suffixed `-2`), defeating dedup-first. Fix: either document
   cross-process duplicates as accepted lossiness (reflect's compaction
   merges them) or take an OS-level lock (`fcntl` lockfile) around phases
   2–5.
3. **RESOLVED:** the §API "Stable-ids dependency" note and the ingest.py
   Files-touched entry are rewritten — return `"id"` is now the `mem_…`
   stable id, the docstring pairs `recall` → `get_episode(id)` (kind-less),
   the insert supplies v2 NOT NULL `stable_id`/`content_hash` via
   `db.memory_stable_id`/`memory_content_hash`, and `index_memory_file` is
   extracted from stable-ids' upsert flow (not the deleted ingest.py:346-363).
   Build order fixed as after stable-ids.

   **The stable-ids transition is understated.** "only the shared
   index-insert helper changes" — in fact: the return payload
   `"id": <rowid>` must become the `mem_…` stable id; the docstring's
   `get_episode(id, "memory")` pairing breaks (stable-ids removes `kind`);
   and the direct `INSERT INTO memories` fails under schema v2's NOT NULL
   `stable_id`/`content_hash` unless remember calls `db.memory_stable_id` /
   `memory_content_hash`. Fix: build after stable-ids and extract
   `index_memory_file` from the v2 upsert flow, not from ingest.py:346-363
   as anchored (that flow is deleted by the stable-ids spec).
4. **RESOLVED:** phase 1 now does `fact = fact.strip()` before both the embed
   and the write, so the dedup vector (over `description + "\n" + fact`)
   matches what re-ingest computes over the stripped body — no vector drift.

   **The "byte-identical vector" claim requires normalizing `fact` first.**
   `parse_memory` strips the body (ingest.py:211), so a `fact` with
   leading/trailing whitespace makes the dedup vector (over
   `description + "\n" + fact`) differ from what re-ingest computes over the
   parsed body — silent, permanent vector drift for that row until its next
   content change. Fix: `fact = fact.strip()` in phase 1, before both the
   embed and the write.
