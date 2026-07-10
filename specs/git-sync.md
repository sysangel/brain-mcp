# git-sync — multi-machine memory via a private GitHub remote

Status: scoped, not implemented. Repo is local-only today (`git remote -v` is
empty, branch `main`).

Related features (spec'd in parallel — assumptions only, not designed here):

- **session-summaries**: git-tracked digests land in `summaries/` and are
  **first-class synced content** — they ride this exact sync just like
  `memories/`/`proposals/` (committed in the reflect commit step; see
  "### Group integration") and partially close the episode gap (Design §3).
  One writer per file (digest candidacy is machine-local) → no merge attribute
  needed. Digest **privacy** is handled upstream by session-summaries'
  `BRAIN_DIGEST_EXCLUDE` denylist, which keeps sensitive projects (e.g.
  personal-finance) out of `summaries/` before anything is ever committed — so the
  privacy gate on remote creation (step 8) is **resolved**: it is safe to run
  once that denylist ships (group-integration item 1).
- **remember tool**: writes + commits a memory mid-session; it calls
  `sync.push()` after its commit — this feature exposes the function, that
  feature owns the hook.
- **usage-logging**: writes only under `cache/` (gitignored, `.gitignore:2`)
  → it can never sync; nothing to do here.

## Problem

The brain runs on one machine. Memories (`memories/*.md`, git = source of
truth) and proposals exist only in the local clone; a second machine (and the
eventual DigitalOcean box) has no way to receive them or contribute back.
Meanwhile the nightly reflect commits locally and is told to "Never push"
(`skills/reflect/SKILL.md:57`), so even with a remote nothing would flow.

Constraints that shape everything:

1. Markdown in git is the source of truth; `cache/brain.db` is rebuildable
   and never leaves the machine.
2. Sync must NEVER block or break ingest/serve — offline is the default
   assumption (laptop asleep at 03:30, no Wi-Fi on a plane).
3. Raw transcripts (`~/.claude/projects/**.jsonl`) are machine-local and are
   NOT in this repo — episode recall is inherently per-machine.
4. No force-push, no cache in the remote, no silent loss of either side of a
   conflict.
5. Subscription-only, uv-only; don't paint the DigitalOcean port into a
   corner (i.e. nothing macOS-only in the sync core — launchd stays the
   scheduler shell, sync itself is plain git + Python).

## Design (chosen approach, plus alternatives rejected and why)

### Overview

A single new module `src/brain/sync.py` (plus console script `brain-sync`)
owns all git-remote interaction. Everything else — server, ingest CLI,
reflect skill — calls into it at four touch points. Sync is enabled by
auto-detection (`git remote get-url origin` succeeds) and hard-disabled with
`BRAIN_SYNC=0`; a clone with no remote behaves exactly as today, zero config.

Every git subprocess runs with `GIT_TERMINAL_PROMPT=0`,
`GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10"`, and a 30s
`subprocess.run` timeout, so a dead network yields a fast, logged failure —
never a hang, never a password prompt inside a daemon thread or headless job.
Every failure degrades to local-only with one grep-able stderr line
(`[brain-sync] pull failed (offline?): <reason> — continuing local-only`)
and, for the CLI, a nonzero exit code that callers treat as advisory.

### 1. Sync points — minimal set, mapped to actual code

| # | Point | Where | Direction | Throttle |
|---|-------|-------|-----------|----------|
| 1 | before ingest | top of `ingest.run_ingest()` (`src/brain/ingest.py:245`) | pull | yes: skip if last successful pull < 15 min, flock-guarded |
| 2 | before reflect | reflect SKILL.md step 1, before `brain-ingest` | pull (unthrottled) + drain-push if ahead | no |
| 3 | after reflect commit | reflect SKILL.md step 7, after `git commit` | push | no |
| 4 | after any memory-writing commit | `sync.push()` exposed for the remember tool | push | no |

Why this set and nothing more:

- **The MCP server never *pushes* directly.** `server.py:67` already funnels
  through `run_ingest()`, so hooking point 1 inside `run_ingest` covers both
  the server's startup daemon thread (`server.py:52-78`) and the `brain-ingest`
  CLI (`ingest.py:431`) with essentially one code change. Pull must precede
  the file scan (it changes `memories/*.md` mtimes, which is exactly how the
  ingester notices new content), hence "top of run_ingest". One nuance: the
  server's `_ensure_fresh_index` runs `run_ingest` only when the index is >1h
  stale, so to avoid a fresh-index session skipping the pull entirely,
  `maybe_pull` is *also* invoked at the top of `_ensure_fresh_index` before
  the staleness gate (throttled, so it costs nothing when recent — see Design
  §4 and Files touched). Both call sites hit the same throttled, flock-guarded
  `maybe_pull`, so double-invocation is a cheap no-op.
- **The server never pushes.** It never writes memories, so it has nothing to
  say. Pushes happen only where commits happen: reflect and (assumption) the
  remember tool. A push at reflect pushes the whole branch, so any commit
  stranded by an earlier offline push gets drained at most ~24h later; the
  reflect-step drain-on-pull (point 2) usually closes it sooner. Whether the
  server's throttled pull should also drain-push is Open Question 3.
- **The launchd plist needs no change.** The reflect skill will invoke
  `uv --directory ~/src/brain run brain-sync pull|push`, and the plist's
  allowlist already contains `Bash(uv:*)`
  (`scripts/brain-reflect.plist.template:11`) — no new allowed tools,
  no wrapper script around `claude -p`.
- **Throttling & concurrency**: several Claude Code sessions can each spawn a
  `brain-server`, so point 1 is guarded by (a) a marker file
  `cache/last-sync-pull` (epoch of last successful pull; skip if < 900s old)
  and (b) a non-blocking `fcntl.flock` on `cache/sync.lock` (if held, another
  process is already pulling — skip silently). git's own `index.lock` is the
  last line of defense; a locked repo is just another logged, degraded pull.

### 2. Pull strategy & conflict policy

Chosen: **ff-only first, rebase fallback, union merge driver for memory
files, abort-and-flag for anything else.** Concretely, `sync.pull()`:

1. `git fetch origin` (timeout-bounded). Failure → log, done (local-only).
2. If `git status --porcelain --untracked-files=no` shows dirty *tracked*
   files: attempt `git merge --ff-only origin/main` only. The
   `--untracked-files=no` flag is load-bearing — plain `git status
   --porcelain` also lists untracked (`??`) entries, and the repo currently
   has an untracked `specs/` dir, so a naive non-empty check would wedge every
   pull into ff-only/skip indefinitely. (Separately: `specs/` should be
   committed so it stops registering as untracked at all.) A dirty tree is
   never rebased and never autostashed — mid-edit reflect/remember state must
   not be juggled by a background thread. Non-ff + dirty → skip with log;
   retry next cycle.
3. Clean tree: `git merge --ff-only origin/main`. If non-ff (both machines
   committed), the response depends on **which path is calling**:
   - **Background pull** (`maybe_pull`, from the server/ingest daemon): stop
     at ff-only — log the non-ff and skip, never rebase. A daemon thread must
     not `git rebase`, because the brain repo mixes data with `src/` +
     `skills/` code the user actively edits; a background rebase can rewrite
     local unpushed SHAs and move `main` under a concurrent interactive git
     session (the "harmless" risk note only ever covered *external*
     references).
   - **Explicit pull** (`brain-sync pull` — reflect step 1 and manual runs):
     `git rebase origin/main`. Rebase keeps history linear and keeps each
     machine's commit trailers (`Session:`/`Project:`/`Host:`) attached to
     their own commits. This is the **only** path that rebases, and it runs
     foreground where a single writer (the user, or the nightly reflect that
     owns the tree at 03:30) is in control.
4. Rebase conflict:
   - `memories/*.md` and `proposals/*.md` carry `merge=union` via a new
     `.gitattributes`. Union merge concatenates both sides' lines — a
     conflict there auto-resolves with BOTH texts preserved, never a lost
     side. Memories are per-file, append-mostly, so real overlaps are rare;
     when union does produce duplicate lines (e.g. both machines appended to
     the same body, or duplicated frontmatter lines), the next reflect
     compaction pass (SKILL.md step 5) is the semantic deduplicator — add one
     sentence there: "repair duplicate frontmatter/body lines left by union
     merges."
   - Any OTHER path conflicting (README, src/, skills/): `git rebase --abort`,
     write `cache/sync-conflict` (a flag file containing the conflicting
     paths + both SHAs), log loudly. Nothing is lost: local commits stay on
     `main`, remote commits stay on `origin/main`. While the flag exists,
     throttled pulls (point 1) skip entirely to avoid thrashing; an explicit
     `brain-sync pull` still attempts and clears the flag on success.
     `brain-sync status` and reflect's report (step 8) both surface the flag
     so a human resolves it with ordinary git. No headless auto-resolution of
     source-code conflicts, ever.

Rejected alternatives:

- **merge (default `git pull`)**: merge commits pollute the reflect-oriented
  history and make the trailer convention harder to read; buys nothing since
  union drivers work identically under rebase (rebase uses the merge
  machinery per commit and honors `.gitattributes`).
- **favor-newest (checkout --theirs/--ours by timestamp)**: silently discards
  one side — violates the never-lose rule outright.
- **park conflicting files in `conflicts/` for reflect to reconcile**: more
  moving parts than union-merge and reachable only in the rare
  double-edit-same-lines case that union already survives; for non-memory
  files, letting an LLM resolve code conflicts headless at 03:30 is how repos
  get eaten. Abort-and-flag is dumber and safer.
- **autostash for dirty trees**: stash-pop can itself conflict in a
  background thread with nobody watching. Deferring the pull 15 minutes is
  free.

### 3. The episode asymmetry — accept it

Raw transcripts never enter the repo, so `recall scope=episodes` remains
per-machine forever: machine B will NOT see machine A's verbatim exchanges,
exact error text, or code snippets from A's sessions. What machine B *does*
see: all synced `memories/` (each machine's nightly reflect distills its own
transcripts into the shared store, so durable facts converge within ~24h),
all `proposals/`, and — once session-summaries lands — git-tracked digests of
A's sessions. One honest wrinkle: memories are told to "reference session ids
instead" of quoting transcripts (SKILL.md:39); on machine B such a reference
to an A-session cannot be dereferenced via `get_episode`. Session-summaries is
a **partial** answer to that, not a full one: digests cover only
heuristic-passing sessions (≥3 main-chain episodes OR ≥15k chars, ≤5 written
per night newest-first) and skip any project on the `BRAIN_DIGEST_EXCLUDE`
denylist. So machine B will still have **no digest at all** for (a) short Q&A
sessions below the heuristic, (b) substantive sessions still sitting in the
nightly-cap backlog, and (c) deliberately excluded (e.g. personal-finance) projects
— a session-id reference to any of those stays a dead link on B. The digest
closes the gap for substantive, non-excluded sessions only; the rest remain
machine-A-local.

Rejected episode-export schemes:

- **Sync raw jsonl through the repo**: transcripts are huge, churn constantly
  (actively-written files), and occasionally contain secrets/tool payloads —
  a private-repo blast radius the memory store shouldn't carry.
- **Export episode rows (sqlite dump / jsonl) as a synced artifact**:
  violates rule (a) — the index is a rebuildable cache, and a synced export
  is a second source of truth that immediately drifts.
- **Markdown episode digests in-repo**: that IS the session-summaries
  feature; designing it here would duplicate a parallel spec.

Recommendation: accept the asymmetry; reflect + session-summaries are the
carriers of cross-machine knowledge.

### 4. Auth & plumbing

**Chosen: a dedicated SSH key for `<your-account>` behind an ssh-config host
alias**, remote URL `git@<remote-host>:<your-account>/brain.git`.

Why not the `gh` credential helper: `gh auth status` on this machine shows
TWO accounts — `<other-account>` (active, git protocol https) and `<your-account>`.
The gh https credential helper authenticates as the *active* account
per-host, not per-repo, so pushes to `<your-account>/brain` would go out as
`<other-account>` (403 on a private <your-account> repo) unless the active account is
switched — which would silently break the work account's git flows. A host
alias pins this one repo to the right identity with zero interaction with
the work setup, works headless, has no token expiry, and translates directly
to the future DigitalOcean box (same alias, same key type).

The 03:30 launchd job:

- The plist (`scripts/brain-reflect.plist.template:13-21`) has
  `StartCalendarInterval` 03:30, `RunAtLoad=false`, and **no wake/power
  keys**. launchd semantics: if the machine is *asleep* at 03:30 the job
  fires (coalesced) at next wake; if *powered off* it is skipped. We do not
  add `pmset` wake scheduling — pull-on-ingest (point 1) heals a missed night
  the next time an ingest actually runs, and the next successful push drains
  all pending commits. Precise caveat: the server's background thread calls
  `run_ingest` only when the index is >1h stale (`server.py:59-66`,
  `INGEST_STALE_SECONDS = 3600`), so a session started against a *fresh* index
  would otherwise perform no pull at all. To make "heals when a session
  starts" literally true, `sync.maybe_pull(log)` is called at the top of
  `_ensure_fresh_index` **before** the staleness check; it is independently
  throttled by `cache/last-sync-pull` (900s), so this adds no extra fetch
  traffic — it just removes the fresh-index blind spot (see point 1 / Files
  touched: server.py).
- gui-domain LaunchAgents inherit `SSH_AUTH_SOCK` from macOS's
  launchd-managed ssh-agent, so the key is available headless provided it's
  loaded (`AddKeysToAgent yes` + `UseKeychain yes` in the alias block, or a
  passphrase-less key). If the agent has no key at 03:30, the push fails
  BatchMode-fast and logs — reflect still commits locally.
- Network down at 03:30: `fetch`/`push` die on `ConnectTimeout=10` →
  `[brain-sync] … continuing local-only` in `cache/reflect.log`; the commit
  rides the next successful sync. No step of reflect is blocked.

### 5. One-time setup (documented in README; user runs these, nothing automated)

Machine A (this machine — repo exists, `gh` active account is `<other-account>`):

```sh
# 1. Key + alias for the <your-account> identity
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_brain -C "brain-sync"
cat >> ~/.ssh/config <<'EOF'
Host <remote-host>
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_brain
  IdentitiesOnly yes
  AddKeysToAgent yes
  UseKeychain yes
EOF

# 2. Create the private repo under <your-account> (switch, create, switch back).
#    `gh ssh-key add` needs the admin:public_key scope, which the default
#    login token does NOT carry (verified: the tokens here hold only
#    gist,read:org,repo[,workflow]). Refresh the scope first — OR skip the
#    `gh ssh-key add` line entirely and upload ~/.ssh/id_ed25519_brain.pub
#    manually at https://github.com/settings/keys while signed in as <your-account>.
gh auth switch --user <your-account>
gh auth refresh -h github.com -s admin:public_key   # grants SSH-key upload
gh ssh-key add ~/.ssh/id_ed25519_brain.pub --title "brain $(hostname -s)"
gh repo create <your-account>/brain --private --description "personal memory"
gh auth switch --user <other-account>

# 3. Wire the remote through the alias and push
cd ~/src/brain
git remote add origin git@<remote-host>:<your-account>/brain.git
git config user.name "Your Name"
git config user.email you@example.com   # stable identity; machine lives in the Host trailer
git config pull.rebase true                      # guard manual `git pull` too
git push -u origin main
```

Machine B (later):

```sh
# same ssh-keygen + ssh-config block, then register the new per-machine key:
#   gh auth switch --user <your-account>
#   gh auth refresh -h github.com -s admin:public_key   # default token lacks it
#   gh ssh-key add ~/.ssh/id_ed25519_brain.pub --title "brain $(hostname -s)"
#   gh auth switch --user <other-account>
#   (or upload the .pub at https://github.com/settings/keys as <your-account>)
git clone git@<remote-host>:<your-account>/brain.git ~/src/brain
cd ~/src/brain && git config user.name "Your Name" && git config user.email you@example.com && git config pull.rebase true
uv sync && uv run brain-ingest --full
claude mcp add brain -- uv run --directory ~/src/brain brain-server
./scripts/install-launchd.sh   # consider staggering: edit Minute to 0 / Hour to 4 first (see Risks)
```

Verification: `uv run brain-sync status` prints remote, ahead/behind, last
pull time, and conflict-flag state.

### 6. Safety

- **cache/ never pushed**: already gitignored (`.gitignore:1-4` covers
  `cache/`, `*.db*`); `sync.py` never runs `git add` at all — it only moves
  commits made by reflect/remember. Belt-and-suspenders: nothing in sync can
  invent a commit.
- **Never force-push**: `sync.push()` runs exactly `git push origin main`.
  On a non-ff rejection it runs `pull(log, drain=False)` (rebase + union; the
  `drain=False` is precisely what stops the push→pull→drain-push→pull… loop)
  and retries the push once; still rejected → log + degrade. No code path
  constructs `--force`/`--force-with-lease`, and the spec forbids adding one.
- **Machine identity**: keep the existing `Host: <hostname -s>` trailer
  (README.md:77-86, SKILL.md step 7) as THE machine marker, and normalize
  committer identity to a stable repo-local `user.email`
  (`you@example.com`) on both machines — today's auto-generated
  `you@your-machine.local` would otherwise make authorship
  differ per machine and duplicate what the trailer already encodes. Two
  machines committing is then fully auditable: `git log --format='%h %(trailers:key=Host,valueonly)'`.

## API / schema changes

No SQLite schema changes; no MCP tool changes. Sync state deliberately lives
in files, not the DB — sync must work while the DB is mid-rebuild.

- **New module** `src/brain/sync.py`:
  - `enabled() -> bool` — `BRAIN_SYNC != "0"` and `git remote get-url origin`
    succeeds (cached per process).
  - `maybe_pull(log) -> None` — throttled, flock-guarded, exception-proof;
    the entry `run_ingest` and the server's `_ensure_fresh_index` use. Skips
    when `cache/sync-conflict` exists. Calls `pull(log, drain=False,
    allow_rebase=False)` — the background path is ff-only and never pushes
    (Design §2 step 3; Open Question 2).
  - `pull(log, drain=True, allow_rebase=True) -> bool` — unthrottled: fetch,
    ff-only, then rebase fallback **only when `allow_rebase`** (the explicit
    `brain-sync pull` path; background callers pass `allow_rebase=False` and
    just log-and-skip on non-ff), union/abort policy; on success clears the
    conflict flag, stamps `cache/last-sync-pull`, and, when `drain` and the
    branch is ahead, runs a **bare `git push origin main`** with **no non-ff
    recovery** — a rejected drain push just logs and waits for the next cycle.
    This bare drain is what breaks the push↔pull recursion.
  - `push(log) -> bool` — `git push origin main`; on non-ff rejection call
    `pull(log, drain=False)` (so the recovery pull never drain-pushes back into
    `push`), then retry the push exactly once; still rejected → log + degrade.
  - `status() -> dict` — remote URL, ahead/behind counts, last-pull epoch,
    conflict flag.
  - `main()` — CLI: `brain-sync pull|push|status`; exit 0 on success/no-op,
    1 on degraded failure (callers treat as advisory, never fatal).
- **New console script** in `pyproject.toml`: `brain-sync = "brain.sync:main"`.
- **New constants** in `config.py`: `SYNC_PULL_MIN_INTERVAL = 900`,
  `SYNC_GIT_TIMEOUT = 30`, `LAST_SYNC_PATH = CACHE_DIR / "last-sync-pull"`,
  `SYNC_LOCK_PATH = CACHE_DIR / "sync.lock"`,
  `SYNC_CONFLICT_FLAG = CACHE_DIR / "sync-conflict"`. Env override:
  `BRAIN_SYNC=0` disables all sync.
- **New file** `.gitattributes`:

  ```
  memories/*.md  merge=union
  proposals/*.md merge=union
  ```

  (`union` is a git built-in driver — no `git config` required, works on any
  clone including the future DO box.)

## Files touched (file:line anchors into current code/scripts)

| File | Anchor | Change |
|------|--------|--------|
| `/home/you/src/brain/src/brain/sync.py` | new | the module above (~180 LOC) |
| `/home/you/src/brain/.gitattributes` | new | 2 lines, union driver |
| `/home/you/src/brain/src/brain/config.py` | :36 (after `INGEST_STALE_SECONDS`) | +6 lines of sync constants |
| `/home/you/src/brain/src/brain/ingest.py` | :245 (top of `run_ingest`, before the `--full` drop at :247) | +2 lines: `from . import sync` / `sync.maybe_pull(log)` |
| `/home/you/src/brain/pyproject.toml` | :11-14 `[project.scripts]` | +1 line `brain-sync` |
| `/home/you/src/brain/skills/reflect/SKILL.md` (anchors assume session-summaries has already landed — it inserts step 4 and renumbers 4–8 → 5–9; see "### Group integration" item 3) | step 1 (:19-20) | prepend `uv --directory ~/src/brain run brain-sync pull` (best-effort: on failure, note it and continue) |
| | step 6 "Compact" (was step 5) | +1 sentence: "repair duplicate frontmatter/body lines left by union merges." |
| | step 8 "Re-index and commit" (was step 7) | build on session-summaries' version of this step (already commits `summaries/` + adds `, N digests`): after the commit, run `uv --directory ~/src/brain run brain-sync push`; failure is non-fatal, note in report. Final merged text in "### Group integration" item 3 |
| | Guardrails | +1 bullet: sync failures never abort a reflection; if `cache/sync-conflict` exists, report it, don't resolve it headless |
| `/home/you/src/brain/README.md` | :67-86 | new "Multi-machine sync" section (setup above) + amend the trailer section |
| `/home/you/src/brain/src/brain/server.py` | :52-78 (`_ensure_fresh_index`) | +1 line: `sync.maybe_pull(log)` at the top of `_ensure_fresh_index`, BEFORE the staleness check — so a session against a *fresh* index still pulls (throttled ⇒ no extra fetch). Without it the server pulls only when the index is >1h stale (Design §1/§4, issue 3) |
| `/home/you/src/brain/scripts/brain-reflect.plist.template` | :11 | NO change — `Bash(uv:*)` already covers `uv run brain-sync`; no wake keys added |
| `/home/you/src/brain/.gitignore` | :1-4 | NO change — `cache/` already excluded, which keeps the flag/marker/lock files and telemetry out of the remote |

## Implementation steps (ordered, each independently verifiable)

1. **Add `.gitattributes`** (union for `memories/*.md`, `proposals/*.md`).
   Verify: `git check-attr merge memories/coding-style-preferences.md` → `merge: union`.
2. **Add sync constants to `config.py`.** Verify:
   `uv run python -c "from brain import config; print(config.SYNC_PULL_MIN_INTERVAL)"`.
3. **Write `src/brain/sync.py`** with the API above; register `brain-sync` in
   `pyproject.toml`; `uv sync`. Verify with NO remote configured:
   `uv run brain-sync status` → "sync disabled (no origin)", exit 0;
   `uv run brain-sync pull` → same, exit 0; `uv run brain-ingest` still clean.
4. **Local two-clone integration test (no network, no GitHub).** In the
   scratchpad: `git clone --bare ~/src/brain fake-origin.git`, make two
   clones A/B pointing at it, set `BRAIN_DIR` per clone; commit disjoint
   memory edits in each, then same-file appends in each. Verify:
   `brain-sync pull`/`push` round-trips; the same-file case union-merges with
   both lines present; a forced `src/` conflict aborts, writes
   `cache/sync-conflict`, and subsequent throttled pulls skip while explicit
   pull retries. Verify no command ever prompts (run with `SSH_ASKPASS=/bin/false`).
5. **Hook `sync.maybe_pull(log)` into `run_ingest`** (`ingest.py:245`).
   Verify: `uv run brain-ingest` twice in <15 min against the fake origin —
   first pulls, second logs the throttle skip; with the remote unreachable
   (point origin at a bogus path) ingest completes normally with the degrade
   line; `uv run brain-server` still answers initialize instantly.
6. **Update `skills/reflect/SKILL.md`** (steps 1, 5, 7, guardrails). Verify:
   run `/reflect` interactively against the fake origin; log shows pull →
   ingest → commit → push; kill the origin and rerun — reflection completes,
   report notes the failed sync.
7. **Update `README.md`** with the Multi-machine sync section. Verify: proof-read;
   commands copy-paste clean.
8. **One-time remote setup** (user runs the §5 commands). Verify:
   `uv run brain-sync status` shows `origin git@<remote-host>:<your-account>/brain.git`,
   ahead 0 / behind 0; a nightly log line in `cache/reflect.log` shows a real
   push after the next reflection.

Steps 1–7 are fully testable before any GitHub repo exists; step 8 is the
only networked action and is reversible (`git remote remove origin` returns
the system to today's behavior exactly).

## Risks & open questions

Risks:

- **Union-merge artifacts**: simultaneous edits to the same memory can leave
  duplicate frontmatter/body lines. Mitigated by reflect compaction (step 5
  amendment); worst case is ugly-but-lossless markdown for one day.
- **Union merge can corrupt the *indexed* view for up to a day.** A
  union-merged duplicate `---` fence makes `parse_memory` split at the FIRST
  closing fence (ingest.py:194-211), so stray frontmatter lines spill into the
  body and a wrong `description` can be indexed until the next compaction pass
  repairs the file. "Ugly-but-lossless" holds for git history, not for recall
  in that window. Mitigation is the same compaction-repair sentence added to
  SKILL.md step 6 (Compact); accepted as a rare, self-healing, single-day
  degradation of recall quality, never of the underlying markdown.
- **Simultaneous 03:30 reflects on two machines** race on push; the
  pull-rebase-retry loop resolves it, but staggering machine B (e.g. 04:00)
  shrinks the window to ~zero. Documented in setup; not enforced.
- **Headless ssh at 03:30**: if the key isn't in the agent/keychain when
  launchd fires, pushes fail until an interactive session loads it. Degrade
  path covers it; `brain-sync status` makes it visible.
- **gh dual-account foot-gun**: any future `gh repo ...` command against this
  repo while `<other-account>` is active may 404/403 on the private <your-account>
  repo. Day-to-day sync never uses gh (pure git+ssh), so exposure is
  one-time-setup only.
- **Rebase rewrites local unpushed SHAs** — harmless for *external*
  references (nothing references brain SHAs externally). To keep it harmless
  for *local* work too, rebase now runs only on the explicit foreground path
  (`brain-sync pull` / reflect step 1), never in a background daemon thread
  (Design §2 step 3): the background path is ff-only, so it can never rewrite
  history or move `main` under a concurrent interactive git session.
- **DO-box portability**: design is plain git+ssh+Python + one built-in merge
  driver; launchd is the only macOS piece and is already isolated in
  `scripts/`. Nothing new binds to macOS.

Open questions (need the user):

1. **Auth**: confirm the dedicated-SSH-key + `<remote-host>` host-alias
   approach — the alternative (switching gh's active account to <your-account> for
   an https credential) interferes with the work account `<other-account>` on the
   same machine.
2. **Server-side drain-push**: should the MCP server's throttled background
   pull also push when the branch is ahead (fastest healing of
   offline-at-03:30 nights), or should pushes stay exclusively at
   reflect/remember points (server never opens an outbound write)? Spec
   defaults to the conservative latter; drain happens only on explicit
   `brain-sync pull` (reflect step 1).
3. **Stagger the nightly jobs**: keep 03:30 on both machines (simpler, races
   handled) or offset machine B to 04:00 (recommended)?

## Size estimate (S/M/L and rough LOC)

**M.** ~180 LOC new Python (`sync.py`), ~10 LOC config/pyproject/ingest
edits, 2 LOC `.gitattributes`, ~15 lines of SKILL.md edits, ~45 lines of
README. Total ≈ 250 LOC; the two-clone integration test (step 4) is the bulk
of the verification effort, not the code.

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES**

Verified against the live system: `git -C ~/src/brain remote -v` is empty,
branch `main`; `gh auth status` confirms exactly the claimed dual-account
state (<other-account> active + <your-account>, both https protocol); the plist matches
every claim (`Bash(uv:*)` in the :11 allowlist, 03:30 StartCalendarInterval,
`RunAtLoad=false`, no wake/power keys, logs to `cache/reflect.log`); launchd
sleep-coalescing semantics are stated correctly; `.gitignore:1-5` covers
`cache/` and `*.db*`; server.py/ingest.py/SKILL.md/README line anchors check
out.

Issues:

1. **RESOLVED:** Design §2 step 2 now uses `git status --porcelain
   --untracked-files=no` so untracked entries (the current `specs/` dir) can't
   wedge every pull into ff-only/skip; also notes `specs/` should be committed.

   **Untracked files defeat the dirty-tree gate as written.** Design §2 step
   2 keys off `git status --porcelain`, which also lists untracked (`??`)
   entries — and the repo has an untracked `specs/` dir right now, so a
   naive non-empty check would degrade every background pull to
   skip/ff-only indefinitely. Fix: use
   `git status --porcelain --untracked-files=no`.
2. **RESOLVED:** the drain is now a **bare `git push`** with no non-ff
   recovery, and `push()`'s recovery pull passes `drain=False` — stated in
   both the API section and Design §6. push→pull→drain-push→pull… can no
   longer form.

   **`pull(drain=True)` and `push()` are mutually recursive as spec'd.**
   API section: pull "(drain) pushes if ahead"; Design §6: push on non-ff
   "runs `pull()` … retries the push once". push→pull→drain-push→pull… is
   unbounded under a moving remote. Fix: drain is a bare `git push` with no
   recovery, and push's recovery pull passes `drain=False`.
3. **RESOLVED:** the claim is corrected in Design §4 (the server ingests only
   when the index is >1h stale), and `sync.maybe_pull(log)` is now called at
   the top of `_ensure_fresh_index` before the staleness check (Files touched:
   server.py; Design §1) — throttled, so it heals the fresh-index case at no
   extra fetch cost.

   **"Heals the moment a Claude session starts" is overstated.** The server
   thread calls `run_ingest` only when the index is >1h stale
   (server.py:59-66, `INGEST_STALE_SECONDS` config.py:36); a session started
   against a fresh index performs no pull at all. Fix: correct the claim, or
   call `sync.maybe_pull` unconditionally in `_ensure_fresh_index` before
   the staleness check.
4. **RESOLVED:** the background path is now restricted to ff-only; rebase
   runs only on the explicit `brain-sync pull` path (Design §2 step 3, the
   `allow_rebase` gate in the API section, and the updated Risks note). A
   daemon thread can never rewrite history or move `main`.

   **Background rebase of an actively-developed repo.** The brain repo mixes
   data with `src/`+`skills/` code the user edits; `maybe_pull`'s rebase
   fallback runs in a server daemon thread and can rewrite local unpushed
   SHAs and move `main` under a concurrent interactive git session (the risk
   note's "harmless" only covers external references). Fix: restrict the
   background path to ff-only; allow rebase only in explicit
   `brain-sync pull` (reflect step 1).
5. **RESOLVED:** §5 now inserts `gh auth refresh -h github.com -s
   admin:public_key` (while <your-account> is active) before `gh ssh-key add` on
   both machine A and machine B, with a manual web-UI upload
   (github.com/settings/keys) documented as the alternative.

   **§5 setup fails at `gh ssh-key add`: missing scope.** Verified: both
   tokens carry only `gist, read:org, repo[, workflow]` — uploading an SSH
   key requires `admin:public_key`. Fix: insert
   `gh auth refresh -h github.com -s admin:public_key` (while <your-account> is
   active) before the `gh ssh-key add` line, or upload the key via web UI.
6. **RESOLVED:** added a Risks bullet acknowledging that a union-merged
   duplicate `---` fence can mis-split `parse_memory` and index a wrong
   `description`/body for up to a day; the compaction-repair sentence (SKILL.md
   step 6) stays the mitigation, explicitly scoped as a recall-only,
   self-healing, single-day degradation.

   **Union merge can mangle the *indexed* view of a memory.** A union-merged
   duplicate `---` fence makes `parse_memory` split at the FIRST closing
   fence (ingest.py:194-211), so stray frontmatter lines land in the body
   and the wrong `description` may be indexed until the next compaction.
   "Ugly-but-lossless" holds for git, not for recall for up to a day. Fix:
   acknowledge in Risks; the compaction-repair sentence stays the mitigation.

### Group integration

1. **RESOLVED:** digest-exclusion is now a config denylist,
   `BRAIN_DIGEST_EXCLUDE` (owned by session-summaries: config constant +
   heuristic post-filter + SKILL.md step-4 text). Reflect drops excluded
   cwds/projects (e.g. personal-finance) before writing any digest, so sensitive
   content never enters `summaries/` and therefore never reaches the remote.
   This spec now names `summaries/` as first-class synced content (Related
   features note) and records the privacy gate on step 8 as satisfied once the
   denylist ships.

   **Privacy is flagged only by the spec that doesn't create the exposure.**
   session-summaries raises digest privacy (its Open Question 2); git-sync —
   which actually pushes digests to GitHub, and rejected transcript sync
   partly on secret blast-radius grounds — never mentions `summaries/`
   content sensitivity. Once the remote exists (step 8), every pre-existing
   digest (personal-finance sessions included) pushes at once. Fix: decide
   digest-exclusion rules (by cwd/project) before executing git-sync step 8.
2. **RESOLVED:** Design §3 now calls session-summaries a "**partial** answer"
   and spells out exactly what machine B still won't see — no digest at all
   for short sub-heuristic Q&A sessions, sessions still in the nightly-cap
   backlog, and denylisted projects; a session-id reference to any of those
   stays a dead link on B.

   **Asymmetry-closure claim is stronger than session-summaries delivers.**
   Design §3 calls session-summaries "the designed answer" to
   non-dereferenceable session-id references in memories on machine B — but
   digests cover only heuristic-passing sessions (≥3 eps OR ≥15k chars,
   ≤5/night); a memory may cite a session that never gets a digest. Fix:
   soften to "partial answer".
3. **RESOLVED:** the splice order is fixed — **session-summaries lands first**
   (inserts step 4, renumbers 4–8 → 5–9), then git-sync re-anchors onto the
   result (this spec's Files-touched SKILL.md rows now target step 1, step 6
   "Compact", step 8 "Re-index and commit", and Guardrails by name, not the
   old line numbers). The single final text of the merged commit step is
   written **here, once**, so no builder reconciles two conflicting edits:

   > 8. **Re-index and commit.** Run `brain-ingest` again to pick up memory
   >    and digest edits. Write the current UTC ISO timestamp to
   >    `cache/last-reflect`. Then one commit of `memories/` + `proposals/` +
   >    `summaries/`:
   >    `reflect: YYYY-MM-DD — N added, N updated, N pruned, N digests` with
   >    trailers `Host: <hostname -s>` and, when the env var exists,
   >    `Session: $CLAUDE_CODE_SESSION_ID`. Then push:
   >    `uv --directory ~/src/brain run brain-sync push` — best-effort and
   >    non-fatal; on failure note it in the report and continue (the commit
   >    rides the next successful sync). This replaces the old "Never push."
   >
   > (Step 1 also gains the pull: prepend `uv --directory ~/src/brain run
   > brain-sync pull` as a best-effort first action. Note the trailer env var
   > is `CLAUDE_CODE_SESSION_ID` — the real variable in the spawned env — not
   > the `CLAUDE_SESSION_ID` that today's SKILL.md:57 still names.)

   **Colliding SKILL.md edits.** session-summaries inserts a new step 4 and
   renumbers 4–8→5–9; this spec targets steps 1/5/7 by number and line
   (:19-20, :44, :52-57, :63-69), and BOTH rewrite the commit step (this one
   replaces "Never push", that one adds `summaries/` + digest counts).
   Whichever lands second must re-anchor; write the final commit-step text
   once: pull in step 1, commit `memories/` + `proposals/` + `summaries/`,
   push in the final commit step.
4. **RESOLVED (confirmed, no change):** `summaries/` is accounted for
   correctly. The Related-features note now names it explicitly as first-class
   synced content; one-writer-per-file genuinely holds because digest
   candidacy is driven by machine-local episodes, so omitting `summaries/`
   from `.gitattributes` remains right — recorded so nobody "fixes" it by
   adding a union driver.

   **`summaries/` is accounted for correctly here** (Related features note):
   one-writer-per-file genuinely holds because digest candidacy is driven by
   machine-local episodes, so omitting `summaries/` from `.gitattributes` is
   right — no fix needed, recorded so nobody "fixes" it by adding union.
5. **RESOLVED (recorded):** build order is session-summaries first, git-sync
   second — and the step-8 privacy gate is now satisfied by the denylist
   (item 1), so step 8 is no longer blocked on an open decision; it only
   requires the denylist to have shipped.

   **Recommended build order: session-summaries first, git-sync second.**
   Summaries is local-only, needs no auth, and drains today's 4-session
   backlog on night one; git-sync steps 1–7 can follow any time (re-anchored
   per item 3), with step 8 (remote creation, the only irreversible-ish
   action) gated on the privacy decision in item 1.
