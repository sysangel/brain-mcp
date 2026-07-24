# State-note freshness audit (`brain-freshness-audit`)

## Problem

`memories/<project>-state.md` notes are the resume substrate: the SessionStart
hook injects one at session start, and reflect stamps a STALE banner when the
project has moved past the note. But nothing *acts* on staleness — a note only
gets refreshed when a human happens to reflect on that project. On the always-on
VM there is no such human in the loop. The freshness
auditor is the first resident maintenance loop: it detects stale state notes and
refreshes them autonomously, on a daily timer, without touching the STALE rule's
definition.

## Design

Check-first loop (autonomous-loops-doctrine): deterministic CHECK in script
code, an LLM ACTOR only for the judgment step, a narrow mutation surface
(existing `*-state.md` files + the index + brain git), reversibility gates, and
a bounded/serialized run.

### Check

Sweep `memories/*-state.md`. For each note:

1. **project** = filename with the `-state.md` suffix removed.
2. **repo** = `~/src/<project>` (`BRAIN_DIR.parent/<project>`).
   - present → compute `repo_drift` against the note's written-at.
   - absent → skip the drift component (normal on the VM, which only checks out
     brain + skills). **Not** a needsUser event.
3. **episodes_since** = non-sidechain episodes for the project's cwd prefix newer
   than the note, from the read-only index db. Filesystem-independent (a cwd
   string query), so it works even when the repo isn't checked out.
4. **STALE** iff `repo_drift OR episodes_since >= SESSION_EPISODES_BANNER`.

This is the SessionStart rule *verbatim*: `session_start.parse_note`,
`episodes_since`, and `repo_drift` are imported and reused (promoted to public
names; SessionStart behavior is unchanged), never reimplemented. `is_stale`
applies the same trigger condition `_build_block` uses for the banner.

### Actor

For each stale note (capped, serialized): `claude -p --model opus` in
`BRAIN_DIR` with a tight allowlist (`AUDIT_ALLOWED_TOOLS` = reflect's git/uv/
sqlite allowlist + Read/Write/Edit + `mcp__brain__recall`/`get_episode`). The
prompt tells it to rewrite THAT note in-format — same frontmatter, same section
shape (Task / Where the work is / Decisions / Next actions / Pointers), `date:`
and `written:` restamped — grounded in recall/get_episode evidence and, when the
repo is present, its real state. The agent auto-lands its rewrite (git add +
commit + `brain-sync push`).

Child hygiene mirrors autoreflect: `BRAIN_NO_AUTOREFLECT=1` on the child (its
spawned MCP server never triggers a nested reflect), its own process group
(`start_new_session`), a bounded `wait`, and `kill_group` (reused from
autoreflect) on timeout or abnormal exit so no grandchild survives to mutate
`BRAIN_DIR` after the run moves on.

## Gates (locked spec)

- **Rewrites auto-land** — the agent commits + pushes.
- **Contradiction / archival candidate** — the agent must NOT paper over
  evidence it can't reconcile, or keep refreshing a finished/abandoned project.
  It writes a needsUser proposal instead and leaves the note unchanged.
- **Mapping failure** — a filename that yields no sensible project (empty /
  path-fragment) is caught deterministically by the auditor, which files the
  needsUser proposal itself.

needsUser proposals follow the doctrine ("loops write `proposals/`, a human
promotes"; no needsUser location is named there): `proposals/needs-user-<UTCdate>-<note>.md`,
committed + pushed so a Mac session surfaces them. Re-filing is suppressed while
an unresolved proposal for the same note already exists.

## Serialization & cost

- **Single-flight**: a non-blocking flock on `cache/.freshness-audit.lock`
  (the autoreflect pattern) means overlapping timer fires can't double-audit.
- **Cap**: `BRAIN_AUDIT_MAX_REFRESH` (default 3) refresh agents per run; over-cap
  stale notes are logged and skipped (they get a slot next run).
- **Timeout**: `BRAIN_AUDIT_REFRESH_TIMEOUT` (default 1800s) per agent.
- Agents run **serially** — each lands its commit before the next starts, so
  git index.lock contention on `BRAIN_DIR` can't arise.

## Dry run

`brain-freshness-audit --dry-run` prints the scanned notes, the stale set with
reasons, and the planned actions (refresh / over-cap skip / would-emit
needsUser) — then returns. It takes no lock, writes no file, and spawns nothing;
a safe read-only smoke check against the live repo.

## Deploy

User systemd units (`deploy/vm/brain-freshness-audit.{service,timer}`), daily
`OnCalendar=daily` + `RandomizedDelaySec=1800`, `Persistent=true`, modeled on
`brain-reflect`. The service runs `brain-ingest` (so `episodes_since` reads a
freshly-topped index) then `brain-freshness-audit`, with `BRAIN_INGEST_SOURCE=episodes`
and `EnvironmentFile=%h/.config/claude.env`.
```
