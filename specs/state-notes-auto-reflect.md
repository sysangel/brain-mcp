# Per-project state notes + debt-triggered reflect

Status: spec, 2026-07-03. Two coupled features that move reflect from a
clock-driven nightly chore toward a self-maintaining, resume-ready loop:

- **Part 1 — rolling per-project state notes.** Reflect maintains one living
  document per active project at `memories/<project>-state.md` (rewritten each
  pass, never appended): current task, state with evidence, decisions, next
  actions, verified pointers. Because it lives in `memories/` it is indexed and
  recallable with **zero ingest/schema changes**.
- **Part 2 — debt-triggered reflect.** A reflection-debt check at brain-server
  startup (beside the existing background ingest thread) fires a detached,
  fire-and-forget `claude -p "/reflect"` when enough unconsolidated work has
  accumulated and enough time has passed — replacing the 03:30 launchd clock as
  the *primary* trigger. The launchd job is demoted to a backstop.

Assumptions about parallel specs (not designed here):

- **resume-skill** (separate spec, builds AFTER this): the upcoming `/resume`
  skill reads a project's state note at **exactly** `memories/<project>-state.md`
  (`<project>` = basename of cwd, matching `_project_of()` / the digest
  `project` field). **That path is a pinned cross-spec contract** — do not
  rename it without updating the resume-skill spec.
- **session-summaries** (already landed — the live SKILL.md has the step-4
  digest step, and `BRAIN_DIGEST_EXCLUDE` exists in config.py): state notes
  reuse its `project` convention and its privacy denylist. This spec builds on
  the current **10-step** SKILL.md, not the pre-session-summaries one.
- **git-sync** (already landed — SKILL.md step 1 pulls, step ~10 pushes):
  `memories/` is git-tracked and syncs across machines. State notes therefore
  sync too, so the `BRAIN_DIGEST_EXCLUDE` privacy gate applies to them exactly
  as it does to digests. `cache/` (last-reflect marker, lockfile) never syncs —
  reflection debt is inherently per-machine.
- **auto-recall-hook** (already landed): the ambient memories-only hook will
  surface state notes automatically, since they are `metadata.type: project`
  memories. That is a feature — a resume nudge with zero extra wiring.

## Problem

Two gaps, one shared root: reflect only runs at 03:30 via launchd, and it only
ever produces *append-only* records (per-session digests, distilled memories).

1. **No resume-ready per-project view.** Digests are per-session and
   append-only; a project's live state (what's the current task, what's
   decided, what's next) is smeared across N digests and M memories with no
   single "here is where this project stands right now" document. The CLAUDE.md
   resume protocol today reconstructs state by hand from `recall(...)` +
   `get_episode` on every resume — expensive and lossy. The forthcoming
   `/resume` skill needs a single, current, cheaply-read target.

2. **The clock is the wrong trigger.** A fixed 03:30 launchd job reflects even
   when nothing happened, and does **not** reflect when you did a full day's
   work and then `/clear` at 22:00 — the session's substance sits
   unconsolidated for 5.5 hours (and is invisible to any resume attempt in that
   window). The machine may also be asleep at 03:30 (a laptop), so the nightly
   silently no-ops for days. Consolidation should be driven by *unconsolidated
   work accumulating*, not by wall-clock time.

Hard constraints (both parts):

- Markdown+git is truth; `cache/brain.db` is a disposable derived index; no new
  MCP tools; `uv` venv only. (design rules a/c)
- The debt check runs inside brain-server startup and must **never** block,
  delay, or break MCP `initialize`, exactly like the existing ingest thread
  (server.py:108–138). Any failure logs one line to stderr and is swallowed.
- The spawned reflect must be genuinely fire-and-forget: it outlives the session
  that spawned it (a 5-minute session still gets its reflect) and can never
  recurse (the reflect's own `claude` starts a brain-server, which must not
  trigger *another* reflect).

## Design (chosen approach, plus alternatives rejected and why)

### 1. State-note location, shape, and lifecycle (Part 1)

`<BRAIN_DIR>/memories/<project>-state.md` — **one file per active project**,
flat in `memories/` (not a subdirectory), so `find_memory_files()` picks it up
unchanged. Verified: `find_memory_files()` (ingest.py:167–180) globs
`MEMORIES_DIR.glob("*.md")` **flat** (ingest.py:178–179) and appends it to the
Claude auto-memory files — so a top-level `*-state.md` is indexed as an ordinary
memory with **no ingest or schema change**, and (crucially) a
`memories/archive/` **subdirectory is NOT globbed**, which is what makes archival
a zero-code move (see lifecycle below).

`<project>` = basename of the session cwd, identical to `_project_of()`
(db.py:451–457) and the digest `project` field. **This is the resume-skill
contract path.**

Frontmatter mirrors an ordinary memory (parse_memory, ingest.py:222–270):

```markdown
---
name: <project>-state
description: "<project> — <one-line current status>"   # ≤150 chars, first line of the note
metadata:
  type: project
date: 2026-07-04       # UTC date of THIS reflect; refreshes recency each rewrite
---

## Task
<the current objective, one or two sentences>

## State (with evidence)
- <fact> — <evidence: commit sha / verdict / file:line / digest session id>

## Decisions
- <decision> — <why, half a sentence> (do not re-litigate on resume)

## Next actions
1. <the single most important next action, first>
2. <...>

## Pointers (verified this pass)
- <path / command / URL> — <what it is>
```

- **Living, rewritten wholesale** — reflect overwrites the file each pass; it is
  never appended to and never merged. `date:` is set to the reflect run date, so
  parse_memory's content hash changes (it includes `date`, ingest.py:225–228),
  the row re-embeds, and recency ranking keeps the freshly-updated note on top.
- **Written directly by reflect, not via the `remember` tool** — `remember` is
  dedup-guarded (DUP_COSINE) and would *block* a near-identical rewrite; reflect
  writes the file and lets the ordinary memory-ingest loop pick it up (same as
  how step 6 distillation edits memory files today).
- **Exempt from compaction.** The Compact step's "~50-file / ~20-line-body cap,
  merge near-duplicates" rules (SKILL.md step 7) must **not** touch
  `*-state.md`: they are intentionally longer than a distilled fact and must
  never be merged into another memory. This exemption is added to the Compact
  step text (see §5). This mirrors how session digests were carved out of the
  memories compaction lifecycle.

**Lifecycle / prune policy (the "gone quiet" rule):**

| Project status this pass | Action |
|---|---|
| Had ≥1 non-sidechain episode since last reflect | **Rewrite** `memories/<project>-state.md` |
| No activity this pass, note exists, newest episode < `BRAIN_STATE_STALE_DAYS` old (default 30) | **Leave as-is** — still indexed and resumable |
| No activity, newest episode ≥ `BRAIN_STATE_STALE_DAYS` old | **Archive**: `git mv memories/<project>-state.md memories/archive/<project>-state.md` |
| On `BRAIN_DIGEST_EXCLUDE` (cwd/project substring match) | **No state note ever** (see privacy below) |

Archival is a plain `git mv` into `memories/archive/` — the flat `*.md` glob
excludes the subdir, so the note drops out of the index on the next ingest while
staying in git history and on disk. If an archived project reactivates, the
active branch writes a fresh top-level `memories/<project>-state.md`; reflect
`git rm`s any stale `memories/archive/<project>-state.md` in the same pass so a
project never has two state files. (`memories/archive/` is committed the same as
`memories/`, since the commit step already stages all of `memories/`.)

**Which projects get a note.** Only projects that clear the same substance bar
as a digest candidate — i.e. a project that has produced at least one
digest-worthy session — get a state note, to prevent one-off `cd`-into-a-dir
sessions from sprouting notes. Reflect already computes per-project activity for
its window; it maintains a state note for each active, substantive,
non-excluded project.

**Privacy.** State notes live in git-tracked, multi-machine-synced `memories/`,
so a sensitive project (e.g. personal-finance) must never get one. Reflect drops any
project whose cwd/project matches a `BRAIN_DIGEST_EXCLUDE` substring — the exact
same denylist that already gates digests (config.py:30–32). This is mandatory,
not optional: without it, state notes would leak excluded-project status to
every synced machine.

**Rejected — append-only project log** (`memories/<project>-log.md`, append each
pass): grows unbounded, defeats the "single current view" purpose, and re-embeds
a growing blob every pass. The whole value is *current* state, not history — git
log already is the history.

**Rejected — store state notes under `summaries/` or a new `state/` dir**: would
need ingest/schema work (new finder/parser/table), breaking the "zero ingest
change" property that is the entire point of putting them in `memories/`.

### 2. The "last reflect" marker (reality, not assumption)

**Reflect already records exactly the marker we need: `cache/last-reflect`, a
single-line ISO-8601 UTC timestamp** (e.g. `2026-07-04T00:38:52Z`). Verified:
SKILL.md step 2 reads it to set the review window, step ~10 writes the current
UTC ISO timestamp; the live file exists and holds `2026-07-04T00:38:52Z`. The
Guardrails also guarantee a *no-op* reflect still writes it ("Nothing worth
keeping is a fine outcome: write `cache/last-reflect`, commit nothing") — so the
marker resets even when a pass produces no commit, which means debt can never
falsely accumulate after a genuine (if empty) reflect.

There is **no** DB meta key or git-only marker for reflect: `get_meta`/`set_meta`
(db.py:227–235) are used only for `last_ingest` and `schema_version`, and the
`reflect: …` git commits (git-tracked, coarse — date only, and absent on a
"no durable facts" pass) are too lossy to gate on. So we **reuse
`cache/last-reflect`** and add **no new marker**. It is `cache/`-local, hence
per-machine — correct, since reflection debt is per-machine.

We add a `LAST_REFLECT_PATH = CACHE_DIR / "last-reflect"` config constant so the
Python side and the skill share one definition (the skill currently hardcodes
the path).

### 3. Debt check at startup (Part 2)

Runs in the **existing** `_ensure_fresh_index` daemon thread (server.py:108–138),
appended after the incremental ingest so the debt count reads a freshly-topped
index. It never runs on the main thread; `mcp.run()` answers `initialize`
immediately regardless.

```
def maybe_trigger(log):                      # new: src/brain/autoreflect.py
    if os.environ.get("BRAIN_NO_AUTOREFLECT") == "1":   # (b) recursion guard
        return
    if not DB_PATH.exists():
        return
    last = _last_reflect()                   # parse cache/last-reflect ISO-Z; None if missing/bad
    age_h = inf if last is None else (now_utc - last).total_seconds() / 3600
    if age_h < REFLECT_MIN_AGE_H:            # default 20h
        return
    debt = _debt(conn, last)                 # sessions with activity since last reflect
    if debt < REFLECT_DEBT_SESSIONS:         # default 1
        return
    claude = _resolve_claude()               # (c) robust binary resolution
    if claude is None:
        log("[brain] autoreflect: claude binary not found; skipping"); return
    # (a) single-flight + fire-and-forget: spawn the wrapper DETACHED
    subprocess.Popen(
        [sys.executable, "-m", "brain.autoreflect"],   # same venv python — no PATH lookup
        stdin=subprocess.DEVNULL,
        stdout=open(AUTOREFLECT_LOG, "a"), stderr=subprocess.STDOUT,
        start_new_session=True,              # setsid: survives this session ending
        close_fds=True,
    )
    log(f"[brain] autoreflect: debt={debt} age={age_h:.1f}h — spawned reflect")
```

**The debt count.** Sessions with unconsolidated activity, straight off the
index:

```sql
SELECT count(DISTINCT session_id) FROM episodes
WHERE is_sidechain = 0 AND session_id IS NOT NULL AND ts > ?;   -- bind last-reflect ISO string
```

Note the parameter is the *stored* `cache/last-reflect` ISO string, compared
lexically against episode `ts` (also ISO-8601 UTC with `Z`) — this is a valid
string comparison and needs **no** `strftime` gymnastics. (This differs from the
digest candidacy query, which compares against `datetime('now')` and *does* need
the load-bearing `strftime('%Y-%m-%dT%H:%M:%S', …)` — do not confuse the two.)
Missing/unparseable `last-reflect` → `age_h = inf` and the debt is counted from
the epoch, so the first server start after a cache wipe schedules exactly one
catch-up reflect (then the marker resets). The current live session contributes
~no debt: at server startup its episodes are not yet ingested.

**Why in the daemon thread, not a launchd/cron replacement or a synchronous
check:** the ingest thread already exists, already runs on every startup, and is
already exception-firewalled and off the initialize path — the debt check is a
few ms of SQLite on top. A synchronous check would risk delaying `initialize`
(forbidden). A separate scheduler is exactly the clock we are trying to escape.

### 4. The wrapper `brain-autoreflect` — single-flight, recursion-safe, robust binary (Part 2)

A tiny new console script (`brain-autoreflect = "brain.autoreflect:main"`) that
is the **single choke point** for *actually running* a reflect. Both trigger
sources funnel through it: the debt spawn (§3) and the demoted launchd backstop
(§6). Its whole job:

```
def main() -> int:                           # src/brain/autoreflect.py
    force = "--force" in sys.argv
    fh = open(REFLECT_LOCK_PATH, "w")         # cache/.reflect.lock (gitignored via cache/)
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)   # (a) single-flight
    except OSError:
        print("[autoreflect] a reflect is already running — skipping"); return 0
    if not force:                             # backstop self-guard: skip if a recent reflect exists
        last = _last_reflect()
        if last is not None and (now_utc - last).total_seconds()/3600 < REFLECT_MIN_AGE_H:
            print("[autoreflect] last reflect too recent — skipping"); return 0
    claude = _resolve_claude()
    if claude is None:
        print("[autoreflect] claude binary not found", file=sys.stderr); return 1
    cmd = [claude, "-p", "/reflect", "--model", "opus",
           "--permission-mode", "acceptEdits", "--allowedTools", *REFLECT_ALLOWED_TOOLS]
    if os.environ.get("BRAIN_AUTOREFLECT_DRYRUN") == "1":   # smoke knob — never burns a run
        print("[autoreflect] DRYRUN cmd:", " ".join(cmd)); return 0
    env = {**os.environ, "BRAIN_NO_AUTOREFLECT": "1"}       # (b) recursion guard on the child
    return subprocess.run(cmd, cwd=str(BRAIN_DIR), env=env).returncode
    # flock auto-releases when fh closes / this process dies (clean OR crash) — no TTL, no stale reclaim
```

Guards, mapped to the mandatory list:

- **(a) Lockfile — single-flight.** `fcntl.flock(LOCK_EX|LOCK_NB)` on
  `cache/.reflect.lock`, held for the wrapper's entire lifetime (which equals the
  reflect run's lifetime). Two concurrent sessions each spawn a wrapper; only one
  wins the flock, the other prints one line and exits. This mirrors the exact
  doctrine `sync.py:maybe_pull` uses for the pull lock (sync.py:282–287): the OS
  releases `flock` automatically on process death, so a crashed reflect needs
  **no** TTL and **no** stale-lock reclaim. The lock lives in the *wrapper*, not
  the server, precisely because the server dies when the user's session ends
  while the detached reflect must keep running.
- **(b) Recursion guard.** The wrapper sets `BRAIN_NO_AUTOREFLECT=1` in the
  `claude` child's environment. `claude -p /reflect` spawns its own MCP
  `brain-server`, which inherits that env, so *its* `maybe_trigger` returns
  immediately (§3, first line). Env inherits down the whole tree
  (wrapper → claude → MCP brain-server). Fallback if a future claude sanitizes
  MCP-child env: also add `BRAIN_NO_AUTOREFLECT=1` to the brain MCP registration
  `env` block (documented in README) — belt-and-braces, normally unneeded.
- **(c) Robust claude resolution.** Daemon/launchd PATH may lack `claude`.
  `_resolve_claude()`: `BRAIN_CLAUDE_BIN` env override → `shutil.which("claude")`
  → probe an ordered fallback list `[~/.local/bin/claude, ~/.claude/local/claude,
  /opt/homebrew/bin/claude, /usr/local/bin/claude]` for the first existing
  executable → `None`. Verified on this machine: `claude` resolves to
  `~/.local/bin/claude` (a symlink into `~/.local/share/claude/versions/…`), so
  the first probe entry is correct; `which` finds it under a login shell.
  Spawning the wrapper itself uses `sys.executable -m brain.autoreflect` (the
  already-running venv Python), which sidesteps the PATH problem for the *spawn*
  entirely — only the `claude` grandchild needs discovery.
- **(d) Last-reflect marker** — `cache/last-reflect`, reused as-is (§2). No new
  marker added.

**Failure isolation.** `maybe_trigger` is wrapped so any exception logs one
stderr line and is swallowed (it is called inside the daemon thread's existing
`try/except`, plus its own guard). The wrapper's failures land in
`cache/autoreflect.log`. The server is never affected.

`REFLECT_ALLOWED_TOOLS` is the single source of truth for reflect's tool
allowlist — the exact set currently inlined in the launchd plist
(`Bash(uv:*) Bash(sqlite3:*) Bash(git add:*) Bash(git commit:*) Bash(git log:*)
Bash(git status:*) Bash(git diff:*) Bash(hostname:*)`), lifted into
`autoreflect.py` so the debt path and the backstop share one list.

### 5. Reflect skill integration (Part 1)

One new step inserted into the current 10-step `skills/reflect/SKILL.md`, plus
one clause added to Compact. Insert a new **step 7, "Refresh per-project state
notes,"** after the current step 6 (Distill, lines 87–96) and before Compact;
renumber current steps 7→8 (Compact), 8→9 (Mine skills), 9→10 (Re-index/commit),
10→11 (Report). Exact text to insert:

> 7. **Refresh per-project state notes.** For each project with ≥1 non-sidechain
>    episode since the review window opened — excluding any whose cwd/project
>    matches `BRAIN_DIGEST_EXCLUDE`, and only for projects substantive enough to
>    have produced a digest — (re)write a single living note at
>    `memories/<project>-state.md` (`<project>` = basename of cwd). **Overwrite
>    it wholesale; never append.** Frontmatter: `name: <project>-state`,
>    `description` (≤150 chars, the current one-line status), `metadata.type:
>    project`, `date:` = today (UTC). Body (≤~25 lines): **Task** (current
>    objective), **State (with evidence)** — every claim carries a commit sha /
>    verdict / `file:line` / digest session id, **Decisions** (with the why; not
>    to be re-litigated on resume), **Next actions** (single most important
>    first), **Pointers** (paths/commands/URLs verified this pass). Same
>    redaction bar as memories. For a project with **no** activity this pass:
>    leave its note untouched if its newest episode is < `BRAIN_STATE_STALE_DAYS`
>    (default 30) old; otherwise `git mv` the note to
>    `memories/archive/<project>-state.md` (archived: still in git, no longer
>    indexed). If a reactivated project has a stale `memories/archive/` copy,
>    `git rm` it after writing the fresh top-level note. These notes are the
>    resume target read by `/resume` at exactly `memories/<project>-state.md`.

Compact step (renumbered 8) gains one clause: *"`*-state.md` files are living
per-project state — exempt from the ~50-file / ~20-line-body caps and never
merged into another memory; they are rewritten wholesale by step 7."*

No change to the marker write (renumbered step 10 already writes
`cache/last-reflect`) and no change to the commit set (renumbered step 10 already
commits all of `memories/`, which now includes `*-state.md` and
`memories/archive/` moves).

### 6. Demoting the launchd job to a backstop (Part 2)

> **SUPERSEDED 2026-07-12:** the launchd backstop was retired; the debt check at
> brain-server startup is now the sole reflect trigger. The design below is kept
> as an as-built historical record.

The 03:30 job is **kept as a backstop, not removed**. Two changes to
`scripts/brain-reflect.plist.template`:

1. Command becomes the wrapper instead of a raw `claude` line:
   `cd "$HOME/src/brain" && uv run --directory "$HOME/src/brain" brain-autoreflect`
   (still under `/bin/zsh -lc`, whose login shell supplies `uv`/`claude` on
   PATH). The inline `--permission-mode`/`--allowedTools` flags move **into** the
   wrapper (`REFLECT_ALLOWED_TOOLS`), so the plist no longer carries them.
   Note one deliberate delta: the wrapper's cmd pins `--model opus`, which the
   current plist line does not (it runs the default model) — the backstop's
   model changes with the demotion; flagged so it is not mistaken for drift.
2. Routing the backstop through the wrapper is what unifies single-flight across
   both triggers: if a debt reflect is mid-flight at 03:30, the nightly wrapper
   fails the flock and no-ops; if a debt reflect already ran within
   `BRAIN_REFLECT_MIN_AGE_H`, the wrapper's age self-guard (§4, non-`--force`
   path) no-ops. So the backstop only ever runs when the debt trigger *hasn't* —
   exactly a backstop. It also inherits the recursion guard for free (the wrapper
   sets `BRAIN_NO_AUTOREFLECT=1` on its `claude` child), which the **old** raw
   plist line did **not** have — a latent double-reflect bug the demotion fixes.

**Eventual-removal criterion (documented, not executed here):** remove the
launchd job once `cache/autoreflect.log` shows the debt path reliably firing
within each `BRAIN_REFLECT_MIN_AGE_H` window on every active machine — i.e. the
nightly wrapper has been consistently hitting its "too recent / already running"
no-op for a sustained period (a couple of weeks). Until then it covers the
machine that goes days without opening a brain session.

### Alternatives rejected

- **A synchronous debt check before `mcp.run()`** — risks delaying
  `initialize`; forbidden by the same rule that put ingest in a daemon thread.
- **A dedicated cron/launchd "debt poller"** — reintroduces the clock we are
  removing and adds a second scheduler to babysit. The server already starts on
  every session; that *is* the natural poll.
- **Server holds the flock, spawns `claude` directly** — the server dies when
  the user's session ends, releasing the lock while the detached reflect is still
  running, so a second session could double-fire. The lock must live with a
  process whose lifetime equals the reflect's — the wrapper.
- **`uv run brain-autoreflect` for the debt spawn** — depends on `uv` being on
  the daemon's PATH (it may not be). `sys.executable -m brain.autoreflect` uses
  the already-running venv interpreter — no PATH lookup, guaranteed same env.
- **A TTL/stale-reclaim lockfile** (write pid+timestamp, reclaim if older than
  N minutes) — unnecessary complexity. `flock` is released by the OS on process
  death, so a crashed reflect leaves no stuck lock. Matches `sync.py` doctrine.
- **A new DB meta key or git-only "last reflect" marker** — `cache/last-reflect`
  already exists, is cheap to read, and resets on no-op passes. Adding a marker
  would be redundant state to keep in sync.

## API / schema changes

- **No MCP tool changes. No DB schema changes. No new dependencies.** State
  notes are ordinary `memories/*.md` files indexed by the existing pipeline;
  the debt trigger is pure orchestration.
- **New console script:** `brain-autoreflect = "brain.autoreflect:main"` in
  `[project.scripts]`.
- **New module** `src/brain/autoreflect.py`: `maybe_trigger(log)`, `main()`,
  `_resolve_claude()`, `_last_reflect()`, `_debt(conn, last)`,
  `REFLECT_ALLOWED_TOOLS`.
- **`config.py` additions** (env-overridable, following the existing
  `_env_float`+int-cast style so a bad override falls back, never crashes):

  | Constant | Env var | Default |
  |---|---|---|
  | `REFLECT_DEBT_SESSIONS` | `BRAIN_REFLECT_DEBT_SESSIONS` | `1` |
  | `REFLECT_MIN_AGE_H` | `BRAIN_REFLECT_MIN_AGE_H` | `20.0` |
  | `STATE_STALE_DAYS` | `BRAIN_STATE_STALE_DAYS` | `30` |
  | `LAST_REFLECT_PATH` | — | `CACHE_DIR / "last-reflect"` |
  | `REFLECT_LOCK_PATH` | — | `CACHE_DIR / ".reflect.lock"` |
  | `AUTOREFLECT_LOG` | — | `CACHE_DIR / "autoreflect.log"` |
  | (claude override, read in autoreflect.py) | `BRAIN_CLAUDE_BIN` | unset → discovery |
  | (recursion guard, read in autoreflect.py) | `BRAIN_NO_AUTOREFLECT` | unset |
  | (smoke dry-run, read in autoreflect.py) | `BRAIN_AUTOREFLECT_DRYRUN` | unset |

- **No new gitignore entries:** `cache/.reflect.lock`, `cache/autoreflect.log`,
  and `cache/last-reflect` are all already covered by the `cache/` ignore
  (verified `.gitignore`). `memories/archive/*.md` is git-tracked (intended).

## Files touched (file:line anchors into current code)

> Anchor caveat (build order): recall-cli lands **first** and inserts ~11
> README lines (one Commands-fence line + a new section before the current
> :112) plus one `[project.scripts]` line — the README/pyproject line numbers
> below are pre-recall-cli and will shift; anchor by section heading / table
> key at build time.

- `/home/you/src/brain/src/brain/autoreflect.py` — **new module**
  (~120–150 LOC): the debt check `maybe_trigger`, the wrapper `main`, binary
  resolution, marker parse, debt query, `REFLECT_ALLOWED_TOOLS`.
- `/home/you/src/brain/src/brain/config.py:50` — after the
  `# Ingest / storage` block (near `INGEST_STALE_SECONDS`), add the reflect/debt
  constants above; extend the module-docstring env list (config.py:3–10) with
  `BRAIN_REFLECT_DEBT_SESSIONS`, `BRAIN_REFLECT_MIN_AGE_H`, `BRAIN_STATE_STALE_DAYS`,
  `BRAIN_CLAUDE_BIN`, `BRAIN_NO_AUTOREFLECT`, `BRAIN_AUTOREFLECT_DRYRUN`.
- `/home/you/src/brain/src/brain/server.py:16-17` — import
  `autoreflect`. `:126-131` — at the end of `_ensure_fresh_index`, after the
  ingest block (still inside the function, wrapped so it can never escape the
  existing `try/except` at :132), call
  `autoreflect.maybe_trigger(lambda s: print(s, file=sys.stderr))`. It must run
  *after* ingest so the debt count sees the topped-up index.
- `/home/you/src/brain/pyproject.toml:12-17` — add `brain-autoreflect`
  to `[project.scripts]` beside `brain-sync`; then `uv sync`.
- `/home/you/src/brain/skills/reflect/SKILL.md:96` — insert the new
  step 7 (text in §5) after step 6 (ends :96); renumber steps 7–10 (lines
  98–124) to 8–11. `:98-103` (Compact, renumbered 8) — add the `*-state.md`
  exemption clause. No change to the marker-write or commit lines (renumbered
  step 10, current :111–119) — they already write `cache/last-reflect` and
  commit `memories/`.
- `/home/you/src/brain/scripts/brain-reflect.plist.template:11`
  — replace the raw `claude -p "/reflect" …` ProgramArguments string with
  `cd "$HOME/src/brain" && uv run --directory "$HOME/src/brain" brain-autoreflect`;
  drop the inline `--permission-mode`/`--allowedTools` (now in the wrapper).
  Keep the 03:30 `StartCalendarInterval` and the log paths.
- `/home/you/src/brain/scripts/install-launchd.sh:2` — comment update
  only ("nightly **backstop** reflection"); no functional change.
- `/home/you/src/brain/README.md:118` — after "Session digests", add a
  short "Per-project state notes (`memories/<project>-state.md`)" subsection:
  living, rewritten-each-reflect, `metadata.type: project`, indexed as an
  ordinary memory, the pinned `/resume` contract path, archived to
  `memories/archive/` when stale, excluded projects get none.
  `:156` — under "Nightly reflection (launchd)", add a "Debt-triggered
  reflection" note: the startup debt check, the `BRAIN_REFLECT_DEBT_SESSIONS` /
  `BRAIN_REFLECT_MIN_AGE_H` / `BRAIN_CLAUDE_BIN` knobs, the launchd demotion to
  backstop and its removal criterion, and the `BRAIN_NO_AUTOREFLECT` MCP-env
  fallback.

## Implementation steps (ordered, each independently verifiable)

**Build order (queue):** recall-cli → **this spec (builds SECOND)** →
resume-skill. This lands after recall-cli and **before** resume-skill, which
consumes the `memories/<project>-state.md` contract this spec establishes.

1. **config.py**: add the constants. Verify:
   `uv run python -c "from brain.config import REFLECT_MIN_AGE_H, REFLECT_DEBT_SESSIONS, REFLECT_LOCK_PATH; print(REFLECT_DEBT_SESSIONS, REFLECT_MIN_AGE_H, REFLECT_LOCK_PATH)"`
   → `1 20.0 …/cache/.reflect.lock`; `BRAIN_REFLECT_DEBT_SESSIONS=3 uv run … `
   → `3`; a garbage override falls back to the default (not a crash).
2. **autoreflect.py — pure logic** (`_resolve_claude`, `_last_reflect`, `_debt`).
   Verify headless, no reflect burned:
   - `BRAIN_CLAUDE_BIN=/bin/echo uv run python -c "from brain.autoreflect import _resolve_claude; print(_resolve_claude())"`
     → `/bin/echo`; unset → the real `claude` path (or `None` with PATH scrubbed).
   - `_debt` against the live DB with a hand-set `last`: returns the distinct
     session count with `ts >` that timestamp (cross-check with a manual
     `sqlite3` run of the query in §3).
   - `_last_reflect()` parses the live `cache/last-reflect`; returns `None` for a
     missing/garbage file.
3. **autoreflect.py — wrapper `main` (DRYRUN + lock probes)**. The mandatory
   smoke, none of which runs a real reflect:
   - **Dry-run trigger:** `BRAIN_AUTOREFLECT_DRYRUN=1 uv run brain-autoreflect --force`
     (`--force` is required here: the live `cache/last-reflect` is usually
     fresh, so the non-force age self-guard would print "too recent" before
     ever reaching the DRYRUN print) → prints the exact
     `claude -p /reflect --model opus …` command and exits 0 **without**
     spawning claude. Confirms binary resolution + arg assembly.
   - **Lockfile probe:** hold the lock in a **background** process
     (`python -c "import fcntl; f=open('cache/.reflect.lock','w'); fcntl.flock(f, fcntl.LOCK_EX); import time; time.sleep(30)" &`),
     then run `BRAIN_AUTOREFLECT_DRYRUN=1 uv run brain-autoreflect --force` →
     prints "already running — skipping", exit 0. Release → the same command
     proceeds to the DRYRUN print (`--force` again, same age-guard reason).
   - **Recursion probe:** `BRAIN_NO_AUTOREFLECT=1 uv run python -c "from brain.autoreflect import maybe_trigger; maybe_trigger(print)"`
     → returns immediately, spawns nothing (add a temporary print or check no
     child appears / no line logged).
   - **Age self-guard:** with a fresh `cache/last-reflect` (now), non-`--force`
     `BRAIN_AUTOREFLECT_DRYRUN=1 uv run brain-autoreflect` → "too recent",
     exit 0; `--force` → proceeds to the DRYRUN print.
4. **server.py wiring**. Verify: `uv run brain-server` starts and answers
   `initialize` immediately (no added latency); with `cache/last-reflect`
   back-dated 24h and `BRAIN_AUTOREFLECT_DRYRUN=1` exported, the stderr log shows
   the `autoreflect: debt=… age=… — spawned reflect` line and the spawned
   wrapper logs the DRYRUN command to `cache/autoreflect.log` — **no real reflect
   runs**. With `BRAIN_NO_AUTOREFLECT=1` exported, no spawn line appears.
5. **pyproject.toml**: register `brain-autoreflect`; `uv sync`. Verify the
   console script resolves and step-3 probes pass through `uv run
   brain-autoreflect` identically.
6. **SKILL.md edits** (new step 7, renumber 7–10→8–11, Compact exemption).
   Verify: read back; run the §3 debt SQL against the live DB and confirm the
   count matches a manual `count(DISTINCT session_id)`.
7. **plist demotion + install-launchd comment**. Verify:
   `plutil -lint scripts/brain-reflect.plist.template` passes;
   `./scripts/install-launchd.sh` then `launchctl print gui/$(id -u)/local.brain.reflect`
   shows the wrapper command; exercise the backstop command line by running the
   plist's exact string under `/bin/zsh -lc` with `BRAIN_AUTOREFLECT_DRYRUN=1`
   exported → prints the DRYRUN line. Do **not** test via
   `BRAIN_AUTOREFLECT_DRYRUN=1 launchctl kickstart …` — env vars prefixed onto
   `launchctl` apply to the `launchctl` process, not the launchd job, so DRYRUN
   would be silently dropped and (with a back-dated marker) a **real** reflect
   would burn. Backstop path exercised without burning a run.
8. **End-to-end, one controlled real run** (only after 1–7 pass): back-date
   `cache/last-reflect` 24h, **unset** `BRAIN_AUTOREFLECT_DRYRUN`, start one
   brain session (or run `uv run brain-autoreflect --force`). Verify: exactly one
   `claude -p /reflect` runs; on completion `cache/last-reflect` advanced,
   `memories/<project>-state.md` written for each active project with valid
   frontmatter and evidence-bearing body, the reflect commit staged them, a
   second session start within 20h does **not** re-trigger (age gate), and no
   recursive reflect spawned (the reflect's own MCP server saw
   `BRAIN_NO_AUTOREFLECT=1`). Confirm a stale project's note archived to
   `memories/archive/`.
9. **README.md** sections. Verify they read correctly and pin the
   `memories/<project>-state.md` contract path.

## Risks & open questions

Risks:

- **Debt-triggered reflect can fire mid-session.** A resumed session with
  indexed episodes newer than `last-reflect` counts as debt, so opening a brain
  session may spawn a reflect while you keep working. Benign: reflect's own
  ≥1h-quiet digest gate skips the live session, the reflect runs detached
  against the index, and the `flock` prevents overlap. Cost is one background
  `claude` invocation.
- **State-note project-name collisions.** `<project>` is a cwd *basename*, so two
  different directories named `web` share one `memories/web-state.md` and would
  clobber each other. Same ambiguity the digest `project` field already carries;
  accepted for v1. Mitigation if it bites: qualify with a path hash — but that
  breaks the clean `/resume` contract path, so deferred.
- **MCP-child env inheritance is the recursion guard's load-bearing assumption.**
  If a future `claude` scrubs env before launching MCP servers,
  `BRAIN_NO_AUTOREFLECT=1` wouldn't reach the reflect's brain-server and it could
  recurse (bounded by the `flock` to one extra reflect, then the age gate stops
  it). Mitigation documented: also set the var in the brain MCP registration
  `env`. Verify inheritance in step 8.
- **Headless reflect writes state notes unreviewed.** A bad pass writes a bad
  state note that `/resume` then trusts. Mitigated: git-tracked (reviewable /
  revertable), ≤~25 lines bounds blast radius, and the note carries evidence
  pointers a resume can re-verify.
- **Laptop-asleep backstop gap shrinks but persists.** The debt trigger fires
  whenever you *open* a session, which is far more reliable than 03:30, but a
  machine you never open for days still relies on the launchd backstop (which
  also needs the machine awake). Acceptable — the debt path strictly improves on
  the status quo.
- **`ts` lexical comparison** assumes episode `ts` and `last-reflect` are both
  ISO-8601 UTC `Z`. Verified true today; a future timezone-offset format would
  break the `>` comparison. Low risk (ingest writes UTC-Z).

Open questions:

1. **Thresholds.** Are `BRAIN_REFLECT_DEBT_SESSIONS=1` / `BRAIN_REFLECT_MIN_AGE_H=20`
   the right defaults? `debt≥1` means "reflect the first time you open a session
   ≥20h after the last reflect" — effectively once-a-day on an active machine.
   Higher debt or lower age would reflect more/less eagerly.
2. **State-note scope bar.** Gating state notes on "produced a digest-worthy
   session" reuses the digest substance threshold — is that the right bar, or
   should any project with N+ episodes since last reflect get one? Too loose
   sprouts notes for throwaway dirs; too tight misses a young project.
3. **`BRAIN_STATE_STALE_DAYS=30` archive horizon** — long enough that a paused
   project stays resumable, short enough that `memories/` doesn't accumulate dead
   notes. Tunable; may want per-project override later.

## Size estimate (S/M/L and rough LOC)

**M.** ~150 LOC of Python (`autoreflect.py` ~120–150: debt query, marker parse,
binary discovery, detached spawn, wrapper with flock/dry-run/recursion env;
`config.py` ~10; `server.py` ~3) plus ~40 lines of markdown (SKILL.md ~25 —
the new step 7 + Compact clause; README ~15) and ~2 lines of plist/script edits.
No migrations, no new dependencies, no new MCP tools, no ingest/schema changes.
Part 1 is almost entirely a reflect-skill (LLM-step) change riding the existing
memory pipeline; Part 2 is the only real code.

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES (folded in)**

Verified against the live system:

- `find_memory_files()` globs `MEMORIES_DIR.glob("*.md")` **flat**
  (ingest.py:178–179) and includes it alongside the Claude auto-memory dirs — so
  `memories/<project>-state.md` is indexed as an ordinary memory with zero
  ingest/schema change, and `memories/archive/` (a subdir) is genuinely NOT
  globbed, making archival a zero-code `git mv`. Part 1's central claim holds.
- The "last reflect" marker is real: `cache/last-reflect` exists, holds a
  single-line ISO-8601 UTC-`Z` timestamp (`2026-07-04T00:38:52Z`), is written by
  SKILL.md's last step and read by step 2, and resets on no-op passes
  (Guardrails). No DB meta key or git marker exists for reflect
  (`get_meta`/`set_meta` only carry `last_ingest`/`schema_version`). Reusing it —
  and adding **no** new marker — is correct.
- `claude` resolves to `~/.local/bin/claude` here (symlink into
  `~/.local/share/claude/versions/2.1.199`); the probe list's first entry
  matches. `sys.executable -m brain.autoreflect` avoids the PATH problem for the
  spawn.
- `.gitignore` covers `cache/` (lock, log, marker) and `local/`; no new ignore
  needed. The plist currently inlines the `--allowedTools` set that
  `REFLECT_ALLOWED_TOOLS` reproduces.
- `_ensure_fresh_index` is already a daemon thread off the `initialize` path with
  a swallow-all `try/except` (server.py:108–138) — the debt check slots in after
  ingest with no latency risk.

Fixes folded into the design during review:

1. **Latent double-reflect in the *old* plist.** The current launchd line runs
   raw `claude -p /reflect` with **no** `BRAIN_NO_AUTOREFLECT`; once the debt
   check exists, that nightly claude's own MCP brain-server would itself trigger
   a second reflect. Resolved by routing the backstop through the wrapper (§6),
   which sets the recursion env — the demotion is also a bug fix.
2. **Detached-lock lifetime.** An earlier "server holds the lock" sketch would
   release the lock when the user's session ends mid-reflect. Resolved by placing
   the `flock` in the wrapper, whose lifetime equals the reflect run
   (§4/Alternatives), matching `sync.py:maybe_pull` doctrine (flock auto-released
   on process death — no TTL).
3. **`strftime` confusion.** The debt query compares episode `ts` to the *stored*
   `last-reflect` ISO string (a plain lexical `>`), NOT to `datetime('now')` — so
   it must **not** copy the digest query's `strftime('%Y-%m-%dT%H:%M:%S', …)`
   dance. Called out explicitly in §3 to prevent a builder from cargo-culting it.
4. **State-note privacy.** State notes sync via `memories/`, so the
   `BRAIN_DIGEST_EXCLUDE` denylist is applied to them as a **mandatory** gate
   (§1), not an afterthought — otherwise excluded-project status would leak to
   every synced machine.

Residual (tracked in Open questions): threshold defaults, the state-note scope
bar, and MCP-child env inheritance (the one runtime assumption to confirm on the
live end-to-end run, step 8).
