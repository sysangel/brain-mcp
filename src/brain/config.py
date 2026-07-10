"""Paths and constants.

Env overrides:
  BRAIN_DIR              — repo root (default: this repo, resolved from the package location)
  BRAIN_CLAUDE_PROJECTS  — Claude Code projects dir (default: ~/.claude/projects)
  BRAIN_RRF_K            — RRF damping constant (default: 60)
  BRAIN_KIND_WEIGHT_{MEMORY,SUMMARY,EPISODE}  — per-kind rank multipliers
  BRAIN_HALF_LIFE_{MEMORY,SUMMARY,EPISODE}    — per-kind recency half-life (days)
  BRAIN_RECENCY_FLOOR   — minimum recency factor (default: 0.35)
  BRAIN_REFLECT_DEBT_SESSIONS — unconsolidated sessions that trigger reflect (default: 1)
  BRAIN_REFLECT_MIN_AGE_H — hours since last reflect before debt can trigger (default: 20)
  BRAIN_STATE_STALE_DAYS — archive a quiet project's state note after this (default: 30)
  BRAIN_SESSION_MAX_CHARS — SessionStart injection block cap (default: 4000)
  BRAIN_SESSION_EPISODES_BANNER — episodes-since count that raises the STALE banner (default: 10)
  BRAIN_SESSION_DRIFT_SLACK_S — repo-HEAD-newer slack before STALE (default: 120)
  BRAIN_SESSION_REFRESH_SETTLE_S — detached-refresh settle sleep (default: 10)
  BRAIN_SESSION_HOOK_DISABLE — set to 1 to make the SessionStart hook fully inert
  BRAIN_CLAUDE_BIN      — override the `claude` binary path (else PATH + fallback probe)
  BRAIN_NO_AUTOREFLECT  — set to 1 to suppress the startup debt-triggered reflect (recursion guard)
  BRAIN_AUTOREFLECT_DRYRUN — set to 1 to print the reflect command and exit without running it
"""

from __future__ import annotations

import math
import os
from pathlib import Path

BRAIN_DIR = Path(
    os.environ.get("BRAIN_DIR") or Path(__file__).resolve().parents[2]
).expanduser()
CACHE_DIR = BRAIN_DIR / "cache"
DB_PATH = CACHE_DIR / "brain.db"
MEMORIES_DIR = BRAIN_DIR / "memories"
SUMMARIES_DIR = BRAIN_DIR / "summaries"  # reflect-written session digests (git-tracked)

# Privacy denylist for session digests: comma-separated cwd/project substrings.
# Reflect drops any digest candidate whose cwd/project contains one of these,
# so sensitive projects (e.g. personal-finance) are never digested and never enter
# git-tracked summaries/. Consulted by reflect only; ingest ignores it.
BRAIN_DIGEST_EXCLUDE = [
    s.strip() for s in os.environ.get("BRAIN_DIGEST_EXCLUDE", "").split(",") if s.strip()
]

CLAUDE_PROJECTS = Path(
    os.environ.get("BRAIN_CLAUDE_PROJECTS") or "~/.claude/projects"
).expanduser()

# Keep the embedding model in a stable, gitignored location instead of the OS
# temp dir (macOS purges temp -> surprise re-downloads). Env override wins.
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(CACHE_DIR / "fastembed"))

# Embedding
EMBED_MODEL = "nomic-ai/nomic-embed-text-v1.5"
EMBED_DIM = 256  # Matryoshka truncation of the 768-dim model output
EMBED_TEXT_CAP = 2_000  # chars fed to the embedder (full text stays in the DB row)

# Ingest / storage
EPISODE_TEXT_CAP = 20_000  # chars stored per episode
MAX_LINE_BYTES = 200_000  # skip jsonl lines bigger than this without parsing
INGEST_STALE_SECONDS = 3600  # server re-ingests if last ingest older than this

# Usage telemetry (see usagelog.py, usage_report.py, specs/usage-logging.md).
# Non-rebuildable, machine-local, gitignored lossy signal — NOT under cache/'s
# "delete freely" contract. Feeds the nightly reflect pass (brain-usage-report).
LOCAL_DIR = BRAIN_DIR / "local"  # never committed; not derived from the corpus
USAGE_DB_PATH = LOCAL_DIR / "usage.db"
USAGE_RETENTION_DAYS = 90  # windowed DELETE at writer init + report run
USAGE_MAX_HITS_LOGGED = 8  # recall_hits rows kept per recall (pos 0..7)
USAGE_QUEUE_MAX = 1000  # bounded in-process queue; full => drop the event
WEAK_VEC_COS = 0.55  # gap-list threshold: best-hit cosine sim below this = weak
VEC_FLOOR_COS = 0.55  # recall hard filter: drop vec hits below this cosine (distinct role from WEAK_VEC_COS's telemetry gap-list)

# Multi-machine sync (see sync.py, specs/git-sync.md). State lives in files
# under cache/ (never the DB) so sync works while the index is mid-rebuild.
# BRAIN_SYNC=0 hard-disables all sync regardless of remote.
SYNC_PULL_MIN_INTERVAL = 900  # throttle: skip a background pull younger than this (s)
SYNC_GIT_TIMEOUT = 30  # per git-subprocess timeout (s); a dead network fails fast
LAST_SYNC_PATH = CACHE_DIR / "last-sync-pull"  # epoch of last successful pull
SYNC_LOCK_PATH = CACHE_DIR / "sync.lock"  # non-blocking flock: one puller at a time
SYNC_CONFLICT_FLAG = CACHE_DIR / "sync-conflict"  # set on aborted rebase; human resolves

# Recall / output
RETURN_TEXT_CAP = 8_000  # get_episode truncation
SNIPPET_WIDTH = 250

# remember tool: dedup thresholds (doc-vs-doc cosine) + fact size cap.
# 0.90 catches restatements; 0.80 flags the gray band (see remember-tool spec).
DUP_COSINE = 0.90
DUP_SOFT_COSINE = 0.80
FACT_MAX_CHARS = 4_000  # memories are distilled facts, not transcripts


def _env_float(
    name: str,
    default: float,
    *,
    lo: float | None = None,
    hi: float | None = None,
    lo_exclusive: bool = False,
) -> float:
    """Read an env override as a float; fall back to `default` if unset,
    unparseable, non-finite (nan/inf), or outside the [lo, hi] bounds. A bad
    override must never crash recall or config import — a parseable-but-invalid
    value (e.g. a zero half-life or a nan RRF_K) is treated exactly like an
    unset one. `lo_exclusive` requires strictly `> lo` (for half-lives)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    if not math.isfinite(val):
        return default
    if lo is not None and (val <= lo if lo_exclusive else val < lo):
        return default
    if hi is not None and val > hi:
        return default
    return val


# Recall ranking (recency + kind priors over RRF fusion; see ranking.py).
# All query-time arithmetic — no persisted state. Env-overridable so the
# untuned defaults have a single source of truth.
RRF_K = int(_env_float("BRAIN_RRF_K", 60, lo=1))  # finite, >=1; standard = 60

# Per-kind rank multipliers: a memory (curated fact) outranks an episode
# (raw transcript) at equal evidence. "summary" is a reserved slot for the
# upcoming session-summaries feature.
KIND_WEIGHTS = {
    "memory": _env_float("BRAIN_KIND_WEIGHT_MEMORY", 1.00),
    "summary": _env_float("BRAIN_KIND_WEIGHT_SUMMARY", 0.85),
    "episode": _env_float("BRAIN_KIND_WEIGHT_EPISODE", 0.70),
}

# Per-kind recency half-life in days: facts stay true longer than events.
# Must be strictly > 0 (a zero half-life divides by zero in the decay; a
# negative one makes the decay term > 1, so aged items would score above 1.0).
HALF_LIFE_DAYS = {
    "memory": _env_float("BRAIN_HALF_LIFE_MEMORY", 180.0, lo=0, lo_exclusive=True),
    "summary": _env_float("BRAIN_HALF_LIFE_SUMMARY", 90.0, lo=0, lo_exclusive=True),
    "episode": _env_float("BRAIN_HALF_LIFE_EPISODE", 45.0, lo=0, lo_exclusive=True),
}

# Age never erases relevance: recency decays toward this floor, not zero.
# Must lie in [0, 1] to keep the recency factor — and thus score — in (0, 1].
RECENCY_FLOOR = _env_float("BRAIN_RECENCY_FLOOR", 0.35, lo=0, hi=1)

# Hook (ambient auto-recall) — knobs for the brain-hook UserPromptSubmit hook
# (see hook.py and specs/auto-recall-hook.md). Env-overridable; the int knobs
# route through _env_float (then cast) so a bad override falls back to the
# default rather than crashing a synchronous every-prompt hook.
HOOK_MAX_HITS = int(_env_float("BRAIN_HOOK_MAX_HITS", 3, lo=0))  # top-N memories kept
HOOK_MAX_CHARS = int(_env_float("BRAIN_HOOK_MAX_CHARS", 1200, lo=0))  # block truncation
HOOK_MIN_MATCH = int(_env_float("BRAIN_HOOK_MIN_MATCH", 2, lo=1))  # distinct-token gate
HOOK_FTS_LIMIT = int(_env_float("BRAIN_HOOK_FTS_LIMIT", 8, lo=1))  # FTS candidates pulled
HOOK_SEEN_TTL_DAYS = int(_env_float("BRAIN_HOOK_SEEN_TTL_DAYS", 7, lo=0))  # seen-file prune age
HOOK_SEEN_DIR = CACHE_DIR / "hook-seen"  # per-session dedup seen-files

# SessionStart hook (see session_start.py and specs/session-start-hook.md).
# Injects memories/<project>-state.md at session start; the refresh knobs
# shape the detached next-session freshener it spawns.
SESSION_BLOCK_MAX_CHARS = int(_env_float("BRAIN_SESSION_MAX_CHARS", 4000, lo=0))  # whole-block cap
SESSION_EPISODES_BANNER = int(_env_float("BRAIN_SESSION_EPISODES_BANNER", 10, lo=1))  # episodes-since that raises STALE
SESSION_REPO_DRIFT_SLACK_S = int(_env_float("BRAIN_SESSION_DRIFT_SLACK_S", 120, lo=0))  # absorbs reflect's write->commit gap
SESSION_REFRESH_SETTLE_S = int(_env_float("BRAIN_SESSION_REFRESH_SETTLE_S", 10, lo=0))  # let server startup ingest win the race
SESSION_REFRESH_LOCK_PATH = CACHE_DIR / "session-refresh.lock"  # non-blocking flock: one refresh at a time

# Debt-triggered reflect (see autoreflect.py and specs/state-notes-auto-reflect.md).
# The server's startup ingest thread spawns a detached reflect when enough
# unconsolidated sessions have accumulated AND enough time has passed since the
# last reflect. The int knobs route through _env_float (then cast) so a bad
# override falls back to the default rather than crashing the daemon thread.
REFLECT_DEBT_SESSIONS = int(_env_float("BRAIN_REFLECT_DEBT_SESSIONS", 1, lo=1))  # sessions to trigger
REFLECT_MIN_AGE_H = _env_float("BRAIN_REFLECT_MIN_AGE_H", 20.0, lo=0)  # hours since last reflect
STATE_STALE_DAYS = int(_env_float("BRAIN_STATE_STALE_DAYS", 30, lo=1))  # archive horizon for quiet notes

# cache/-local (per-machine, never synced): reflection debt is inherently local.
# LAST_REFLECT_PATH is the single source of truth for the marker the skill and
# the Python side share (the skill previously hardcoded the path).
LAST_REFLECT_PATH = CACHE_DIR / "last-reflect"  # single-line ISO-8601 UTC-Z; written by reflect
REFLECT_LOCK_PATH = CACHE_DIR / ".reflect.lock"  # non-blocking flock: one reflect at a time
AUTOREFLECT_LOG = CACHE_DIR / "autoreflect.log"  # detached-spawn stdout/stderr
