# Stable, content-derived ids + upsert re-ingest

## Problem

Episode and memory ids are ephemeral SQLite rowids. Re-ingest of a changed
file deletes all of that file's rows and re-inserts them (ingest.py:299-329,
:346-363), so ids shift on every change and the README has to warn "never
store ids" (README.md:37-39). Two costs:

1. **No durable references.** The planned usage-logging feature (which hit
   was recalled/opened, across days) and the `remember` tool both need ids
   that survive re-ingest. Today nothing can safely persist an id past a
   single session.
2. **Wasted embedding work.** Because delete+reinsert discards every row of
   a changed file, the vector backfill (ingest.py:365-392, keyed on "rows
   missing a vector") re-embeds *all* episodes in that file, not just new
   ones. Active session files grow every turn and the server re-ingests
   hourly (server.py:52-75), so a session accumulating N episodes gets
   embedded 1+2+…+N times ≈ O(N²) total embeddings instead of N.
   Measured on this machine: fastembed nomic-v1.5 runs ~1.4 docs/s at the
   2,000-char cap. The largest current session file has 20 episodes → ~14 s
   of re-embedding on every incremental ingest while it's active, vs ~0.7 s
   for just the one new episode. (Corpus today: 60 episodes, 9 memories,
   31 files — small now, but the O(N²) shape is the point.)

## Design (chosen approach, plus alternatives rejected and why)

### Id scheme

Ids are short, prefixed, content-derived hex strings — opaque to consumers:

| kind    | format          | key material (sha256, hex-truncated to 16 chars)              |
|---------|-----------------|----------------------------------------------------------------|
| episode | `ep_` + 16 hex  | `"brain-ep-v1\x00" + session_id + "\x00" + uuid`               |
| memory  | `mem_` + 16 hex | `"brain-mem-v1\x00" + str(absolute_path)`                      |

- **Episode key material.** `parse_transcript` already extracts, per episode,
  the `sessionId` and `uuid` of the *user message that opens the episode*
  (ingest.py:123-124). That pair is the stable transcript identity: verified
  against the full local corpus (31 files, 809 user lines) — `uuid` and
  `sessionId` are present on 100% of user lines and `(sessionId, uuid)` has
  zero duplicates, same-file or cross-file. Assistant continuations appended
  later change the episode *text* but not its identity — exactly the
  semantics we want (same id, re-embed).
  - Defensive fallback if `uuid` is ever missing:
    `"brain-ep-fallback-v1\x00" + (session_id or str(source_path)) + "\x00"
    + (ts or "") + "\x00" + user_text`. Never triggers on current data.
- **Memory key material.** Absolute file path. `path` is already the
  effective unique key (delete key at ingest.py:351, `files` table key).
  Frontmatter `name` is human-friendly but can collide across projects'
  `memory/` dirs, so it stays a display field in hits, not identity.
- **Prefix encodes kind.** `ep_`/`mem_` makes ids self-describing, which
  lets `get_episode` drop its `kind` parameter (smaller tool surface) and
  gives usage-logging a single opaque string per hit.
- **Version-tagged salt** (`brain-ep-v1`) so any future scheme change
  produces disjoint ids instead of silent collisions.

**Collision stance (accept-and-detect):** 16 hex = 64 bits. At a lifetime
corpus of 10^6 episodes, birthday-collision probability ≈ 3×10^-8 —
negligible. Be precise about the write path: the `UNIQUE` constraint does
**not** turn a collision into an error here, because every write is
`INSERT … ON CONFLICT(stable_id) DO UPDATE` — a genuine 64-bit collision is
silently absorbed as an update (two distinct episodes flapping over one
row), never raised. We accept this rather than bolt on hard-fail machinery
for a 3×10^-8 event; the remedy if it ever mattered is bumping the salt and
rebuilding. To make the near-impossible case *observable*, the upsert path
compares the existing row's `(session_id, uuid)` against the incoming pair
and logs a warning when they differ **under an unchanged `source_file`** —
the one signature that separates a true hash collision from an ordinary
content update or an ownership takeover. Cross-file duplicate
`(sessionId, uuid)` pairs (e.g. if a future Claude Code version copies lines
on session resume) are handled by upsert ownership-takeover, not treated as
collisions — see below.

### Upsert re-ingest

File-level change detection stays exactly as is (`files` table,
mtime+size, ingest.py:228-232). What changes is what happens *inside* a
changed file, replacing delete-all+reinsert with a three-way diff keyed on
`stable_id`:

1. Parse the file; compute for each row `stable_id` and a `content_hash`
   (episodes: sha256 over `\x00`-joined `ts, cwd, git_branch,
   is_sidechain, text` after the 20k cap; memories: `name, description,
   mtype, text` — deliberately excluding `ts`, which is file mtime, so a
   pure `touch` re-parses but leaves the row untouched). Parsed episodes
   are deduped into a dict keyed by stable_id (last wins).
2. **Deletes:** `existing_stable_ids(source_file) − parsed_stable_ids` →
   delete those base rows (AD trigger cleans FTS) and their vec rowids.
3. **Upserts:** one statement per row, verified against SQLite 3.53.1
   (venv) — returns a row only when it actually inserted or updated:

   ```sql
   INSERT INTO episodes(stable_id, content_hash, session_id, uuid, ts, cwd,
                        git_branch, is_sidechain, source_file, text)
   VALUES (?,?,?,?,?,?,?,?,?,?)
   ON CONFLICT(stable_id) DO UPDATE SET
       content_hash = excluded.content_hash,
       session_id   = excluded.session_id,
       uuid         = excluded.uuid,
       ts           = excluded.ts,
       cwd          = excluded.cwd,
       git_branch   = excluded.git_branch,
       is_sidechain = excluded.is_sidechain,
       source_file  = excluded.source_file,
       text         = excluded.text
   WHERE (episodes.content_hash <> excluded.content_hash
          OR episodes.source_file  <> excluded.source_file)
     AND (excluded.source_file = episodes.source_file
          OR excluded.ts >= episodes.ts)   -- takeover only moves forward in
                                            -- time; never regresses text to
                                            -- an older duplicate copy
   RETURNING id;
   ```

   - No row returned → row unchanged: no FTS churn, vec row kept, **no
     re-embedding**. This is the O(N²)→O(N) win.
   - Row returned (fresh insert or real update) → when vec is available,
     delete that rowid from the vec table (no-op for fresh inserts). A vec
     delete alone is **not** sufficient, though: a vec-less ingest
     (`has_vec` false → `_delete_vec_rows` no-ops, ingest.py:235-238) that
     updates a row *in place* leaves the old vector attached to the
     surviving rowid, and a "rows missing a vector" backfill would never
     re-embed it — a permanently stale vector served by recall. To close
     this honestly, each base table gains an `embedded_hash` column
     recording the `content_hash` the stored vector was built from; the
     backfill re-embeds every row where `embedded_hash IS NULL` **or**
     `embedded_hash <> content_hash` (via `INSERT OR REPLACE`, so a stale
     vector is overwritten in place), then stamps `embedded_hash =
     content_hash`. One query heals both the fresh/changed-row case and the
     vec-less-update case; orphan-healing is unchanged. (The earlier
     "backfill needs zero changes" claim was false for the vec-less path —
     review item 3.)
   - The `source_file` term in the WHERE gives ownership-takeover: if the
     same `(sessionId, uuid)` ever appears in two files, ownership moves to
     the file whose copy is *newer* (the `excluded.ts >= episodes.ts`
     tiebreak), so a later mtime bump of a *stale* duplicate can never
     regress the row's text to an older copy. The per-file delete diff
     (scoped to `WHERE source_file = ?`) can never delete a row another file
     has taken over.
4. FTS stays in sync via triggers, which requires adding the missing
   `AFTER UPDATE` triggers (external-content 'delete'+insert pattern,
   verified working) — today only AI/AD exist (db.py:59-73).

The vanished-file prune (ingest.py:267-286) keeps its current shape
(delete everything for that path); it just no longer needs the separate
memories delete-by-path since prune is already path-scoped.

### Memory recency (frontmatter `date`, not file mtime)

Today `parse_memory` sets a memory's `ts` from the file's mtime
(ingest.py:213-215), which is noise: a `git clone`, a `touch`, or an editor
rewrite all reset it, so "recency" tracks disk churn rather than when the
fact was established. Under this pass `ts` is resolved in order:

1. **Frontmatter `date`** (ISO-8601, if present and parseable) — the
   authored date. `parse_memory`'s frontmatter loop gains a top-level
   `date` key alongside `name`/`description`.
2. Else the file's **git-log date**:
   `git -C <dir> log -1 --format=%cI -- <path>` (last commit that touched
   it) — accurate for every committed memory, including everything the
   `remember` tool writes (it commits immediately) and everything reflect
   commits.
3. Else **file mtime** — the current behavior, retained as the last-resort
   fallback for uncommitted, un-dated files.

Change-detection interaction: `memory_content_hash` includes the raw
frontmatter `date` string (so *editing* the authored date updates the row's
recency), but **not** the git-log/mtime fallbacks — those are environmental,
and folding them into the hash would churn the row on every commit or touch
and defeat the incremental win. Documented consequence (not a hidden bug):
an un-dated memory's `ts` only refreshes when its *content* changes (it then
re-reads the git-log/mtime value), the same benign staleness already noted
for pure `touch`.

Ranking dependency: this `ts` is the "recency" that any recall-time recency
weighting and the usage-logging *never-recalled* report (`m.ts < :since`,
usage-logging.md) key on. Both consume authored recency, not disk mtime,
once this lands — which is why it lands **here in the stable-ids pass**,
ahead of the dependent specs.

### Rowid mapping (SQLite mechanics)

FTS5 external-content tables and vec0 virtual tables are rowid-addressed
and stay that way. The base tables keep `id INTEGER PRIMARY KEY` (the
rowid, alias preserved across UPDATE-in-place) as the internal join key
for FTS (`content_rowid='id'`), vec (`vec0 rowid = id`), and RRF fusion —
and gain a `stable_id TEXT NOT NULL UNIQUE` column as the *only*
externally visible id. Translation happens at the API edge: `recall`
returns `stable_id`, `get_episode` looks up `WHERE stable_id = ?`.
Internal search plumbing (fts_search, vec_search, _rrf) is untouched.

### Alternatives rejected

- **Make stable_id the primary key / use `rowid = hash` integers.** FTS5
  external-content and vec0 need integer rowids anyway; a 64-bit int id
  derived from the hash would work but leaks sign/format weirdness through
  JSON, loses the self-describing prefix, and saves nothing. Text UNIQUE
  column + integer rowid is the standard, boring mapping.
- **Human-readable memory ids (`mem_<name>`).** Collides across projects'
  memory dirs, needs sanitization rules, and breaks the "opaque id"
  contract. `name` is already returned in every hit for humans.
- **Contentless / re-created FTS tables instead of UPDATE triggers.**
  Contentless FTS5 forbids UPDATE and would force delete+reinsert again;
  rebuilding FTS per ingest defeats incrementality. External-content +
  three triggers is the documented pattern.
- **Hash of episode text as identity.** Text *grows* while a session is
  live (assistant continuations), so content-hash identity would churn ids
  for the most recent episode — the one usage-logging most wants to track.
  Identity and change-detection must be separate hashes (stable_id vs
  content_hash).
- **UUIDs/ULIDs assigned at first sight, persisted in a mapping table.**
  Violates "cache is rebuildable": a fresh `--full` rebuild would mint new
  ids. Content-derived ids reproduce identically after any rebuild.

## API / schema changes

### MCP tool surface (server.py)

- `recall(query, k=6, scope="all")` — unchanged signature; each hit's
  `"id"` becomes the stable string (`ep_…`/`mem_…`). All other hit fields
  unchanged.
- `get_episode(id: str)` — `id` is now the stable string; **`kind`
  parameter removed** (inferred from prefix; unknown prefix → `{"error":
  …}`). Output `"id"` is the stable id; other fields unchanged.
- Backward compat is a non-goal: integer ids were documented as
  never-store. README note (README.md:37-39) is replaced by the new
  contract (see Files touched).

### Id contract (what downstream features may assume)

- Ids are opaque strings matching `^(ep|mem)_[0-9a-f]{16}$`; the prefix
  identifies the kind.
- Stable across incremental re-ingests **and** full rebuilds on the same
  machine. Episode ids are machine-independent (derived from transcript
  content only); memory ids are path-derived, hence machine-local, and a
  renamed/moved memory file is a new id.
- Safe to persist indefinitely (usage log, remember tool). Consumers must
  tolerate `get_episode` returning an error for an id whose source was
  deleted or renamed.

### Schema (cache/brain.db — derived, rebuilt via `brain-ingest --full`)

New `SCHEMA_VERSION = 2` constant in db.py, recorded in `meta` under key
`schema_version`. Exact new statements (replacing db.py:29-50 tables and
adding to db.py:59-73 triggers; `files`, `meta`, FTS tables, and
VEC_SCHEMA are unchanged):

```sql
CREATE TABLE IF NOT EXISTS episodes(
    id           INTEGER PRIMARY KEY,
    stable_id    TEXT NOT NULL UNIQUE,   -- "ep_" + 16 hex, content-derived
    content_hash TEXT NOT NULL,          -- change detector, drives re-embedding
    embedded_hash TEXT,                  -- content_hash the stored vector was built from; NULL until embedded
    session_id   TEXT,
    uuid         TEXT,
    ts           TEXT,
    cwd          TEXT,
    git_branch   TEXT,
    is_sidechain INTEGER,
    source_file  TEXT,
    text         TEXT
);
CREATE INDEX IF NOT EXISTS episodes_source ON episodes(source_file);

CREATE TABLE IF NOT EXISTS memories(
    id           INTEGER PRIMARY KEY,
    stable_id    TEXT NOT NULL UNIQUE,   -- "mem_" + 16 hex, path-derived
    content_hash TEXT NOT NULL,
    embedded_hash TEXT,                  -- content_hash the stored vector was built from; NULL until embedded
    name         TEXT,
    description  TEXT,
    mtype        TEXT,
    path         TEXT,
    ts           TEXT,                    -- authored recency: frontmatter `date` → git-log date → mtime (see §Memory recency)
    text         TEXT
);
CREATE INDEX IF NOT EXISTS memories_path ON memories(path);

CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO episodes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, name, description, text)
    VALUES ('delete', old.id, old.name, old.description, old.text);
    INSERT INTO memories_fts(rowid, name, description, text)
    VALUES (new.id, new.name, new.description, new.text);
END;
```

New helpers in db.py (single home so ingest, usage-logging, and the
remember tool share one definition):

```python
def episode_stable_id(session_id, uuid, *, source_path="", ts="", user_text="") -> str
def memory_stable_id(path) -> str
def episode_content_hash(ts, cwd, git_branch, is_sidechain, text) -> str
def memory_content_hash(name, description, mtype, text, date="") -> str  # frontmatter date participates; git/mtime fallbacks do not
```

## Files touched (file:line anchors into current code)

- `src/brain/db.py`
  - :19-74 `SCHEMA` — add `stable_id`/`content_hash`/`embedded_hash` columns
    to episodes (:29-39) and memories (:41-49); add `episodes_au`/
    `memories_au` triggers after :73; add `SCHEMA_VERSION = 2` constant
    near :17.
  - :107-119 `connect()` — after `executescript(SCHEMA)`, write
    `schema_version = SCHEMA_VERSION` into `meta` **when that key is absent**,
    so a freshly created v2 DB is stamped v2 immediately (not only at ingest
    end) — closes the auto-rebuild race (review item 2).
  - near :135 (`serialize_f32`) — add the four id/hash helpers
    (`memory_content_hash` takes an extra `date` arg).
  - :267-297 `recall()` hit dicts — `"id": rid` → `"id": row["stable_id"]`
    at :275 and :289.
  - :301-339 `get_item()` — signature `(conn, item_id: str)`; dispatch on
    `ep_`/`mem_` prefix; lookup `WHERE stable_id = ?`; output `"id"` =
    stable id (:309, :325); drop the `kind` arg and its error branch :333.
- `src/brain/ingest.py`
  - :9-12 module docstring — describe upsert semantics.
  - ~:254 after `db.connect()` — schema-version check **under an exclusive
    lock file** (`fcntl.flock` on `cache/.ingest.lock`): re-read
    `meta.schema_version` while holding the lock; if it `!= SCHEMA_VERSION`,
    close, delete DB files (reuse the :247-252 unlink loop, extracted to a
    small helper), reconnect, log ("schema changed: full rebuild"). The lock
    plus the `connect()`-time stamp above stop a concurrent `run_ingest`
    (each brain-server runs one, server.py:52-74) from unlinking a v2 DB
    another process is mid-build on — review item 2.
  - :267-286 prune loop — unchanged behavior (path-scoped full delete).
  - :288-329 transcript per-file block — replace delete+reinsert
    (:299-323) with dedupe-by-stable_id, delete-diff, conditional upsert
    with `RETURNING id`, vec-row delete for returned ids. Stats become
    `episodes_added` / `episodes_updated` / `episodes_deleted`.
  - :188-223 `parse_memory` — resolve `ts` from frontmatter `date` →
    git-log date → mtime (see §Memory recency): add a top-level `date` key
    to the frontmatter loop and a `_git_log_date(path)` helper (subprocess
    `git -C <dir> log -1 --format=%cI -- <path>`, empty/non-repo → mtime).
  - :331-363 memory per-file block — same upsert pattern (one row per file;
    the delete-by-path at :351 disappears; deletion is prune's job).
    `memory_content_hash` is now called with the frontmatter `date`.
  - :365-392 embedding backfill — extend the "rows missing a vector" query
    to also match `embedded_hash IS NULL OR embedded_hash <> content_hash`,
    switch the vec write to `INSERT OR REPLACE`, and stamp
    `embedded_hash = content_hash` on the base row after each embed (fixes
    the vec-less stale-vector path, review item 3). :394-411 orphan
    healing — no changes.
  - :413-414 — also `set_meta(conn, "schema_version", str(db.SCHEMA_VERSION))`.
- `src/brain/server.py`
  - :38-49 `get_episode` — `def get_episode(id: str)`, drop `kind`,
    docstring: prefix-typed stable ids, safe to store.
  - :24-30 `recall` docstring — mention ids are stable `ep_`/`mem_` strings.
- `README.md`
  - :37-39 — replace the "ids are ephemeral / never store ids" note with
    the id contract: ids are stable content-derived strings
    (`ep_…`/`mem_…`), safe to store and reuse across sessions and
    rebuilds; memory ids change if the memory file is moved/renamed.
- `src/brain/config.py` — no changes.
- `specs/stable-ids.md` — this spec.

## Implementation steps (ordered, each independently verifiable)

1. **db.py: schema v2 + helpers.** Add `stable_id`/`content_hash`/
   `embedded_hash` columns, `_au` triggers, `SCHEMA_VERSION`, the
   `connect()`-time `schema_version` stamp, and the id/hash helpers.
   *Verify:* on a scratch path, `db.connect(tmp)`; `PRAGMA
   table_info(episodes)` shows `stable_id`/`content_hash`/`embedded_hash`;
   `SELECT value FROM meta WHERE key='schema_version'` is `'2'` right after
   connect; `SELECT name FROM sqlite_master WHERE type='trigger'` lists 6
   triggers; helper round-trip `episode_stable_id("s","u")` is deterministic
   and matches `^ep_[0-9a-f]{16}$`.
2. **ingest.py: schema-version auto-rebuild (locked).** Mismatch (or
   missing key) → drop and rebuild inside `run_ingest`, guarded by the
   `cache/.ingest.lock` flock and the re-read-under-lock.
   *Verify:* with the existing v1 `cache/brain.db` present, `uv run
   brain-ingest` logs the rebuild and produces v2 tables; running it again
   does not rebuild; launching two `brain-ingest` processes against a v1 DB
   leaves exactly one rebuild and a consistent DB (no unlink of an
   in-progress build).
3. **ingest.py: transcript upsert.**
   *Verify:* `uv run brain-ingest --full`; note some `stable_id`s; append
   a line to one session jsonl (or wait for a real turn); `uv run
   brain-ingest` → stats show `files_changed: 1`, `episodes_updated +
   episodes_added` ≪ file's episode count, `embedded` equals exactly that
   sum, and previously noted stable_ids are unchanged with unchanged
   rowids' vec rows intact.
4. **ingest.py: memory upsert + authored recency.** Parse frontmatter
   `date`; resolve `ts` = frontmatter `date` → git-log date → mtime; call
   `memory_content_hash` with the date.
   *Verify:* edit a memory body → 1 update, 1 embed; `touch` the file
   without edits → `files_changed: 1` but 0 updates, 0 embeds; a memory
   carrying a frontmatter `date:` shows that date as its `ts` (not mtime);
   editing that `date` → 1 update; a committed, un-dated memory shows its
   last-commit date, not mtime. Also confirm a vec-less run followed by a
   body edit then a normal run re-embeds the changed row (embedded_hash
   mismatch heals it).
5. **db.py: recall/get_item on stable ids.**
   *Verify:* `uv run python -c` — `recall` hits carry `ep_`/`mem_` ids;
   `get_item(conn, hit["id"])` round-trips for both kinds; bad prefix
   returns `{"error": …}`.
6. **server.py: tool signatures + docstrings.**
   *Verify:* drive the real MCP flow (e.g. via a registered `brain`
   server or FastMCP in-process client): `recall` → take an id →
   `get_episode(id)` returns the full text.
7. **README note.**
   *Verify:* README no longer says "never store ids"; states the contract.
8. **Determinism check (the whole point).**
   *Verify:* `brain-ingest --full`, dump `SELECT stable_id FROM episodes
   UNION ALL SELECT stable_id FROM memories` to a file; `--full` again;
   diff is empty.

## Risks & open questions

Risks:

- **`RETURNING` needs SQLite ≥ 3.35.** Venv ships 3.53.1 (verified);
  python.org/uv CPython bundles its own SQLite, so this is safe locally.
  A one-line startup assert on `sqlite3.sqlite_version_info` makes the
  failure mode explicit if the environment ever changes.
- **Old server process + rebuilt v2 DB (or new code + v1 DB) during
  rollout.** `recall` could briefly error until the background ingest's
  auto-rebuild completes. Transient by design; the server already
  tolerates ingest running concurrently (WAL + busy_timeout, db.py:112-113).
- **Ownership takeover (bounded, no text regression).** If a future Claude
  Code version duplicates `(sessionId, uuid)` across files (session resume),
  the `excluded.ts >= episodes.ts` tiebreak makes the *newer* copy win, so a
  flip re-embeds that one row but can never regress its text to an older
  duplicate — the failure the tiebreak was added for (review item 4).
  Bounded to one row per flip; measured zero occurrences in the current
  corpus. Not worth further machinery now.
- **Episode id depends on the opening user message's `uuid`.** If Claude
  Code ever rewrites uuids in place, ids churn — the system degrades to
  today's behavior (rows replaced), never corrupts; a full rebuild
  restores consistency.
- **An un-dated memory's `ts` only refreshes on a content change.** Under
  §Memory recency, `ts` = frontmatter `date` → git-log date → mtime, and
  only the frontmatter `date` is in the content hash. So a memory with no
  frontmatter `date` keeps its last-content-change recency across pure
  `touch`/commit — more truthful than mtime, and documented here so it
  isn't reported as a bug.

Open questions (need a user decision):

1. **Memory identity: path (chosen) vs frontmatter `name`.** Path is
   unique and matches the delete/files-table key, but a rename/move mints
   a new id and orphans usage-log history for that memory. Name survives
   renames but can collide across projects' memory dirs and can be edited
   in frontmatter. Recommendation: path.
2. **Drop `get_episode`'s `kind` parameter** in favor of prefix
   inference (chosen — smaller, cheaper tool surface), or keep it as an
   optional cross-check? The remember/usage-logging specs consume this
   signature, so it should be settled before implementation.
   Recommendation: drop it.

## Size estimate (S/M/L and rough LOC)

**M.** Roughly 210–260 LOC of diff: db.py ~80 (schema + embedded_hash,
triggers, helpers, connect() schema stamp, get_item/recall edge), ingest.py
~105 (locked rebuild check + two upsert blocks replacing delete+reinsert +
`embedded_hash` backfill + `parse_memory` authored-recency and
`_git_log_date` helper), server.py ~10, README ~10, plus the verification
commands above. No new dependencies (stdlib `fcntl`/`subprocess` only), no
data migration beyond one automatic full rebuild (~60 s of embedding at
today's corpus size on this machine).

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES**

Verified before the issues below: the `ON CONFLICT … DO UPDATE … WHERE …
RETURNING` semantics hold on the venv's SQLite 3.53.1 (fresh insert → row;
unchanged → no row; hash or source_file change → row; rowid preserved across
updates); the external-content FTS5 AU trigger pattern works as written; and
the identity claim re-verified on the current corpus — 37 files, 999 user
lines, 0 missing `sessionId`/`uuid`, 0 duplicate `(sessionId, uuid)` pairs,
0 resume-copied lines (sessionId ≠ filename stem).

1. **RESOLVED:** dropped the "loud error" claim; §Collision stance is now
   accept-and-detect — the upsert compares the existing row's
   `(session_id, uuid)` against the incoming pair and logs on mismatch under
   an unchanged `source_file` (the true-collision signature).

   **Collision stance contradicts the chosen write path.** The spec claims
   the `UNIQUE` constraint makes a real hash collision "a loud ingest error,
   never silent data corruption" (§Collision stance), but every write goes
   through `INSERT … ON CONFLICT(stable_id) DO UPDATE` — a genuine collision
   is silently absorbed as an update/ownership-takeover (two distinct
   episodes flapping over one row); no code path ever raises. Fix: drop the
   "loud error" claim; optionally compare the existing row's
   `(session_id, uuid)` in the upsert path and log when they differ.
2. **RESOLVED:** `connect()` now stamps `schema_version` on a freshly
   created v2 DB (not only at ingest end), and the mismatch check +
   delete-and-rebuild run under a `cache/.ingest.lock` flock with a
   re-read-under-lock — a concurrent `run_ingest` can no longer unlink a DB
   another process is mid-build on.

   **Auto-rebuild race: `schema_version` is only written at ingest
   completion.** The mismatch check goes at ingest.py:254 but the key is set
   at ingest.py:413-414 — a v2 DB mid-first-build has no `schema_version`,
   so any concurrent `run_ingest` (every brain-server process runs
   `_ensure_fresh_index`, server.py:52-74, and the usage-logging spec is
   premised on one server process per Claude session) sees "mismatch" and
   unlinks the DB files another process is actively writing. Fix: set
   `schema_version` in `db.connect()` right after `executescript(SCHEMA)`
   (when absent), and guard delete-and-rebuild with an exclusive lock file.
3. **RESOLVED:** added an `embedded_hash` column to both base tables; the
   backfill now re-embeds any row where `embedded_hash IS NULL OR
   embedded_hash <> content_hash` (via `INSERT OR REPLACE`) and stamps
   `embedded_hash = content_hash`, healing the vec-less-update path. The
   false "zero changes" claim is removed.

   **Vec-staleness regression in the vec-less update path.** Today a
   vec-less ingest (`has_vec` false → `_delete_vec_rows` no-ops,
   ingest.py:235-238) leaves orphans that healing purges because
   delete+reinsert changes rowids (ingest.py:394-411). Under upsert the
   rowid survives UPDATE: a text change during a vec-less run leaves the old
   vector attached to a live rowid — backfill (`id NOT IN (SELECT rowid …)`,
   ingest.py:375-378) never re-embeds it and orphan healing never removes
   it, so recall serves a permanently stale embedding. "The backfill and
   orphan-healing passes need **zero changes**" is false for this path.
   Fix: record the content_hash each vector was computed from (e.g. an
   `embedded_hash` column) and extend backfill to re-embed on mismatch.
4. **RESOLVED:** added an `excluded.ts >= episodes.ts` tiebreak to the
   upsert WHERE so a takeover only moves forward in time (newer copy wins),
   never regressing text to an older duplicate; the Risks entry now prices
   text, not just embeds.

   **Ownership takeover is mispriced: it can regress text, not just
   re-embed.** Takeover is last-ingest-wins (the `source_file` WHERE term);
   if duplicate `(sessionId, uuid)` ever exists in two files, any mtime bump
   of the *stale* file overwrites the row's text with the older copy until
   the fresh file next changes. The Risks entry ("Ownership takeover
   re-embeds") prices this as embedding cost only. Fix: document a tiebreak
   (e.g. longer text / newer episode `ts` wins) or explicitly accept
   stale-text flips in the risk entry.
5. **RESOLVED:** Files-touched anchor corrected to db.py:275 (memory anchor
   :289 unchanged).

   Minor anchor: recall's episode `"id": rid` is at db.py:275, not :276
   (the memory anchor :289 is correct). Fix: :275.

### Group integration

1. **RESOLVED:** §Id contract here is authoritative and unchanged
   (episodes → `ep_`+16hex keyed on `(sessionId, uuid)`; memories →
   `mem_`+16hex keyed on absolute path). Both dependents' dependency
   sections are rewritten to quote it verbatim — remember-tool.md
   §Stable-ids dependency (payload `mem_…`, `get_episode(id)` kind-less,
   v2 NOT NULL columns) and usage-logging.md header + `recall_hits.stable_id`
   comment + Risks (path-keyed, rename-orphans-history).

   **Both dependent specs quote a contract this spec doesn't provide.**
   usage-logging.md's header assumes "episodes are hash-keyed … memories are
   name-keyed" and its `recall_hits.stable_id` comment says "memory stable
   name" — this spec keys episodes on `(sessionId, uuid)` (content-hash
   identity explicitly *rejected* in §Alternatives) and memories on absolute
   path (name explicitly rejected). remember-tool.md claims "when stable
   content-derived ids land, only the shared index-insert helper changes" —
   in fact its return payload (`"id": <rowid>` → `mem_…`), its
   `get_episode(id, "memory")` pairing (this spec removes `kind`), and its
   direct `INSERT INTO memories` (v2 adds NOT NULL `stable_id`/
   `content_hash`) all change too. Both specs must restate their dependency
   sections against §Id contract here before implementation.
2. **RESOLVED:** landing order fixed stable-ids → remember → usage-logging;
   remember-tool.md and usage-logging.md each carry an explicit re-anchor
   note that their ingest.py:346-363 edits must rebase onto this spec's
   rewritten memory-upsert block (remember's `index_memory_file` is extracted
   from the v2 upsert flow, not the deleted delete-by-path+insert).

   **ingest.py churn collision with remember-tool.** remember extracts
   `index_memory_file` from the delete-by-path+insert flow
   (ingest.py:346-363) that this spec deletes ("the delete-by-path at :351
   disappears"); neither spec's Files-touched accounts for the other.
   No behavioral conflict — under upsert, remember's insert converges on the
   same stable_id and its files-row bookkeeping still makes the next ingest
   skip the file, so "recallable promptly" survives — but whichever lands
   second must re-anchor its ingest.py changes.
3. **RESOLVED (in usage-logging.md):** the Files-touched get_episode row now
   reads `usagelog.log_open(kind=out["kind"], stable_id=out["id"],
   source="mcp")` — no reference to the removed `kind` parameter.

   **usage-logging's `log_open(kind=kind, …)` breaks against this spec**
   (get_episode loses the `kind` parameter); it must use `out["kind"]` —
   detailed in the review appended to usage-logging.md.
4. **RESOLVED:** all three specs use `CLAUDE_CODE_SESSION_ID` in their design
   bodies (verified — remember-tool.md; the others don't name the var). The
   SKILL.md:57 `$CLAUDE_SESSION_ID` → `$CLAUDE_CODE_SESSION_ID` fix is folded
   into usage-logging.md's Files-touched (it already edits SKILL.md for the
   usage step), so the group lands the correction once.

   **Provenance naming drift.** skills/reflect/SKILL.md:57 tells reflect to
   emit `Session: $CLAUDE_SESSION_ID`, an env var that does not exist
   (verified: the real variable, present in both the Bash tool env and the
   spawned server env, is `CLAUDE_CODE_SESSION_ID`) — reflect's Session
   trailer silently never appears, while remember-tool's docstring names the
   correct var. Align the group on `CLAUDE_CODE_SESSION_ID`.
5. **RESOLVED:** build order adopted across the group (stable-ids → remember
   → usage-logging); the two later specs carry re-anchor notes and depend on
   the kind-less, `mem_…`-returning, `vec_search → (rowid, distance)` final
   surface this spec defines.

   **Recommended build order: stable-ids → remember → usage-logging.**
   stable-ids is the dependency root (both others persist/return its ids);
   remember second so the ingest.py rewrite and the helper extraction land
   back-to-back and remember's created memories are trackable from
   telemetry's first day; usage-logging last so it instruments the final
   three-tool, kind-less surface and its `vec_search → (rowid, distance)`
   signature change doesn't force rebases of the other two.
