# Per-session digests (kind="summary")

Session digests written by the nightly reflect pass, stored as git-tracked
markdown under `summaries/`, and indexed as first-class recall items alongside
episodes and memories.

Assumptions about parallel specs (not designed here):

- **ranking-weights**: `kind="summary"` will receive a rank boost that places
  summaries between memories and episodes at fusion time.
- **stable-ids**: ids may become stable later; this spec keeps today's
  ephemeral integer ids and the "pair recall → get_episode within one
  session" rule.
- **git-sync**: `summaries/` is git-tracked text and will sync across
  machines; raw transcripts never do — so a digest must stand on its own
  without its raw episodes.

## Problem

Recall over raw episodes works for detail queries ("what was that error
string") but is poor for session-level questions ("what did we decide about
the launchd job", "which files did the ingest refactor touch"). A session's
substance is smeared across many episode rows, each capped at 20k chars and
snippeted at 250 — no single row states decisions, outcomes, or the file
set touched. Memories deliberately don't fill this gap: they are curated,
compacted durable facts, not per-session records (SKILL.md step 4 explicitly
forbids storing "one-off task details").

A per-session digest fixes this, but it costs LLM time to produce, and the
only LLM budget is the nightly headless `claude -p "/reflect"` run (design
rule b). That makes digests **not freely rebuildable**, which by design rule
(a) disqualifies `cache/` — they must live as git-tracked text, with the
SQLite index remaining a derived, droppable cache of them.

## Design (chosen approach, plus alternatives rejected and why)

### 1. Where digests live

`<BRAIN_DIR>/summaries/YYYY-MM/<session_id>.md`

- `YYYY-MM` comes from the session's first episode timestamp (keeps directory
  listings bounded as history grows).
- Filename is the session UUID — this makes "does a digest exist for session
  X" a single `ls` glob, gives idempotency for free, and can never collide
  across machines under git-sync (UUIDs).
- Git-tracked: `.gitignore` already only ignores `cache/` and `*.db`, so no
  gitignore change is needed.

Frontmatter (parsed by the same YAML-ish frontmatter conventions as
`memories/*.md`; all values single-line):

```markdown
---
session_id: a1b2c3d4-5e6f-4a7b-8c9d-0e1f2a3b4c5d
date: 2026-07-02
project: src                # basename of cwd, matches _project_of() output
cwd: /home/you/src
host: devbox                # `hostname -s`; matters under git-sync
episodes: 20                # main-chain episode count at digest time
description: Built brain v0 ingest pipeline and MCP server skeleton  # ≤150 chars
---
```

Body = the digest. Target **15–30 lines, hard ceiling ~1,800 chars**. The
ceiling is not arbitrary: ingest embeds `description + "\n" + body` and
`EMBED_TEXT_CAP = 2_000` (config.py:31). **Both** fed fields are bounded — the
`description` frontmatter field is capped at **≤150 chars** and the body at
**≤1,800 chars** — so the embed input is at most `150 + 1 (newline) + 1,800 =
1,951 ≤ 2,000` chars. That ~49-char headroom is what makes the no-truncation
property actually hold: summaries are the one kind whose *entire* digest fits
in its embedding with no lossy truncation at embed time. (Only `description`
and the body enter the embed text; the other frontmatter fields — session_id,
date, project, cwd, host — never do, so they don't count against the cap.)
Body template:

```markdown
## What happened
1–3 sentences.

## Decisions
- bullet per decision, with the "why" in half a sentence

## Facts learned
- durable-ish facts surfaced (the truly durable ones also become memories)

## Files touched
- path — one clause on what changed

## Outcome
Done / blocked / follow-ups.
```

Same redaction bar as memories (SKILL.md step 4): never secrets or
credentials, no transcript excerpts longer than a sentence.

**Rejected: store digests in `memories/` with `metadata.type: summary`.**
Memories have a compaction lifecycle (~50-file cap, merge/prune — SKILL.md
step 5) that would either eat digests or be broken by them; digests are
append-only per-session records with different metadata (session_id, cwd).
Also `scope="memories"` would silently start returning session digests.

**Rejected: `cache/summaries/`.** Violates rule (a): cache is deletable, and
digests cost the nightly LLM budget to regenerate — deleting them is losing
data, not dropping an index.

**Rejected: one file per month/day containing many digests.** Breaks the
per-file ingest granularity (files table keys on path; one changed digest
would re-embed the whole month) and makes the "which sessions lack digests"
check a parse instead of a glob.

### 2. Which sessions merit a digest

Cheap heuristic evaluated entirely against the already-built index — no
transcript parsing, no LLM cost:

```sql
SELECT session_id, count(*) AS n, sum(length(text)) AS chars,
       substr(min(ts),1,7) AS ym, substr(min(ts),1,10) AS day, cwd
FROM episodes
WHERE is_sidechain = 0
GROUP BY session_id
HAVING (n >= 3 OR chars >= 15000)
   AND max(ts) < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 hour')
ORDER BY max(ts) DESC;
```

- `n >= 3`: at least three real (already junk-filtered) user prompts —
  one-or-two-prompt Q&A sessions are trivially covered by their raw episodes.
- `OR chars >= 15000`: catches the short-but-deep session (one prompt, a long
  agentic run producing lots of assistant text).
- `max(ts) < now - 1h`: never digest a possibly-live session (digests are
  written once; digesting a half-finished session would freeze a stale view).
  Note the `strftime` format: episode `ts` is ISO-8601 with a `T` separator,
  and SQLite's default `datetime('now')` uses a space, which breaks lexical
  comparison — the explicit format string is load-bearing.
- Sidechains (`is_sidechain = 1`) are never digested; they're support
  material for a main session.
- **Privacy denylist (config).** Before writing any digest, reflect consults
  `BRAIN_DIGEST_EXCLUDE` (a config constant parsed from the env var of the
  same name — comma-separated cwd/project substrings; see API section) and
  drops every candidate whose `cwd` (or `project` basename) contains an
  excluded substring. Excluded projects (e.g. personal-finance work) are **never**
  digested; those sessions stay recallable as raw local episodes only, and
  nothing about them is ever written to git-tracked `summaries/` — so nothing
  about them can sync under git-sync. Applied as a post-filter on the query
  result (not baked into the SQL) because the substrings come from the
  environment. This is what resolves both this spec's digest-privacy open
  question and git-sync's remote-creation privacy gate.

Against the current corpus (sessions with 20, 13, 6, 4 main-chain episodes)
all four qualify — reasonable, since all four were substantive build
sessions.

**Rejected: message-count from raw jsonl.** Requires re-reading transcripts;
the episodes table already is the noise-filtered signal.

### 3. Reflect integration

New step 4 inserted into `skills/reflect/SKILL.md` (current steps 4–8
renumber to 5–9). Exact text to insert after step 3:

> 4. **Write session digests.** Find digest-worthy sessions from the index
>    with exactly this query — the `strftime` format string is load-bearing
>    (episode `ts` is ISO-8601 with a `T` separator, so a bare
>    `datetime('now')`, which uses a space, would break the lexical `-1h`
>    comparison and let live sessions through). Do not improvise it:
>
>    ```sql
>    SELECT session_id, count(*) AS n, sum(length(text)) AS chars,
>           substr(min(ts),1,7) AS ym, substr(min(ts),1,10) AS day, cwd
>    FROM episodes
>    WHERE is_sidechain = 0
>    GROUP BY session_id
>    HAVING (n >= 3 OR chars >= 15000)
>       AND max(ts) < strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 hour')
>    ORDER BY max(ts) DESC;
>    ```
>
>    **Privacy denylist:** drop any candidate whose `cwd` contains a substring
>    listed in `BRAIN_DIGEST_EXCLUDE` (config env var; e.g. personal-finance
>    projects) — those are never digested and never written to `summaries/`.
>    For each remaining session, skip it if
>    `~/src/brain/summaries/*/<session_id>.md` already exists — unless its
>    current episode count (`n`) is at least double the `episodes:` value in
>    that file's frontmatter (a resumed session), in which case **rewrite the
>    digest in place at that exact glob-matched path** — never recompute the
>    `YYYY-MM` directory on a rewrite, or an old-month copy would survive and
>    double-index the session. Take at most **5** per night, newest first;
>    leftovers wait for the next night. For each selected session, read its
>    episodes from the index (`SELECT ts, substr(text,1,2500) FROM episodes
>    WHERE session_id = '<id>' AND is_sidechain = 0 ORDER BY ts`; pull more of
>    an episode only when the slice is clearly consequential) and write
>    `summaries/YYYY-MM/<session_id>.md` (new digests only — `YYYY-MM` from the
>    first episode's ts); frontmatter `session_id, date, project` (basename of
>    cwd), `cwd, host` (`hostname -s`), `episodes` (count), `description` (one
>    line, **≤150 chars**); body ≤ **~1,800 chars**: what happened, decisions,
>    facts learned, files touched, outcome. Same bar as memories: never
>    secrets/credentials, no transcript quotes longer than a sentence.

Two further SKILL.md edits:

- Step 7 (renumbered 8, "Re-index and commit"): commit `summaries/` alongside
  `memories/` + `proposals/`, and extend the commit subject to
  `reflect: YYYY-MM-DD — N added, N updated, N pruned, N digests`.
- Step 8 (renumbered 9, "Report"): add "digests written (by session id)" to
  the summary lines.

**Splice order with git-sync.** Both this spec and git-sync rewrite the same
commit step. This spec lands **first**: it inserts step 4, renumbers 4–8 → 5–9,
and is the edit that adds `summaries/` to the commit set and `, N digests` to
the subject. git-sync then builds on the *result* of this splice (its edits
target the already-renumbered steps) and adds the `brain-sync push` after the
commit. To avoid two builders reconciling conflicting instructions, the single
final merged text of the commit step (pull in step 1 → commit `memories/` +
`proposals/` + `summaries/` → push) is written once in git-sync's
"### Group integration" section; treat that as authoritative for the commit
step once both features land.

**How reflect finds sessions lacking digests:** the query above is
corpus-wide, not window-limited — existence of the digest file is the "done"
marker, so no extra bookkeeping state (no new field in `cache/last-reflect`).

**Bounding work:** cap of 5 digests/night, newest first. Each digest reads at
most a few dozen 2,500-char slices and writes ≤2 KB — well inside the
existing nightly budget next to distillation.

**Backfill stance:** no separate backfill mode. Because the query is
corpus-wide and the cap is per-night, existing history drains automatically
at ≤5/night, newest first (the most recallable sessions get digests first).
With today's 4 qualifying sessions, backfill completes on the first night.
If a large multi-machine history appears later, it self-paces; if that's too
slow the cap is one number in SKILL.md.

### 4. Ingest / schema: first-class `summaries` table

A third table trio (`summaries`, `summaries_fts`, `summaries_vec`) mirroring
the memories pattern exactly — triggers keep FTS in sync, vec rows maintained
at ingest. First-class kind is required by the feature statement and by the
ranking-weights assumption (fusion must be able to see `kind == "summary"`).

Because the whole SCHEMA runs as `CREATE ... IF NOT EXISTS` on every
`db.connect()` (db.py:115), existing `cache/brain.db` files gain the new
tables with **no migration step**; the next incremental ingest indexes any
`summaries/**/*.md` because the files table has no record of them.

**Rejected: reuse the memories table (extra columns + mtype="summary").**
Needs ALTERs on an existing cache (breaking "no migration" simplicity),
`scope="memories"` semantics get muddy, `_project_of()` and hit-shaping grow
special cases, and ranking-weights would have to peek at mtype instead of
kind.

### 5. Recall-time interplay: no dedupe — let ranking sort it out

A digest and its raw episodes will both match many queries. Chosen behavior:
**return both, no suppression or collapsing.**

- Suppressing raw episodes whose session also produced a digest hit is a
  recall-miss machine: the digest is a lossy ≤1,800-char summary, and a query
  matching a specific detail (an exact error string, a path) may match *only*
  the raw episode. For a memory system, silent misses are the worst failure
  mode; mild redundancy is cheap at k=6.
- Ranking already handles the common case: ranking-weights (parallel spec)
  boosts summaries above episodes, so for session-level queries the digest
  wins the top slots and the episodes trail it — which is exactly the right
  reading order (overview, then detail).
- The overlap is visible, not hidden: summary hits and episode hits both
  carry `session_id`, so the calling Claude can see "these three hits are one
  session" and act accordingly. Zero code.

**Rejected: collapse to best-hit-per-session across kinds.** Conflates "same
session" with "same information" — a 20-episode session spans multiple
topics, and its digest necessarily drops most of them. If redundancy ever
becomes real noise, a `max_per_session` cap inside `db.recall()` is a
10-line follow-up; deferred.

### 6. get_episode for summaries

`get_episode(id, kind="summary")` returns the digest row: `kind, id,
session_id, date, project, cwd, description, path, ts` + `text` (the body,
subject to the same `RETURN_TEXT_CAP` truncation — never hit in practice at
≤1,800 chars). `session_id` is the drill-down handle: on the originating
machine, `recall(query, scope="episodes")` hits from the same session give
the raw detail. On other machines (git-sync), raw episodes for that session
don't exist — which is why the digest body must be self-sufficient.

## API / schema changes

MCP tool surface: **no new tools** (rule c). `recall` gains one scope value;
`get_episode` gains one kind value.

- `recall(query, k, scope)` — scope becomes
  `"all" | "episodes" | "memories" | "summaries"`; `"all"` now includes
  summaries. Summary hits:
  `{"kind": "summary", "id", "date", "project", "snippet", "session_id"}`.
- `get_episode(id, kind)` — kind becomes `"episode" | "memory" | "summary"`.

New DDL (appended to `SCHEMA` / `VEC_SCHEMA` in db.py):

```sql
CREATE TABLE IF NOT EXISTS summaries(
    id          INTEGER PRIMARY KEY,
    session_id  TEXT,
    date        TEXT,
    project     TEXT,
    cwd         TEXT,
    host        TEXT,
    description TEXT,
    path        TEXT,
    ts          TEXT,
    text        TEXT
);
CREATE INDEX IF NOT EXISTS summaries_path ON summaries(path);
CREATE INDEX IF NOT EXISTS summaries_session ON summaries(session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
    description, text, content='summaries', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts(rowid, description, text)
    VALUES (new.id, new.description, new.text);
END;
CREATE TRIGGER IF NOT EXISTS summaries_ad AFTER DELETE ON summaries BEGIN
    INSERT INTO summaries_fts(summaries_fts, rowid, description, text)
    VALUES ('delete', old.id, old.description, old.text);
END;

-- in VEC_SCHEMA:
CREATE VIRTUAL TABLE IF NOT EXISTS summaries_vec USING vec0(
    embedding float[256]
);
```

Embed text for summaries (mirrors memories):
`coalesce(description,'') || char(10) || coalesce(text,'')`.

New config constants:

- `SUMMARIES_DIR = BRAIN_DIR / "summaries"`.
- `BRAIN_DIGEST_EXCLUDE` — a list of cwd/project substrings parsed from the
  `BRAIN_DIGEST_EXCLUDE` env var (comma-separated), e.g.
  `BRAIN_DIGEST_EXCLUDE = [s.strip() for s in os.environ.get("BRAIN_DIGEST_EXCLUDE", "").split(",") if s.strip()]`.
  Consulted by reflect (not ingest) before writing any digest: candidates
  whose cwd/project matches an excluded substring are never digested, so
  sensitive projects (e.g. personal-finance) never enter git-tracked `summaries/`.

## Files touched (file:line anchors into current code)

`src/brain/config.py`
- :18 — after `MEMORIES_DIR`, add `SUMMARIES_DIR = BRAIN_DIR / "summaries"`.
- :18 — also add the `BRAIN_DIGEST_EXCLUDE` privacy denylist (parse the
  `BRAIN_DIGEST_EXCLUDE` env var — comma-separated substrings — into a list;
  see API section). Reflect reads it to skip excluded projects/cwds; ingest
  ignores it.

`src/brain/db.py`
- :50 — after `memories_path` index, add `summaries` table + indexes (DDL
  above) inside `SCHEMA`.
- :55–57 — after `memories_fts`, add `summaries_fts`.
- :66–73 — after the memories triggers, add `summaries_ai`/`summaries_ad`.
- :76–83 — add `summaries_vec` to `VEC_SCHEMA`.
- :151 (`fts_search`), :169 (`_vec_nonempty`), :177 (`vec_search`) — replace
  the two-way ternary `"episodes_x" if kind == "episode" else "memories_x"`
  with a module-level dict, e.g.
  `_FTS_TABLE = {"episode": "episodes_fts", "memory": "memories_fts", "summary": "summaries_fts"}`
  (and `_VEC_TABLE` likewise).
- :217–227 (`_project_of`) — add a summary branch: `return row["project"] or "?"`
  (summaries carry project directly).
- :235–239 (`recall` scope map) — add `"summaries": ["summary"]` and extend
  `"all"` to `["episode", "memory", "summary"]`; update the ValueError text.
- :267–297 (hit-building loop) — add an `elif kind == "summary"` branch:
  fetch from `summaries`, snippet over
  `f"{row['description'] or ''} {row['text'] or ''}"`, emit the hit shape in
  "API / schema changes".
- :301–339 (`get_item`) — add a `kind == "summary"` branch returning the
  fields in Design §6; update the error message at :334 to
  `"kind must be 'episode', 'memory' or 'summary'"`.

`src/brain/ingest.py`
- :26–32 — import `SUMMARIES_DIR`.
- :174 — after `find_memory_files`, add
  `find_summary_files() -> list[Path]: return sorted(SUMMARIES_DIR.glob("*/*.md")) if SUMMARIES_DIR.is_dir() else []`.
- :188–223 — alongside `parse_memory`, add `parse_summary(path)` reusing
  `_FM_KEY`/`_unquote` (:177–186) to pull top-level keys `session_id, date,
  project, cwd, host, description` — **not** `episodes` (the `summaries` table
  has no such column, and no hit/get_item shape carries it; reflect reads
  `episodes:` straight from the file for its rewrite-when-doubled rule, so the
  parser never stores it). body = post-frontmatter text;
  `ts` = frontmatter `date` (fallback: file mtime, as :213–215 does).
  (Optional refactor: extract a shared `_parse_frontmatter(raw) ->
  (dict, body)` used by both parsers.)
- :256–261 — add `"summaries_added": 0` to the stats dict.
- :262–264 — `summary_files = find_summary_files()`; include in
  `current_paths`.
- :267–286 (prune loop) — add a summaries block mirroring the memories one
  (delete by `path`, `_delete_vec_rows(conn, "summaries_vec", old)`).
- :331–363 — after the memories loop, add an identical per-file loop for
  summaries (delete-by-path, insert, update `files`).
- :371–374 (embedding backfill tuple list) — add
  `("summaries_vec", "summaries", "coalesce(description,'') || char(10) || coalesce(text,'')")`.
- :399 (orphan-heal tuple list) — add `("summaries_vec", "summaries")`.
- :416–424 — add `summaries_total` and (when vec) `summaries_vec_total`.

`src/brain/server.py`
- :24–30 — `recall` docstring: scope line becomes
  `"all" | "episodes" | "memories" | "summaries"`; mention summary hits carry
  session_id.
- :38–45 — `get_episode` docstring: add
  `kind="summary" returns a per-session digest (reflect-written)`.

`skills/reflect/SKILL.md`
- Insert the new step 4 (exact text in Design §3) **after step 3** — i.e.
  after line 29 (the blank line following step 3, which ends at :28); renumber
  current steps 4–8 (lines 30–61) to 5–9. (The old ":28" anchor was wrong —
  inserting there splits step 3's body.)
- :52–57 (commit step, renumbered 8) — commit `summaries/` too; subject gains
  `, N digests`. NOTE: git-sync amends this same step to add `brain-sync push`;
  land session-summaries first, then apply git-sync's edit on the result (see
  Design §3 "Splice order with git-sync" and git-sync "### Group integration").
- :59–61 (report step, renumbered 9) — add digests written to the summary
  lines.

`README.md` (docs)
- :50–54 — after "Memory file format", add a short "Session digests
  (`summaries/YYYY-MM/<session_id>.md`)" section: written by reflect,
  git-tracked (not cache), frontmatter fields, indexed as `kind="summary"`.

New: `summaries/` directory (created by reflect on first digest; nothing
committed by this spec).

## Implementation steps (ordered, each independently verifiable)

1. **config.py**: add `SUMMARIES_DIR`.
   Verify: `uv run python -c "from brain.config import SUMMARIES_DIR; print(SUMMARIES_DIR)"`.
2. **db.py schema**: summaries table/indexes/FTS/triggers/vec.
   Verify: `BRAIN_DIR=$(mktemp -d) uv run python -c "from brain import db; c=db.connect(); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE name LIKE 'summaries%'\")])"`
   → lists `summaries`, `summaries_fts…`, `summaries_vec…`. Also connect
   against the existing `cache/brain.db` and confirm the tables appear
   (no-migration check).
3. **ingest.py**: `find_summary_files` + `parse_summary` + driver loop +
   prune + embed/orphan/stats entries.
   Verify: hand-write one fixture digest under a temp
   `BRAIN_DIR/summaries/2026-07/test.md`, run `run_ingest`, confirm
   `summaries_added: 1`, `summaries_vec_total: 1`; touch/edit the file and
   re-run → re-indexed once, no duplicates; delete it → pruned.
4. **db.py search**: table dicts, scope map, `_project_of`, hit branch,
   `get_item` branch.
   Verify: `db.recall(conn, "<word from fixture>", scope="summaries")`
   returns the fixture with `kind="summary"` and its `session_id`;
   `scope="all"` includes it; `db.get_item(conn, id, "summary")` returns
   frontmatter fields + body; bad scope/kind raise/return the updated
   messages.
5. **server.py docstrings**.
   Verify: `uv run brain-server` starts; a Claude session's tool listing
   shows the new scope/kind wording; `recall(scope="summaries")` works over
   MCP.
6. **SKILL.md edits** (new step 4, renumber, commit + report steps).
   Verify: read back; run the step-4 SQL against the live DB and confirm it
   lists the 4 current sessions with correct counts.
7. **End-to-end**: run `/reflect` interactively once.
   Verify: ≤5 digest files appear under `summaries/2026-07/` named by
   session id, each ≤~1,800-char body with valid frontmatter; re-running
   reflect writes zero new digests (idempotent); `brain-ingest` picks them
   up; `recall("brain ingest pipeline")` surfaces a summary hit above raw
   episodes once ranking-weights lands (before it: present in results).
8. **README.md** section.
   Verify: reads correctly; mentions git-tracked-not-cache rule.

## Risks & open questions

Risks:

- **Stale digests on resumed sessions.** A session that continues after
  being digested keeps its old digest. Mitigated: the ≥1h-quiet gate avoids
  live sessions, new episodes are still individually indexed, and the
  "rewrite when episode count doubles" rule catches big resumptions (costs
  one slot of the nightly cap). The doubling-rewrite overwrites the existing
  glob-matched file **in place** (never recomputing `YYYY-MM`), so a session
  can never end up with two digest files stranded across month directories —
  even after Claude Code prunes old transcripts and ingest drops those
  episodes, shifting `min(ts)`.
- **Digest quality drift.** Headless reflect writes these unreviewed; a bad
  night writes bad digests. Mitigated: git-tracked → reviewable/revertable,
  ≤1,800 chars bounds the blast radius, and raw episodes remain the ground
  truth in the index.
- **Cross-machine drill-down.** Under git-sync a digest's `session_id`
  points at transcripts that exist only on the originating host —
  `get_episode` on raw episodes fails elsewhere. Accepted: digests are
  written to be self-sufficient; `host:` frontmatter says where the raw
  history lives.
- **Timestamp format trap.** The heuristic compares ISO-`T` strings; using
  bare `datetime('now')` (space separator) silently breaks the `-1h` gate.
  The spec'd `strftime` format is load-bearing — worth a comment in
  SKILL.md.
- **Ranking before ranking-weights lands.** Until the boost exists,
  summaries compete with episodes on raw RRF and may rank below them. Benign
  and temporary.

Open questions:

1. **Nightly cap value.** Is 5 digests/night the right split of the single
   nightly LLM budget against distillation/compaction work? (Backlog drains
   slower at lower caps; a big backfill appetite would argue for a one-off
   higher-cap run.)
2. **Digest privacy under git-sync — RESOLVED (config denylist).** Digests
   summarize everything substantive and (under git-sync) push to every synced
   machine, so a blanket "same redaction bar as memories" is not enough for
   sensitive projects (e.g. personal-finance). Resolved by `BRAIN_DIGEST_EXCLUDE`
   (Design §2, API section): reflect drops any candidate whose cwd/project
   matches an excluded substring, so those sessions are never digested and
   never enter `summaries/` — they stay recallable as raw local episodes only.
   This also unblocks git-sync's remote-creation step (git-sync
   group-integration item 1).

## Size estimate (S/M/L and rough LOC)

**M.** ~220 LOC of Python (db.py ~85: DDL, dicts, recall/get_item branches;
ingest.py ~110: finder, parser, driver loop, prune/embed/stats; config.py 2:
`SUMMARIES_DIR` + `BRAIN_DIGEST_EXCLUDE`; server.py ~5 docstring lines) plus
~45 lines of markdown (SKILL.md ~30 — including the inlined candidacy SQL and
the denylist consult, README ~15). No migrations, no new dependencies, no new
MCP tools. The denylist is consulted by reflect (an LLM step), so it adds
config wiring but no recall-path Python.

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES**

Verified against the live system: the candidacy SQL runs as written (SQLite
accepts `n`/`chars` aliases in HAVING) and returns exactly the claimed
sessions (20/13/6/4 main-chain episodes; the 13-ep one currently excluded
only by the 1h-quiet gate); episode `ts` is ISO-`T`+`Z`, so the strftime trap
is real and correctly diagnosed; `EMBED_TEXT_CAP = 2_000` is at config.py:31
and applied in embed.py:63; `CREATE IF NOT EXISTS` on connect (db.py:115)
does deliver no-migration; `.gitignore` needs no change; the launchd
allowlist already permits `Bash(sqlite3:*)` (plist:11), so headless reflect
can run the query. Backfill math holds: only 6 top-level transcripts / 4
indexed sessions exist. db.py and ingest.py line anchors are accurate.

Issues:

1. **RESOLVED:** `description` is now capped at ≤150 chars (frontmatter block,
   body-math prose in Design §1, and the SKILL.md step-4 text); the size math
   is redone explicitly as 150 + 1 + 1,800 = 1,951 ≤ 2,000, so the
   no-truncation property provably holds.

   **"No lossy truncation at embed time" is unproven.** Embed input is
   `description || '\n' || text` capped at 2,000 chars (embed.py:63). A
   ≤1,800-char body leaves only ≤199 chars for `description`, which the spec
   never bounds ("one line" is not a length). Fix: cap `description` at ~150
   chars in the frontmatter spec and the SKILL.md step text.
2. **RESOLVED:** the exact candidacy query — including the load-bearing
   `strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 hour')` call — is now inlined as
   a fenced SQL block in the SKILL.md step-4 text (Design §3), with a "do not
   improvise it" note.

   **The load-bearing SQL is not in the SKILL step.** The inserted step-4
   text (Design §3) gives prose criteria and points at
   `specs/session-summaries.md`, while Risks (line ~418) admits the
   `strftime('%Y-%m-%dT%H:%M:%S', ...)` format is load-bearing — a headless
   reflect improvising `datetime('now')` silently breaks the 1h gate. Fix:
   inline the exact query (with the strftime call) in the SKILL.md step.
3. **RESOLVED:** `parse_summary` no longer extracts `episodes` (Files touched,
   ingest.py entry); reflect reads `episodes:` straight from the file for the
   rewrite-when-doubled rule, so nothing is parsed-and-dropped.

   **`episodes` frontmatter key has no destination.** `parse_summary` is
   spec'd to extract `episodes` (Files touched, ingest.py :188–223 entry),
   but the `summaries` DDL has no such column and no hit/get_item shape
   includes it; it is parsed and dropped. Fix: remove it from the parser
   (reflect reads it from the file directly for the rewrite rule).
4. **RESOLVED:** the rewrite rule (SKILL.md step 4 and the "Stale digests"
   risk) now overwrites the existing glob-matched path **in place** and
   explicitly never recomputes the `YYYY-MM` directory, so a resumed session
   can never leave two digest files across month dirs.

   **Rewrite can strand a second digest in another YYYY-MM dir.** Claude
   Code prunes old transcripts (cleanupPeriodDays); ingest then deletes those
   episodes (ingest.py:267–286), shifting the session's `min(ts)` — a
   resumed-session rewrite that recomputes YYYY-MM writes a NEW file while
   the old one survives in the old month dir, and both index as separate
   summary rows for one session. Fix: rewrite in place at the glob-matched
   path, never recompute the directory.
5. **RESOLVED:** the insertion anchor in Files touched now reads "after step 3
   — i.e. after line 29 (the blank line following step 3, which ends at :28)"
   and renumbers steps 4–8 at lines 30–61, replacing the wrong ":28"/":30–61"
   anchors.

   **SKILL.md insertion anchors are off by one-to-two lines.** Step 3 ends
   at SKILL.md:29 and current steps 4–8 span :31–61, not ":30–61"; inserting
   at ":28" splits step 3's text. Fix: insert after line 29.
