---
name: catchup
description: Deep-recovery resume — rebuild a project's working state from the
  brain when the automatic SessionStart brief is missing, flagged STALE, or
  contradicts the repo. Spawns one subagent that syncs, refreshes the index,
  reads the state note, cross-checks against the real repo, and returns a short
  distilled brief. Use when asked to "catch up", "/catchup", "deep resume",
  "reconstruct state", or when the injected state note carries a STALE banner.
---

# catchup

Reconstruct a project's working state from the brain and hand the main session
one distilled brief to act on — without spending the main window on the
retrieval.

This is the DEEP path. The routine path is automatic: the `brain-session-start`
SessionStart hook injects `memories/<project>-state.md` into context at session
start (startup / --resume / post-/clear), stamped with freshness. Reach for
/catchup only when that brief is missing (no state note yet), carries the STALE
banner, or visibly disagrees with the repo — its subagent re-derives state from
the ground up (sync, ingest, recall, git cross-check), which the injection
deliberately does not do.

All the expensive legwork (sync, ingest, state-note read, recall, freshness
cross-check, pointer verification) happens inside ONE subagent; the main window
only holds the brief. This is the /lean discipline applied to resuming: hold
the brief, never the raw recall.

## 1. Resolve the project

- Use the `/catchup <project>` argument if given.
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

- `brain-recall` not installed → the subagent uses the MCP brain tools
  (`recall`, `get_episode`) and notes it.
- `<project>-state.md` missing (no note for this project yet) → fall back to
  digests + episodes; note it.
- Offline / no remote → skip the pull, brief from the local index, flag "brain
  not synced".
