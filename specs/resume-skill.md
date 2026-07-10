# Spec: a `/resume` skill — pick up a project at near-zero main-window cost

> **2026-07-04: superseded in part.** The skill was renamed `/catchup`
> (`skills/catchup/`) because `resume` shadowed Claude Code's built-in
> `/resume`, and it is now the deep-recovery path only — routine resume is the
> `brain-session-start` SessionStart injection. See
> `specs/session-start-hook.md`. The subagent protocol below is otherwise
> still accurate.

Status: proposed · Depends on: recall-cli (`brain-recall` — spec'd at
`specs/recall-cli.md`), state-notes-auto-reflect (`memories/<project>-state.md`
— spec'd at `specs/state-notes-auto-reflect.md`) · Builds LAST (third)

> **Stated assumptions (dependencies):** this skill consumes two pinned
> cross-spec contracts that have not yet landed as code:
>
> - **recall-cli** (`specs/recall-cli.md`) adds a `brain-recall` CLI
>   (`uv run --directory ~/src/brain brain-recall "<query>"`, with
>   `--scope all|episodes|memories|summaries` and `--get <ep_…|mem_…|sum_…>`) —
>   flag names verified verbatim against that spec. The subagent uses this
>   **Bash** CLI, never the MCP tools, so it always runs fresh code (a live MCP
>   server can hold a stale schema — see CLAUDE.md's "stale server" note).
>   `brain-recall` is **not** in `pyproject.toml` `[project.scripts]` today
>   (only `brain-ingest`, `brain-server`, `brain-hook`, `brain-sync`,
>   `brain-usage-report`).
> - **state-notes-auto-reflect** (`specs/state-notes-auto-reflect.md`) — pins
>   the path `~/src/brain/memories/<project>-state.md` **verbatim** (verified
>   against that spec's §1; a naming drift would silently send every resume
>   down the degradation path). Reflect owns writes to it; this skill only
>   reads it — that spec states the same ownership boundary.
>
> Until both land, `/resume` still works via its degradation path (MCP brain
> tools + digests/episodes) — see §Degradation. That is why this builds third:
> after both dependencies, the clean path is available; before them, it runs
> degraded but functional.

## Problem

The "Resuming work" protocol currently lives inline in `~/src/CLAUDE.md`
(§ Resuming work): recall project state, recall digests, `get_episode` the top
1–3 hits, cross-check freshness against the brain's git log and the target
repo, then brief the user. Executing it inline is exactly the kind of expensive
legwork the house "lean by default" rule says to delegate: it runs two-plus
recalls and pulls 1–3 full episodes (each up to 20k raw chars) **into the main
window**, which then has to turn around and do the actual work with a context
already polluted by raw retrieval transcripts. The main window should hold the
distilled brief — the task, the evidence, the single next action — never the
raw recall it was distilled from.

The `/handoff` skill's resume mode partially covers this, but (a) it reads a
single latest handoff note (an insurance artifact written before autonomous
runs), not the brain's rolling state, and (b) it still reads inline in the main
window. So resuming from the brain proper — state note + digests + episodes,
freshness-checked — has no lean path today.

The feature: a `/resume` skill that spends near-zero main-window tokens. One
subagent does all the retrieval and cross-checking and returns only a
~400-word structured brief; the main session relays it and acts. This is the
`/lean` discipline (hold briefs and verdicts, never raw bulk) applied to
session continuity.

## Design (chosen approach, plus alternatives rejected and why)

### 1. Trigger and project resolution

`/resume [project]`, or a natural request ("pick up the brain project", "resume
where we left off"). Resolution order:

1. The `/resume <project>` argument, if given.
2. Else infer from the conversation topic or cwd (basename of the working dir,
   e.g. `~/src/brain` → `brain`).
3. If genuinely ambiguous (no arg, no clear cwd/topic), ask the user **once**,
   then proceed. Never silently guess between two live candidates.
4. Headless (`claude -p`, no user to ask): infer from cwd, proceed, and flag
   the inference in the brief's Staleness warnings (resolved decision).

Single-mode, unlike `/handoff` (which selects write vs. resume). `/resume` is
always resume; writing state back to the brain is reflect's job, not this
skill's.

### 2. One subagent, model `opus`, Bash-only

The whole design is **one** subagent that does every expensive step and returns
one coherent brief. Rationale for each choice:

- **One, not inline.** The point of the feature is main-window context cost.
  Inline recall defeats it. The subagent burns a fresh context window on the
  raw retrieval; the main window receives only the ≤400-word brief.
- **One, not a fan-out.** The steps are a short sequential pipeline (sync →
  ingest → read state → recall → cross-check → verify), each cheap. Splitting
  recall and freshness-check across two subagents would return two partial
  briefs the main window then has to reconcile — merge cost for no parallelism
  benefit.
- **Model `opus`.** Per the house model economy, Opus does scoping / searching
  / legwork; Fable is reserved for adversarial verification and final
  distillation. Reconstructing state is legwork (sync, ingest, search,
  cross-check), so `opus`.
- **Bash-only, CLI not MCP.** The subagent shells out to `brain-recall` /
  `brain-ingest` / `brain-sync`, never the MCP `recall`/`get_episode` tools.
  The CLI runs fresh code every invocation; a long-lived MCP server may serve
  an old schema after brain code changes (CLAUDE.md "stale server" note). MCP
  tools remain the **fallback** when the CLI is absent (§Degradation).

### 3. The subagent brief (the load-bearing contract)

The subagent runs these steps, in order (verbatim brief text lives in the
SKILL.md body, §API / skill contract):

1. `uv run --directory ~/src/brain brain-sync pull` — best-effort; on failure
   (offline, no remote) note "brain not synced" and continue. Never blocks.
2. `uv run --directory ~/src/brain brain-ingest` — incremental, near-instant;
   makes episodes current through the last session so the recall in step 4
   sees the latest work.
3. Read `~/src/brain/memories/<project>-state.md` if it exists (the rolling
   state note, written by reflect). Missing → fall back to digests + episodes
   and note it.
4. `uv run --directory ~/src/brain brain-recall "<project> state decisions next
   steps"`, then again with `--scope summaries`; fetch full text of the top
   1–3 hits with `brain-recall --get <id>` (snippets are too thin to brief
   from — the same rule the inline protocol states). CLI absent → MCP fallback.
5. Freshness cross-check: `git -C ~/src/brain log -5 --oneline` **and** the
   target repo's own `git log`/`git status`. The brain is only current through
   the last reflect; if the target repo moved past it, **trust the repo** and
   flag the drift.
6. Verify every file path / command it is about to cite still exists (a state
   note can point at files since moved or deleted).

### 4. The return contract (≤ ~400 words, exact structure)

The subagent returns **only** a distilled brief in exactly these fields —
mirroring the inline protocol's "brief the user" step and `/handoff`'s briefing
shape:

- **Task** — what the project is and what we were doing.
- **State** — done / in-progress / blocked, each with evidence (commit hashes,
  verdicts, test results).
- **Decisions already made** — calls not to re-litigate.
- **Single next action** — the one concrete first step.
- **Verified pointers** — exact file paths / commands, each confirmed to exist.
- **Staleness warnings** — only when the brain lagged reality, sync failed, the
  state note was missing, or a pointer failed to verify; omitted when clean.

No raw recall output, transcripts, or file dumps in the return — only the
brief. The ≤400-word cap and fixed structure are what enforce the distillation
that keeps the main window lean.

### 5. Main-session behavior

Relay the brief, then act on the **Single next action**. The main session
**must not** re-run recall or re-read anything the subagent already distilled —
the brief is the working set (lean context-hygiene rule). If a returned brief
is over-length or missing a section, send it back with a sharper brief; do not
patch the gap by reading in the main window.

### Rejected alternatives

- **Rejected: keep the protocol inline in CLAUDE.md.** The entire motivation is
  main-window cost; inline runs recall + `get_episode` in the window being kept
  lean. CLAUDE.md keeps only a one-line route + a fallback (§Files touched).
- **Rejected: reuse `/handoff resume`.** It reads one latest note (insurance
  artifact), not the rolling brain state, and reads inline. `/resume`
  supersedes the *read* side of handoff for brain-backed continuity; `/handoff`
  stays for its explicit note artifact before long autonomous runs.
- **Rejected: MCP tools as the primary retrieval path.** A live server can
  serve a stale schema after brain code changes; the Bash CLI always runs fresh
  code. MCP is the guaranteed fallback, not the default.
- **Rejected: multiple parallel subagents.** No real parallelism in a short
  sequential pipeline; two partial briefs cost a reconciliation step.
- **Rejected: model `fable` for the subagent.** Fable is for verification /
  distillation only; state reconstruction is legwork → `opus`.
- **Rejected: have the subagent `remember`/write an updated state note when it
  detects drift.** Reflect owns state-note writes (state-notes-auto-reflect);
  a read-path skill writing memory would race reflect and blur ownership. The
  subagent only *flags* drift in its Staleness warnings. (See open question 1.)

## API / skill contract (SKILL.md content + install)

No Python, no schema, no MCP tools, no `pyproject.toml` change. The deliverable
is one skill file plus a symlink, mirroring the in-repo `reflect`/`handoff`
precedent (in-repo skills use `name` + `description` frontmatter only; the
`triggers:` key is a standalone-skill convention, not used here).

**Location & install (mirror `reflect`):**

- File: `~/src/brain/skills/resume/SKILL.md` (committed in the **brain** repo).
- Symlink: `ln -s ../brain/skills/resume ~/src/skills/resume` — a **relative**
  directory symlink, exactly matching the real precedents
  (`~/src/skills/reflect -> ../brain/skills/reflect`,
  `handoff -> ../brain/skills/handoff`). Note the README's reflect snippet
  shows an absolute `ln -s ~/src/brain/...` form, but the on-disk links are
  relative — mirror the on-disk reality so `readlink` output matches the
  precedent. Committed in the **skills** repo (`~/src/skills` is its own git
  repo, not the brain repo); propagates to `~/.claude/skills/resume` for free
  because `~/.claude/skills -> ~/src/skills`.

**SKILL.md (exact content to write):**

```markdown
---
name: resume
description: Pick up a project where you left off, at near-zero main-window
  cost. Spawns one subagent that syncs and refreshes the brain, reads the
  project's rolling state, cross-checks freshness against the real repo, and
  returns a short distilled brief; the main session relays it and acts on the
  single next action. Use when asked to "resume", "pick up <project>",
  "pick up where we left off", "resume from brain", or "/resume".
---

# resume

Reconstruct a project's working state from the brain and hand the main session
one distilled brief to act on — without spending the main window on the
retrieval. All the expensive legwork (sync, ingest, state-note read, recall,
freshness cross-check, pointer verification) happens inside ONE subagent; the
main window only holds the brief. This is the /lean discipline applied to
resuming: hold the brief, never the raw recall.

## 1. Resolve the project

- Use the `/resume <project>` argument if given.
- Else infer from the conversation topic or cwd (basename of the working dir,
  e.g. `~/src/brain` -> `brain`).
- If genuinely ambiguous (no arg, no clear cwd/topic), ask once, then proceed.
  Never silently guess between two live candidates.
- Headless (`claude -p`, no user to ask): infer from cwd, proceed, and record
  the inference in the brief's Staleness warnings so it is auditable.

## 2. Spawn ONE subagent (model: opus) with this exact brief

Model `opus` — this is retrieval legwork (house model economy: Opus for
scoping/searching, Fable only for verification). Give the subagent this brief
verbatim, substituting `<project>`:

> You are reconstructing the working state of the `<project>` project from the
> brain (`~/src/brain`). Use **Bash only — no MCP tools** (the CLI always runs
> fresh code; a live MCP server may hold a stale schema). Run, in order:
>
> 1. `uv run --directory ~/src/brain brain-sync pull` — best-effort; on
>    failure (offline / no remote) note "brain not synced" and continue.
> 2. `uv run --directory ~/src/brain brain-ingest` — incremental, fast; makes
>    episodes current through the last session.
> 3. Read `~/src/brain/memories/<project>-state.md` if it exists — the rolling
>    state note reflect maintains. Missing → say so and fall back to digests +
>    episodes.
> 4. `uv run --directory ~/src/brain brain-recall "<project> state decisions
>    next steps"`, then again with `--scope summaries`. Fetch full text of the
>    top 1–3 hits with `brain-recall --get <id>` — snippets are too thin to
>    brief from. If `brain-recall` is not installed, fall back to the MCP
>    brain tools (`recall` / `get_episode`) and note the fallback.
> 5. Cross-check freshness: `git -C ~/src/brain log -5 --oneline` AND the
>    target repo's own `git log`/`git status`. The brain is only current
>    through the last reflect; if the target repo moved past it, **trust the
>    repo** and flag the drift.
> 6. Verify every file path and command you are about to cite still exists.
>
> Return ONLY a distilled brief, **max ~400 words**, in exactly this structure:
>
> - **Task** — what this project is and what we were doing.
> - **State** — done / in-progress / blocked, each with evidence (commit
>   hashes, verdicts, test results).
> - **Decisions already made** — calls not to re-litigate.
> - **Single next action** — the one concrete first step.
> - **Verified pointers** — exact file paths / commands, each confirmed to
>   exist.
> - **Staleness warnings** — only if the brain lagged reality, sync failed, the
>   state note was missing, or a pointer failed to verify. Omit if clean.
>
> Do not return raw recall output, transcripts, or file dumps — only the brief.

## 3. Relay and act

- Relay the subagent's brief, then act on the **Single next action**.
- Do NOT re-run recall or re-read anything the subagent already distilled — the
  brief is the working set. If it is over-length or missing a section, send it
  back with a sharper brief; never fill the gap by reading in the main window.
- If a staleness warning changes the plan, surface it before acting.

## Degradation

- `brain-recall` not installed (recall-cli not yet landed) → the subagent uses
  the MCP brain tools (`recall`, `get_episode`) and notes it.
- `<project>-state.md` missing (state-notes-auto-reflect not landed, or no note
  for this project) → fall back to digests + episodes; note it.
- Offline / no remote → skip the pull, brief from the local index, flag "brain
  not synced".
```

## Files touched (file:line anchors into current code)

| File | Repo / anchor | Change |
|---|---|---|
| `skills/resume/SKILL.md` | brain repo · new file | The SKILL.md above. |
| `~/src/skills/resume` | **skills** repo · new symlink | `ln -s ../brain/skills/resume ~/src/skills/resume` (relative, matching `reflect`/`handoff`); commit in the skills repo. Propagates to `~/.claude/skills/resume` via the existing `~/.claude/skills -> ~/src/skills` link. |
| `skills/handoff/SKILL.md` | brain repo · frontmatter lines 2–7 | **De-conflict triggers:** handoff's description currently claims "resume" and "pick up where we left off" — identical phrases to this skill's. Drop those two from handoff's trigger list (keep "hand off", "write a handoff"; add "handoff resume" for its explicit read mode) so `/resume` owns the resume phrasing. |
| `~/src/CLAUDE.md` | **not in git** (personal) · § Resuming work, lines 43–58 | Replace the 3-step inline protocol with a one-line route to `/resume` + a one-line fallback (text below). Cross-repo edit — outside the brain repo. |
| `README.md` | brain repo · after line 154 (end of "## Reflect skill" section) — **line number is pre-recall-cli/state-notes**: both earlier specs insert README lines above this point (~11 + ~15), so anchor by the section heading, not :154, at build time | Add a parallel "## Resume skill (session continuity)" section with the `ln -s` install snippet (text below). |

**CLAUDE.md § Resuming work — exact replacement:**

```markdown
## Resuming work ("resume from brain", "pick up where we left off")

Session continuity lives in the brain. On a resume request, run **/resume
[project]** — it spawns one subagent that syncs, refreshes the index, reads the
project's rolling state note, cross-checks freshness against the real repo, and
returns a short distilled brief; relay it and act on the single next action. Do
NOT run recall inline — the point is to keep the main window lean.

Fallback if the skill is unavailable: `recall("<project> state decisions next
steps")` + `recall("<project>", scope="summaries")`, `get_episode` the top 1–3
hits, cross-check the newest against `git -C ~/src/brain log -3` and the target
repo, then brief with the single next action.
```

**README.md — new section after the Reflect skill section** (4-backtick outer
fence because the snippet itself contains a fenced `sh` block):

````markdown
## Resume skill (session continuity)

`skills/resume/SKILL.md` reconstructs a project's working state from the brain
at near-zero main-window cost: it spawns one subagent that syncs, incrementally
ingests, reads the project's rolling state note, recalls + cross-checks
freshness against the real repo, and returns a short distilled brief the main
session acts on. Symlink it into the skills repo like reflect:

```sh
ln -s ../brain/skills/resume ~/src/skills/resume
```
````

## Implementation steps (ordered, each independently verifiable)

0. **Precondition (soft).** Note whether recall-cli (`brain-recall` in
   `pyproject.toml` scripts) and state-notes-auto-reflect
   (`memories/*-state.md`) have landed. Not blocking — the skill degrades — but
   the smoke test's expected path depends on it. *Verify:* `uv run --directory
   ~/src/brain brain-recall --help` succeeds (clean path) or fails (degraded
   path expected).
1. **Write `skills/resume/SKILL.md`** (content in §API). *Verify:* frontmatter
   parses (name/description); file reads back intact.
2. **Create the symlink.** `ln -s ../brain/skills/resume ~/src/skills/resume`
   (relative — matches the on-disk `reflect`/`handoff` links). *Verify:*
   `readlink ~/src/skills/resume` → `../brain/skills/resume`;
   `ls ~/.claude/skills/resume/SKILL.md` resolves through both symlink hops; a
   fresh Claude session lists `resume` as a skill.
3. **De-conflict handoff triggers.** Edit `skills/handoff/SKILL.md`
   frontmatter: remove "resume" and "pick up where we left off" from its
   description's trigger phrases; add "handoff resume". *Verify:* neither
   phrase appears in handoff's description; `/handoff resume <topic>` still
   documented in its Mode selection.
4. **Edit `~/src/CLAUDE.md`** § Resuming work (replacement text in §Files
   touched). *Verify:* the section now routes to `/resume` and carries the
   one-line fallback; no other section changed.
5. **Add the README section** after the Reflect skill section. *Verify:* reads
   correctly; the `ln -s` snippet matches step 2.
6. **Smoke test** (headless — see below). *Verify:* the returned brief has all
   six fields, is ≤ ~400 words, and every listed pointer resolves.

## Smoke plan (headless-runnable)

Drive the skill once against the **real** brain for a project that certainly
has history — `brain` itself:

```sh
claude -p "/resume brain" --dangerously-skip-permissions 2>&1 | tee /tmp/resume-smoke.txt
```

Assertions on `/tmp/resume-smoke.txt`:

1. **Structure:** each of the six section labels is present — grep **each
   label separately** (a single alternation only proves at least one matched):

   ```sh
   for label in 'Task' 'State' 'Decisions already made' 'Single next action' \
                'Verified pointers' 'Staleness'; do
     grep -q "$label" /tmp/resume-smoke.txt || echo "MISSING: $label"
   done
   ```

   (Staleness is present-or-legitimately-omitted: accept its absence only if
   the brief shows no fallback/drift notes.)
2. **Length:** the brief body is ≤ ~400 words (`wc -w`), i.e. the subagent
   distilled rather than dumped.
3. **Pointers verify:** every path the "Verified pointers" section names exists
   (`ls`/`test -e` each), and every command runs `--help`/`--dry` clean.
4. **Degradation exercised:** because `brain-recall` may not be installed yet,
   confirm the run either used the CLI **or** noted the MCP fallback in a
   Staleness warning — both are acceptable passes; a crash is not.

A tighter, model-free variant for CI: skip `claude -p` and run the subagent's
brief steps 1–6 directly as a shell script against `~/src/brain`, asserting
each command exits 0 (or is gracefully skipped) — this proves the pinned
command contract without spending a model call.

## Build queue note

Builds **LAST (third)** in its group. Depends on:

- **recall-cli** (spec'd: `specs/recall-cli.md`) — provides `brain-recall` +
  `--scope summaries` + `--get`, the subagent's primary retrieval path.
- **state-notes-auto-reflect** (spec'd: `specs/state-notes-auto-reflect.md`) —
  provides `memories/<project>-state.md`, the subagent's step-3 read; that
  spec adopts the exact path (its §1 pins it as the resume contract).

Landing this third means both the clean CLI path and the state note exist when
the skill ships. Landed earlier, it runs only the degradation path (MCP tools +
digests/episodes) — functional, so an early land is not wrong, just suboptimal.

## Risks & open questions

**Risks**

- **Primary path not yet shipped.** `brain-recall` and the state note are
  dependencies; until they land, the *degradation* path is the *only* path, so
  the skill must be correct on both. Mitigated: the MCP-tools + digests/episodes
  fallback is spelled out and is the same retrieval the inline protocol uses
  today; building third makes the clean path the default at ship time.
- **Brief bloat defeats the purpose.** If the subagent returns raw recall
  instead of a distilled brief, the main window is polluted anyway. Mitigated:
  the ≤400-word cap + fixed six-field structure + the main session's instruction
  to reject and re-brief an over-length return (lean review discipline).
- **Freshness gap.** Incremental ingest closes the *episode* gap, but digests
  and the state note are only as fresh as the last reflect. The step-5 cross
  check against the real target repo is load-bearing — without it the brief can
  confidently describe a superseded state. Mitigated: "trust the repo, flag the
  drift" is mandatory in the brief.
- **Wrong-project inference.** cwd/topic inference can pick the wrong project.
  Mitigated: ask once when genuinely ambiguous; never guess between two live
  candidates.
- **Stale MCP server on the fallback path.** The Bash CLI avoids the stale
  server, but the MCP fallback inherits it (e.g. an old `get_episode` schema
  after a brain code change). Low-frequency; note it if the fallback misbehaves,
  reconnect `/mcp`.
- **Cross-repo edits.** The build touches three repos/roots (brain, skills,
  and untracked `~/src/CLAUDE.md`). A commit in the wrong repo strands the
  symlink or the doc. Mitigated: the Files-touched table names the repo for
  each.

**Open questions**

1. **Read-only, or write drift back?** When step 5 finds the brain lagging
   reality, should the subagent (or main session) update
   `<project>-state.md`, or stay strictly read-only and leave writes to
   reflect? Spec's stance: **read-only** — reflect owns the state note;
   resume only flags drift. The state-notes-auto-reflect spec (now written)
   does both: it adopts the `memories/<project>-state.md` path verbatim and
   assigns state-note writes exclusively to reflect.
2. **Ambiguous project in headless mode — RESOLVED.** With no user to ask
   (nightly / `claude -p`), `/resume` infers from cwd, proceeds, and puts the
   inference in a Staleness warning so it is auditable. Encoded in the
   SKILL.md §1 text (headless bullet).

## Size estimate (S/M/L and rough LOC)

**S.** One SKILL.md (~70 lines of markdown, most of it the verbatim subagent
brief), one symlink (one `ln -s`), a ~3-line handoff frontmatter de-conflict,
a ~7-line CLAUDE.md replacement, and a ~10-line README section. No Python, no
schema, no new dependencies, no `pyproject.toml` change, no MCP tool surface.
The only "logic" is prose the main session and the subagent follow.

## Adversarial review (verifier)

Verdict: **SOUND WITH FIXES** (all fixes applied in this revision)

Verified against the live system: `~/src/skills/reflect` and `handoff` are
relative directory symlinks (`-> ../brain/skills/<name>`); `~/.claude/skills ->
~/src/skills`; `~/src/skills` and `~/src/brain` are separate git repos; in-repo
skill frontmatter is `name` + `description` only (no `triggers:`);
`pyproject.toml` `[project.scripts]` is exactly brain-ingest / brain-server /
brain-hook / brain-sync / brain-usage-report — no `brain-recall` yet;
`specs/recall-cli.md` exists and pins `brain-recall`,
`--scope all|episodes|memories|summaries`, and `--get <id>` matching this
spec's subagent brief verbatim; `brain-sync pull` and incremental
`brain-ingest` are real entry points; CLAUDE.md § Resuming work is exactly
lines 43–58; README's Reflect-skill section ends at line 154, so the insertion
anchor is valid.

Issues:

1. **RESOLVED:** the symlink command is now the relative form
   (`ln -s ../brain/skills/resume ~/src/skills/resume`) everywhere — §API,
   Files touched, implementation step 2, and the README snippet — matching
   the on-disk precedents and making step 2's `readlink` check pass.

   **Symlink command contradicted its own verify step.** Real precedent links
   are relative (`reflect -> ../brain/skills/reflect`); the spec's absolute
   `ln -s ~/src/brain/skills/resume ...` would make the
   `readlink → ../brain/skills/resume` verification fail. (The README's
   reflect snippet also uses the absolute form — reality diverged from docs.)
2. **RESOLVED (superseded):** `specs/state-notes-auto-reflect.md` has since
   been written and adopts `memories/<project>-state.md` verbatim (reflect
   writes, resume reads); the header, Build queue note, and open question 1
   now reference that spec directly instead of "spec pending".

   **state-notes-auto-reflect was described as "spec'd separately"** but no
   such spec exists in `specs/` and no `memories/*-state.md` exists; open
   question 1's "confirm with that spec" was unactionable as written.
3. **RESOLVED:** Files touched gains a `skills/handoff/SKILL.md` frontmatter
   row and implementation step 3 de-conflicts the triggers — handoff drops
   "resume" / "pick up where we left off" (kept: "hand off", "write a
   handoff"; added: "handoff resume") so `/resume` owns the resume phrasing.

   **Trigger collision with handoff.** handoff's description claims "resume"
   and "pick up where we left off" — identical phrases to the new skill's
   description, making skill selection ambiguous.
4. **RESOLVED:** the smoke plan now greps each of the six labels separately
   (with Staleness allowed to be legitimately omitted), and the README snippet
   uses a 4-backtick outer fence so the nested ```sh block renders.

   **Minor:** (a) `grep -c -E 'Task|State|…'` counted any-label lines rather
   than verifying each of six fields; (b) the README snippet nested ```sh
   inside a ```markdown fence, which breaks CommonMark rendering.
