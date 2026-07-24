# Episodes-in-git transport (`episodes/`)

## Problem

Episodes are the one corpus that is NOT in git. Memories, summaries, and state
notes are markdown under `~/src/brain` and sync over the private remote;
episodes are parsed from Claude Code transcripts that live only on the machine
that produced them. An always-on Linux VM has no Mac transcripts, so `git pull`
gives it every memory/summary but zero episodes — the raw event layer recall
leans on is blind there. cache/brain.db can't fix this: it is gitignored and
per-machine by contract.

## Design (locked)

The Mac-side ingest additionally emits one file per episode into a git-tracked
tree; the VM's ingest indexes those files instead of transcripts it doesn't
have. The markdown+git source-of-truth rule now covers episodes too.

- **Format.** One file per episode at `episodes/YYYY/MM/<stable_id>.md`, sharded
  from the episode `ts`. YAML frontmatter (`stable_id, content_hash, session_id,
  uuid, ts, cwd, git_branch, is_sidechain`) + a blank line + the exact
  `episode_text()` body + a trailing newline. Scalars are JSON-encoded, so a
  value can never contain a physical newline and break the `\n---\n` fence scan;
  the render is deterministic, so re-emitting an unchanged episode is
  byte-identical. Filename = stable_id, so a re-emit overwrites in place.

- **Source selection** (`_run_ingest_locked`). A machine runs in exactly one
  mode, chosen by `BRAIN_INGEST_SOURCE` (config):
  - `auto` (default) — infer: transcripts present → transcript mode (parse AND
    emit); else `episodes/` non-empty → episode-file mode.
  - `episodes` — force episode-file mode. Transcripts are NOT scanned, indexed,
    emitted, or committed. This is mandatory on the VM: it accrues its own
    `claude -p` reflect transcripts under `~/.claude/projects`, so `auto` would
    flip it into transcript mode — indexing those, ignoring the pulled episode
    files, and (worst) emitting+pushing its own episodes, breaking the
    Mac-is-sole-writer rule.
  - `transcripts` — force transcript mode (parse + emit as usual).

  An unknown value is rejected LOUDLY at the very start of the run (never a
  silent fallback to `auto` — that would reintroduce the VM bug invisibly).
  Episode-file mode parses `episodes/**/*.md` into episode rows (`source_file` =
  the .md path), reuses the existing files-table / `_file_changed` incremental
  logic — a `git pull` bumps the .md mtimes, which is exactly how the scan
  notices new content — and NEVER emits or commits (the VM is read-only on
  `episodes/`). Switching source on an existing DB is safe without a rebuild: the
  now-absent other-source rows fall out of `current_paths` and the normal prune
  loop removes them on the next run (a `--full` rebuild also works but is not
  required).

- **Emit (Mac, incremental).** The transcript loop accumulates the stable_ids
  its upsert actually inserted/updated (the `row is not None` branch). After the
  loop, `emit_episode_file(row)` writes those (or, on `--full`, every episode via
  the same sweep as backfill). An unchanged on-disk file is left untouched (no
  mtime churn, no spurious diff).

- **Privacy gate.** `BRAIN_EPISODE_EXCLUDE` (config) defaults to
  `BRAIN_DIGEST_EXCLUDE` when the env var is unset, so a project kept out of
  git-tracked `summaries/` is also kept out of `episodes/`. Matched like the
  digest denylist: substring of the episode `cwd`. Excluded episodes stay in the
  local index exactly as today — they just never get an `episodes/` file.

- **Verify on parse.** `parse_episode_file` reconstructs the body byte-exactly;
  the episode-file loop recomputes `content_hash` from the parsed fields and
  compares it against the frontmatter. A mismatch (tamper/corruption) skips that
  file with a log line — a bad file can't poison the index.

- **Commit / push.** `remember.commit_paths(paths, message, trailers)` is the
  generalized add + commit + index.lock-retry that both `remember` and the new
  `commit_episode_files(paths)` share. The episode commit
  (`brain-ingest: emit N episodes`, Host trailer) runs INSIDE the ingest lock;
  `sync.push()` runs from `run_ingest` AFTER the lock releases (mirror
  remember.py — never push under a lock). Nothing staged → clean no-op, no push.
  All logging goes through the `log` callable (stdout belongs to MCP).

- **Deletion policy.** Append-mostly: an episode file is never deleted because
  its source transcript aged off disk. The git file is the durable record.

- **Prune guards.** The zero-transcript guard fires in transcript mode when the
  index tracks `*.jsonl` but the scan found none — the Mac emits into `episodes/`
  too, so a Mac whose scan root went missing must abort here rather than silently
  fall through. It is SKIPPED in explicit `episodes` mode (it is about transcript
  roots we deliberately do not scan; stale `*.jsonl` rows there are cleaned up by
  the ordinary prune loop). A second guard covers the VM: no transcripts AND no
  episode files, while the index tracks episode-file sources, is a wiped/empty
  checkout — abort before the prune. A fresh (empty-index) machine passes both.

- **Backfill.** `brain-ingest --emit-episodes` sweeps every indexed episode
  through the emit path (honoring the gate), one `git add episodes/` + single
  commit, then push. Idempotent. `--full` in transcript mode re-exports via the
  same sweep after the rebuild.

## Deployment

- **Mac** (transcript producer): leave `BRAIN_INGEST_SOURCE` unset (`auto`) —
  transcripts are present, so it parses + emits + pushes as the sole writer.
- **VM** (always-on, transcript-less consumer): set `BRAIN_INGEST_SOURCE=episodes`
  in its brain environment. It then indexes only the `episodes/**/*.md` files it
  pulls, ignores any `claude -p` reflect transcripts it accrues locally, and
  never emits/commits/pushes episodes. Do NOT rely on `auto` there — a single
  local transcript would flip it into transcript mode and make it a second
  writer.

## Invariants

- **One mode per machine.** `BRAIN_INGEST_SOURCE` pins the source (or `auto`
  infers from disjoint tracked-file sets); the guards make a degenerate empty
  scan abort loudly instead of pruning.
- **Idempotent + byte-stable.** Re-emit rewrites nothing when unchanged;
  frontmatter round-trips exactly; `content_hash` is the tamper check on the VM.
- **Privacy honored both ways.** Excluded projects reach neither `summaries/` nor
  `episodes/`, so nothing about them ever syncs — but they stay locally
  recallable as raw episodes.
- **`episodes/` is git-tracked.** `.gitignore` covers `cache/`, `local/`, `*.db`
  only; `episodes/` is committed, unlike the derived index.
- **Push never under a lock.** Commit inside the ingest lock, push after it
  closes; a slow network never serializes writers, and a clean tree never pushes.
