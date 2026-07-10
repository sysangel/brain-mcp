# SessionStart state-note injection (`brain-session-start`)

## Problem

Resume was command-driven and expensive: /resume spawned an Opus subagent
(~93s wall, ~23k tokens) to hand back a ~400-word brief that reflect had
already written to `memories/<project>-state.md`. Measurement (2026-07-04)
showed ~93% of that wall clock was subagent spawn + LLM inference; total
brain-side work was ~5–7s. Resume is a cache hit, not a computation — the
Hermes-style fix is pre-turn injection of the pre-distilled artifact, not a
faster retrieval pipeline.

## Design

A `SessionStart` hook (matcher `startup|resume|clear`; `compact` deliberately
excluded — the compact summary already carries working state, and a mid-task
re-injection misleads) runs `brain-session-start`, which:

1. Reads the hook JSON on stdin (`cwd`, `source`).
2. Resolves project = basename of `git -C <cwd> rev-parse --show-toplevel`,
   falling back to the cwd basename (covers non-repo dirs like `~/src`).
3. Reads `memories/<project>-state.md`. Missing note → **inject nothing**
   (quiet fallback; never guess another project's note — wrong context is
   worse than none).
4. Emits the note body as `hookSpecificOutput.additionalContext`, framed as
   reference-not-instructions, capped at `SESSION_BLOCK_MAX_CHARS`.
5. Appends a freshness footer and, when warranted, a STALE banner routing to
   `/catchup` (the demoted deep-recovery subagent path, renamed from `resume`
   to un-shadow Claude Code's built-in /resume — skills override built-ins).
6. After flushing stdout, spawns a **detached** `brain-session-refresh`
   (stdio all DEVNULL + `start_new_session=True` — an inherited stdout pipe
   would hold the hook's EOF hostage until the child exits) that freshens the
   index for the NEXT session.

## Freshness model

The note is only as fresh as the last reflect; the hook stamps rather than
guarantees:

- **Note write time**: frontmatter `written:` (full UTC ISO, stamped by
  reflect step 7) > the note's last-commit time in the brain repo (survives
  `git pull`, unlike mtime) > frontmatter `date:` > mtime.
- **Episodes-since**: read-only count of non-sidechain episodes for this
  project's cwd prefix newer than the note (lexical ISO compare, same
  invariant as reflect's SQL). Informative; raises the STALE banner alone
  only at `SESSION_EPISODES_BANNER` (default 10).
- **Repo drift**: project repo HEAD commit time > the LATER of the note's
  `written:` stamp and its commit time, + `SESSION_REPO_DRIFT_SLACK_S`
  (default 120s). Comparing against the commit time is load-bearing for the
  brain project itself: reflect stamps `written:` at step 7 but commits at
  step 10, a multi-minute gap (compact, skill mining, re-ingest) that would
  otherwise false-flag the note's own project after every reflect.

## Refresh path (`brain-session-refresh`)

Mirrors `server._ensure_fresh_index` exactly: `sync.maybe_pull()` (throttled
900s, flock-guarded, ff-only) then incremental ingest gated on the
`last_ingest` meta key vs `INGEST_STALE_SECONDS`. Its own non-blocking flock
(`cache/session-refresh.lock`) dedupes concurrent spawns; a settle sleep
(`SESSION_REFRESH_SETTLE_S`) lets a same-moment MCP-server startup ingest
stamp first, so concurrent execution degrades to a no-op. No autoreflect —
spawning reflect stays the server's job. Net effect: the current session
serves the possibly-slightly-stale note instantly; the next session reads a
freshened index.

## Invariants (shared with hook.py)

- Quiet: no note → no output; every failure → exit 0, empty stdout.
- Fast: blocking path is one stdin read, ≤3 short git calls (each bounded to
  3s so even pathological git hangs stay under the hook's 10s budget), one
  file read, one read-only COUNT — target well under 500ms including `uv run`
  overhead. Truncation eats note body only, never the freshness tail.
- Safe: the hook never writes; only the detached refresh does, through the
  same guarded code paths the server already uses.
- Kill switch: `BRAIN_SESSION_HOOK_DISABLE=1` makes it fully inert (no
  injection, no refresh spawn). Debug: `BRAIN_HOOK_DEBUG=1` (stderr only).

## Context budget

One injection per session start: the ~400-word note ≈ 450–600 tokens, plus
header/footer. The UserPromptSubmit hook (≤300 tokens, usually silent) is
unchanged — it remains the per-turn ambient surface; SessionStart carries the
working brief. Compare ~23k tokens per /resume before.
