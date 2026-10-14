# brain

**A personal memory engine and MCP server: hybrid RAG (SQLite FTS5 + vector
search) over a git-backed markdown store.**

[![CI](https://github.com/OWNER/brain-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/brain-mcp/actions/workflows/ci.yml)
![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

> Replace `OWNER` in the CI badge with your GitHub account once you push this
> to a repo.

## Why

An LLM's context is amnesiac: everything it learns about you, your projects,
and your decisions evaporates when the session ends. `brain` fixes that by
persisting durable facts as plain markdown in git — the kind of store you can
read, edit, `grep`, and diff by hand — and making it recallable to any MCP
client through hybrid search. The markdown is the source of truth; the search
index is a rebuildable cache you can delete at any time.

## Architecture

```
          SOURCE OF TRUTH (markdown + git)          DERIVED INDEX (rebuildable)
        ┌───────────────────────────────────┐      ┌─────────────────────────┐
        │  memories/     curated facts       │      │  cache/brain.db         │
        │  summaries/    session digests     │─────▶│  ── FTS5 (BM25)         │
        │  ~/.claude/…   ingested transcripts│ ingest  ── sqlite-vec (256-d   │
        │                 (episodes)         │      │     nomic-embed vectors)│
        └───────────────────────────────────┘      └───────────┬─────────────┘
                    ▲                                           │
                    │ writes auto-commit                        │ hybrid recall
                    │ (optional push to a private remote)       ▼
        ┌───────────┴───────────┐                   ┌─────────────────────────┐
        │   remember(fact,type) │◀──── MCP ────────▶│  recall(query,k,scope)  │
        │                       │     (stdio)       │  get_episode(id)        │
        └───────────────────────┘                   └─────────────────────────┘
                                                     any MCP client (Claude Code, …)
```

- **Three kinds of memory.** `memories/*.md` are curated, durable facts
  (preferences, runbooks, per-project state notes). `summaries/YYYY-MM/*.md`
  are per-session digests. **Episodes** are ingested Claude Code transcripts
  (read from `~/.claude/projects/**.jsonl`). The first two are git-tracked
  text you own; episodes derive from live transcripts — though on an emitting
  host, ingest also writes each episode as a git-tracked file under
  `episodes/YYYY/MM/`, so a transcript-less machine can index them by
  `git pull` alone (see [Running across two machines](#running-across-two-machines-split-host)).
- **The index is a cache.** `cache/brain.db` holds an FTS5 table and a
  `sqlite-vec` table of 256-dimension [nomic-embed-text-v1.5][nomic] vectors
  (Matryoshka-truncated from 768). It is **always rebuildable** and **never
  committed** — delete it freely and `brain-ingest --full` recreates it.
- **Hybrid recall.** Each query runs both a lexical (FTS5/BM25) and a semantic
  (vector) leg; the two rankings fuse via [reciprocal-rank fusion][rrf], then
  a prior re-weights by **kind** (a curated memory outranks a raw episode at
  equal evidence) and **recency** (exponential decay with a per-kind
  half-life, floored so age never fully erases relevance). Every hit carries a
  `score` in `(0, 1]`. The whole ranking surface is env-tunable — see
  [Configuration](#configuration).
- **Durability & sync.** Memory writes auto-commit, so a fact is safe the
  moment it is written. Point `origin` at *any* private git remote you control
  and the markdown store syncs across machines; the index never leaves the
  box. Sync is **off unless you configure a remote**, and hard-disabled with
  `BRAIN_SYNC=0`.

[nomic]: https://huggingface.co/nomic-ai/nomic-embed-text-v1.5
[rrf]: https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+. This repo ships a
tiny synthetic store under `examples/` so you can try recall without any data
of your own.

```sh
uv sync                                              # install deps into .venv

# Build the index over the shipped examples only.
# BRAIN_CLAUDE_PROJECTS points at an empty dir so no real transcripts are read;
# drop it to also ingest your own ~/.claude/projects transcripts.
BRAIN_DIR=$PWD/examples BRAIN_CLAUDE_PROJECTS=$(mktemp -d) \
  uv run brain-ingest --full

# Hybrid recall from the shell.
BRAIN_DIR=$PWD/examples uv run brain-recall "postgres backup"
```

Expected top hit:

```
scope=all
mem_…  memory  2026-01-12  …  score=0.67…  How the demo acme-webapp Postgres database is backed up each night. …
```

Register the MCP server with Claude Code (or any MCP client that speaks stdio):

```sh
claude mcp add brain -- uv run --directory "$PWD" brain-server
```

> Need a **read-only** server — e.g. behind a gate that requires every exposed
> tool to be auto-allowable? Register `brain-server-readonly` instead; it
> exposes only `recall` + `get_episode`, dropping the `remember` write that
> such a gate would reject.

Write a fact mid-session from the shell (`brain-remember` reads one JSON object
from stdin — only `fact` is required):

```sh
echo '{"fact": "Staging DB resets nightly at 03:00 UTC.", "type": "reference"}' \
  | uv run brain-remember
```

> **First run downloads the embedding model** (nomic-embed-text-v1.5, a few
> hundred MB) to `$FASTEMBED_CACHE_PATH` (default `cache/fastembed/`,
> gitignored). Subsequent runs are instant. Everything runs locally — no API
> key, no external inference calls.

## Isolated empty store for external MCP clients

Use a **new, dedicated directory**, not this package checkout, the example
store, an existing brain checkout, or a symlink to any of them. A fresh store
has no memories, summaries, or episodes to index. The package and its store
are separate paths; do not copy personal brain data into the new store.

```sh
BRAIN_PACKAGE=/absolute/path/to/brain-mcp   # public package checkout
export BRAIN_DIR="$HOME/.local/share/brain-mcp-external"
export BRAIN_CLAUDE_PROJECTS="$BRAIN_DIR/empty-claude-projects"
export BRAIN_INGEST_SOURCE=episodes          # episode files, not transcripts
export BRAIN_SYNC=0                          # never pull/push a git remote
export BRAIN_NO_AUTOREFLECT=1                # no background reflection
mkdir -p "$BRAIN_DIR" "$BRAIN_CLAUDE_PROJECTS"
uv run --directory "$BRAIN_PACKAGE" brain-ingest --full
```

Pass those **same five variables and their persistent absolute directory paths**
through the MCP client's per-server environment; do not use a fresh temporary
path on every launch. **Never omit `BRAIN_CLAUDE_PROJECTS`:** even in episode-file
mode, ingest scans Claude project memory Markdown under the default
`~/.claude/projects` and `~/.claude-work/projects` roots. Set the client's stdio
executable to `uv`, with arguments
`["run", "--directory", "/absolute/path/to/brain-mcp", "brain-server-static-readonly"]`.
Pin **`brain-server-static-readonly`**, the static read-only entrypoint, rather than
`brain-server` (which exposes `remember`) or `brain-server-readonly` (which
still starts background ingest). The static server does not update the index;
run `brain-ingest` explicitly after adding material to the isolated store. Explicitly ingest
only data you place in the isolated store. A synthetic-canary test verifies
that the explicit empty Claude-project override keeps both transcripts and
Claude memories out, while omitting it would index Claude memories.

## Commands

Every entry point is a console script; run it with `uv run <name>`.

| Command                | What it does                                                             |
|------------------------|--------------------------------------------------------------------------|
| `brain-ingest`         | Incrementally index memories, summaries, and episodes — from live transcripts or committed episode files (`--full` rebuilds). |
| `brain-server`         | MCP stdio server exposing `recall`, `get_episode`, `remember`.           |
| `brain-server-readonly`| Read-only MCP stdio server: `recall` + `get_episode` only (no `remember`). |
| `brain-recall`         | Hybrid search / full-text fetch from the shell (no MCP needed).          |
| `brain-remember`       | Write a durable memory from the shell (dedup-guarded, auto-commits).     |
| `brain-sync`           | `status` / `pull` / `push` against the optional private remote.          |
| `brain-usage-report`   | Recall/open telemetry report (feeds the consolidation pass).             |
| `brain-hook`           | `UserPromptSubmit` hook: ambient auto-recall (Claude-Code-specific).     |
| `brain-session-start`  | `SessionStart` hook: injects a project's state note (Claude-Code-specific). |
| `brain-autoreflect`    | Wrapper that triggers a headless consolidation pass when debt accrues.   |
| `brain-freshness-audit`| Refreshes `memories/*-state.md` notes that have fallen behind their project. |

## Memory file format

A memory is YAML frontmatter plus a markdown body (the same shape Claude Code
uses for auto-memory), so it stays readable and hand-editable:

```markdown
---
name: postgres-nightly-backup
description: How the demo acme-webapp Postgres database is backed up each night.
metadata:
  type: reference
date: 2026-01-12
---
The acme-webapp production Postgres runs a nightly logical backup at 02:00 UTC…
```

- `name` — kebab-case slug (defaults to the filename).
- `description` — one line; indexed alongside the body.
- `metadata.type` — one of `user` (preferences), `feedback`, `project`
  (per-project living state notes), `reference` (facts/runbooks).
- `date` — optional ISO date; drives recency decay.

See [`examples/memories/`](examples/memories) for one of each kind, and
[`examples/summaries/`](examples/summaries) for the session-digest format.

## Configuration

All configuration is environment variables with sensible defaults — see
[`src/brain/config.py`](src/brain/config.py) for the full surface. Highlights:

| Variable                              | Default              | Purpose                                        |
|---------------------------------------|----------------------|------------------------------------------------|
| `BRAIN_DIR`                           | the repo root        | Root of the markdown store + `cache/`.         |
| `BRAIN_CLAUDE_PROJECTS`               | `~/.claude/projects` `+ ~/.claude-work/projects` | Colon-separated list of transcript roots (each included only if it exists). |
| `BRAIN_INGEST_SOURCE`                 | `auto`               | Episode source: `auto` / `episodes` / `transcripts` (see [split-host](#running-across-two-machines-split-host)). |
| `BRAIN_EPISODE_EXCLUDE`               | inherits `BRAIN_DIGEST_EXCLUDE` | Comma-separated cwd/project substrings whose episodes get **no** git-tracked `episodes/` file (still indexed in the local cache DB). |
| `FASTEMBED_CACHE_PATH`                | `cache/fastembed/`   | Where the embedding model is cached.           |
| `BRAIN_EMBED_BATCH`                   | `32`                 | Embedder inference batch size; lower it on small-RAM boxes (32 can OOM a 2 GB box). |
| `BRAIN_EMBED_THREADS`                 | `4`                  | onnxruntime intra-op thread cap for the embedder. |
| `BRAIN_EMBED_BACKFILL_CHUNK`          | `128`                | DB paging chunk size for the embedding backfill. |
| `BRAIN_SYNC`                          | on (if remote set)   | `0` hard-disables all git sync.                |
| `BRAIN_NO_AUTOREFLECT`                | unset                | `1` disables the in-process debt-spawn reflect trigger (set on the emitting server + reflect/refresh children). |
| `BRAIN_AUDIT_MAX_REFRESH`             | `3`                  | Max freshness-audit refresh agents per run.    |
| `BRAIN_AUDIT_REFRESH_TIMEOUT`         | `1800`               | Per-refresh-agent timeout (seconds) in the freshness audit. |
| `BRAIN_RRF_K`                         | `60`                 | Reciprocal-rank-fusion damping constant.       |
| `BRAIN_KIND_WEIGHT_{MEMORY,SUMMARY,EPISODE}` | `1.0 / 0.85 / 0.70` | Per-kind rank multipliers.               |
| `BRAIN_HALF_LIFE_{MEMORY,SUMMARY,EPISODE}`   | `180 / 90 / 45` days | Per-kind recency half-lives.             |
| `BRAIN_RECENCY_FLOOR`                 | `0.35`               | Floor the recency factor decays toward.        |

## Optional integrations (Claude Code)

These are conveniences for a Claude Code workflow and are entirely optional —
the core (`ingest` + `recall`/`remember` + MCP server) has no dependency on
them.

- **Ambient auto-recall** — a `UserPromptSubmit` hook (`brain-hook`) that runs
  a fast, read-only FTS pass on every prompt and silently injects the strongest
  matching memories as context.
- **SessionStart injection** — `brain-session-start` injects a project's
  rolling state note when a session opens, stamped with a freshness banner.
- **Skills** — [`skills/reflect`](skills/reflect) (consolidation pass),
  [`skills/catchup`](skills/catchup) (deep resume), and
  [`skills/handoff`](skills/handoff).
- **Reflect triggering** — on a workstation, consolidation is triggered solely
  by the in-process *reflection-debt* spawn from the MCP server (no cron or
  scheduled job); on an always-on box, a systemd timer runs
  `brain-autoreflect --if-debt`. See
  [Running across two machines](#running-across-two-machines-split-host).

Design docs for these live under [`specs/`](specs).

## How multi-machine sync works (optional)

The markdown store can sync across machines through **any private git remote
you control** — there is nothing brain-specific about it:

```sh
git remote add origin <your-private-repo>   # e.g. git@github.com:you/brain.git
git config user.name  "Your Name"           # any identity you like
git config user.email you@example.com
git push -u origin main
```

`cache/brain.db` is rebuildable and never pushed. Sync auto-detects the
remote: with no `origin`, brain behaves exactly as a local-only store, zero
config. `BRAIN_SYNC=0` disables it entirely regardless of remote. `memories/`
and `proposals/` carry a `merge=union` attribute so concurrent edits from two
machines concatenate losslessly rather than conflict; the next consolidation
pass dedups them. See [`specs/git-sync.md`](specs/git-sync.md) for the full
design.

Memory commits record provenance as git trailers (`Session`, `Project`,
`Host`) so you can audit which machine and session produced each fact.

## Running across two machines (split-host)

The store is built to run split across an **emitting workstation** and an
**always-on box** (a small VM), both talking to the same markdown store through
the private remote from
[How multi-machine sync works](#how-multi-machine-sync-works-optional). They
divide the labour:

- **The workstation emits.** It has the live Claude Code transcripts, so its
  `brain-ingest` parses them, writes new memories, and — the key part — emits
  one markdown file per episode under `episodes/YYYY/MM/<id>.md`. Unlike
  `cache/` (gitignored), `episodes/` is **git-tracked and committed** (as
  `brain-ingest: emit N episodes`) and pushed to the remote. The workstation
  runs its MCP server with `BRAIN_NO_AUTOREFLECT=1`, so it never spends a
  reflect itself — it delegates that to the box.
- **The always-on box indexes.** It has no transcripts of its own, so it
  `git pull`s and indexes straight from the committed episode files, then runs
  the maintenance loops on a timer. It sets `BRAIN_INGEST_SOURCE=episodes` so
  ingest reads `episodes/**/*.md` only and never scans, emits, or commits
  transcripts — important because the box accrues its own `claude -p` reflect
  transcripts that `auto` would otherwise wrongly index and re-emit.

### Episode source selection (`BRAIN_INGEST_SOURCE`)

| Value            | Behaviour                                                              |
|------------------|------------------------------------------------------------------------|
| `auto` (default) | If transcripts are present, transcript mode (parse + emit episode files); otherwise, if episode files are present, episode-file mode. |
| `episodes`       | Index `episodes/**/*.md` **only** — never scan, emit, or commit transcripts. |
| `transcripts`    | Force transcript mode.                                                  |

A transcript-less box therefore stays current on episodes purely by `git pull`.
See [`specs/episodes-in-git.md`](specs/episodes-in-git.md).

### reflect triggering

There is no nightly cron or scheduled OS job. reflect is triggered one way per host:

- **On the workstation**, solely by the in-process *reflection-debt* spawn from
  the MCP server: when accrued debt crosses the threshold, the server spawns a
  headless consolidation pass. `BRAIN_NO_AUTOREFLECT=1` disables it (as the
  split-host workstation does).
- **On the always-on box**, by a systemd timer running
  `brain-autoreflect --if-debt` — a debt-gated reflect that only spends a
  `claude -p /reflect` when reflection debt has actually accrued.

### systemd timers (`deploy/vm/`)

The [`deploy/vm/`](deploy/vm) dir ships user-level systemd units (plus a README)
for the always-on box:

- **`brain-reflect.timer`** (hourly, jittered) runs `brain-ingest`, then
  `brain-autoreflect --if-debt`.
- **`brain-freshness-audit.timer`** (daily) runs `brain-ingest`, then
  `brain-freshness-audit`: it sweeps the `memories/*-state.md` project notes,
  reuses the SessionStart **STALE** rule (repo drift *or* enough new episodes
  since the note was written) to find notes that have fallen behind their
  project, and dispatches one Opus refresh agent per stale note to rewrite it
  in-format and auto-land it. Runs are bounded by `BRAIN_AUDIT_MAX_REFRESH`
  (default 3) agents; contradictions, archival candidates, and filename
  mapping-failures are written out as `proposals/needs-user-<date>-<note>.md`
  for a human instead.

## Scope & honesty

- **Single-user personal project.** Extracted from a working personal system
  and published with **fresh git history** — not a hardened multi-tenant
  service.
- **Developed on macOS.** The core is pure Python and portable. The only
  platform-specific pieces are optional deployment glue: the workstation runs
  on macOS, and the always-on box uses Linux user-level systemd units
  ([`deploy/vm/`](deploy/vm)).
- **Local embeddings.** Vectors are computed locally via
  [fastembed](https://github.com/qdrant/fastembed) — no API key and no
  external inference calls. (CI runs FTS-only, since the model download and
  native `sqlite-vec` wheel may be unavailable on the runner; recall degrades
  gracefully to lexical-only when the vector table is absent.)
- **Sync is bring-your-own and off by default.** Nothing leaves your machine
  unless you add a private remote.

## License

[MIT](LICENSE).
