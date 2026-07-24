"""Debt-triggered reflect: startup debt check + fire-and-forget wrapper.

Two entry points funnel through this module:

- `maybe_trigger(log)` runs inside the server's background ingest daemon thread
  (server.py:_ensure_fresh_index), AFTER the incremental ingest so the debt
  count reads a freshly-topped index. When enough unconsolidated sessions have
  accumulated (`REFLECT_DEBT_SESSIONS`) and enough time has passed since the last
  reflect (`REFLECT_MIN_AGE_H`), it spawns this module's `main` DETACHED via
  `sys.executable -m brain.autoreflect` — no PATH lookup, survives the session
  that spawned it. It never blocks, delays, or breaks MCP `initialize`.
- `main()` is the console script `brain-autoreflect`: the single choke point that
  actually runs `claude -p /reflect`. The debt spawn is the sole trigger (the
  nightly launchd backstop was retired 2026-07-12); it and any manual
  `brain-autoreflect` invocation route through here, so one file lock
  (`REFLECT_LOCK_PATH`) gives single-flight across them.

Recursion guard: `main` sets `BRAIN_NO_AUTOREFLECT=1` on the `claude` child, which
spawns its own MCP brain-server; that server inherits the env and its own
`maybe_trigger` returns immediately — so a reflect can never trigger a reflect.
"""

from __future__ import annotations

import fcntl
import math
import os
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .config import (
    AUTOREFLECT_LOG,
    BRAIN_DIR,
    DB_PATH,
    LAST_REFLECT_PATH,
    REFLECT_DEBT_SESSIONS,
    REFLECT_LOCK_PATH,
    REFLECT_MIN_AGE_H,
)

# Reflect's tool allowlist — the single source of truth (was inlined in the
# now-retired launchd plist). The debt spawn (the sole reflect trigger) uses it.
REFLECT_ALLOWED_TOOLS = [
    "Bash(uv:*)",
    "Bash(sqlite3:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(git log:*)",
    "Bash(git status:*)",
    "Bash(git diff:*)",
    "Bash(git mv:*)",
    "Bash(git rm:*)",
    # Models habitually prepend `-C ~/src/brain` even though the session cwd
    # is already BRAIN_DIR; without this entry every such git call is gated
    # and a headless reflect finishes with its commit left pending.
    "Bash(git -C:*)",
    "Bash(hostname:*)",
]

# Ordered fallback probe for the claude binary when a daemon/launchd PATH lacks
# it. Matches the live install (`claude` -> ~/.local/bin/claude, a symlink into
# ~/.local/share/claude/versions/…), so the first entry is the hot path.
_CLAUDE_FALLBACKS = [
    Path.home() / ".local" / "bin" / "claude",
    Path.home() / ".claude" / "local" / "claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
]


def resolve_claude() -> str | None:
    """BRAIN_CLAUDE_BIN override -> `shutil.which("claude")` -> ordered fallback
    probe (first existing executable) -> None. Daemon/launchd PATH may lack
    claude, so `which` alone is not enough."""
    override = os.environ.get("BRAIN_CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for cand in _CLAUDE_FALLBACKS:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _last_reflect() -> datetime | None:
    """Parse `cache/last-reflect` (a single-line ISO-8601 UTC-`Z` timestamp) into
    an aware UTC datetime, or None if the file is missing / empty / unparseable."""
    try:
        raw = LAST_REFLECT_PATH.read_text().strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso_z(dt: datetime) -> str:
    """Serialize back to the stored `cache/last-reflect` form (`…Z`, no fractional
    seconds) so the debt query's lexical `ts > ?` compares like against like."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _debt(conn, last: datetime | None) -> int:
    """Distinct non-sidechain sessions with episodes newer than `last`. The bind
    is the *stored* last-reflect ISO string, compared lexically against episode
    `ts` (both ISO-8601 UTC-`Z`) — a plain string `>`, NOT the digest query's
    `strftime` dance (see spec §3). A None `last` (missing/bad marker) counts
    from the epoch (`ts > ''` is always true) -> exactly one catch-up reflect."""
    since = _iso_z(last) if last is not None else ""
    row = conn.execute(
        "SELECT count(DISTINCT session_id) FROM episodes "
        "WHERE is_sidechain = 0 AND session_id IS NOT NULL AND ts > ?",
        (since,),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def maybe_trigger(log) -> None:
    """Startup reflection-debt check — runs in the server's ingest daemon thread
    AFTER ingest. Spawns the detached wrapper when debt and age both clear. Never
    blocks the server; any failure logs one line and is swallowed."""
    try:
        if os.environ.get("BRAIN_NO_AUTOREFLECT") == "1":  # (b) recursion guard
            return
        if not DB_PATH.exists():
            return
        last = _last_reflect()
        now = datetime.now(timezone.utc)
        age_h = float("inf") if last is None else (now - last).total_seconds() / 3600
        if age_h < REFLECT_MIN_AGE_H:
            return
        conn = db.connect_ro()
        if conn is None:  # DB vanished between the exists() check and here
            return
        try:
            debt = _debt(conn, last)
        finally:
            conn.close()
        if debt < REFLECT_DEBT_SESSIONS:
            return
        claude = resolve_claude()
        if claude is None:
            log("[brain] autoreflect: claude binary not found; skipping")
            return
        # (a) single-flight + fire-and-forget: the wrapper owns the flock and
        # outlives this server, so we spawn it DETACHED and do not wait.
        AUTOREFLECT_LOG.parent.mkdir(parents=True, exist_ok=True)
        logfh = open(AUTOREFLECT_LOG, "a")
        try:
            subprocess.Popen(
                [sys.executable, "-m", "brain.autoreflect"],  # same venv python — no PATH lookup
                stdin=subprocess.DEVNULL,
                stdout=logfh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # setsid: survives this session ending
                close_fds=True,
            )
        finally:
            logfh.close()  # the child dup'd the fd; our copy is done
        log(f"[brain] autoreflect: debt={debt} age={age_h:.1f}h — spawned reflect")
    except Exception as e:  # never break the server on autoreflect problems
        log(f"[brain] autoreflect: skipped ({e})")


def main() -> int:
    """Console script `brain-autoreflect`. Acquires the single-flight flock, then
    (unless `--force`) self-guards against a too-recent reflect, resolves the
    claude binary, and runs `claude -p /reflect`. `--if-debt` additionally
    requires the reflection debt to clear `REFLECT_DEBT_SESSIONS` — the same
    gate `maybe_trigger` applies before spawning — so a dumb timer can invoke
    this frequently and only pay for a reflect when one is owed.
    `BRAIN_AUTOREFLECT_DRYRUN=1` prints the assembled command and exits without
    spawning claude."""
    force = "--force" in sys.argv
    if_debt = "--if-debt" in sys.argv
    REFLECT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(REFLECT_LOCK_PATH, "w")  # held for this process's whole lifetime
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # (a) single-flight
    except OSError:
        print("[autoreflect] a reflect is already running — skipping")
        return 0
    if not force:  # self-guard: skip if a recent reflect already ran
        last = _last_reflect()
        if last is not None:
            age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if age_h < REFLECT_MIN_AGE_H:
                print("[autoreflect] last reflect too recent — skipping")
                return 0
    if if_debt:  # timer-friendly gate: reflect only when debt is owed
        if not DB_PATH.exists():
            print("[autoreflect] --if-debt: no index db — skipping")
            return 0
        conn = db.connect_ro()
        if conn is None:
            print("[autoreflect] --if-debt: index unreadable — skipping")
            return 0
        try:
            debt = _debt(conn, _last_reflect())
        finally:
            conn.close()
        if debt < REFLECT_DEBT_SESSIONS:
            print(f"[autoreflect] --if-debt: debt={debt} < {REFLECT_DEBT_SESSIONS} — skipping")
            return 0
        print(f"[autoreflect] --if-debt: debt={debt} — reflecting")
    claude = resolve_claude()
    if claude is None:
        print("[autoreflect] claude binary not found", file=sys.stderr)
        return 1
    cmd = [
        claude, "-p", "/reflect",
        "--model", "opus",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *REFLECT_ALLOWED_TOOLS,
    ]
    if os.environ.get("BRAIN_AUTOREFLECT_DRYRUN") == "1":  # smoke knob — never burns a run
        print("[autoreflect] DRYRUN cmd:", " ".join(cmd))
        return 0
    env = {**os.environ, "BRAIN_NO_AUTOREFLECT": "1"}  # (b) recursion guard on the child
    # (c) bounded child: a hung `claude` would otherwise hold the flock forever and
    # wedge every future auto-reflect. We run it in its OWN process group
    # (start_new_session) so that on timeout we can signal the whole tree — the
    # `claude` child AND its grandchildren (spawned MCP brain-server, tool
    # children mid `git commit`). Killing only the direct child would leave
    # grandchildren alive to keep mutating BRAIN_DIR after the flock releases and
    # race the next reflect.
    try:
        timeout_s = float(os.environ.get("BRAIN_REFLECT_TIMEOUT", "1800"))
        # nan/inf would silently disable the timeout; <= 0 would kill instantly.
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            timeout_s = 1800.0
    except ValueError:
        timeout_s = 1800.0
    proc = subprocess.Popen(cmd, cwd=str(BRAIN_DIR), env=env, start_new_session=True)
    try:
        return proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        print(
            f"[autoreflect] reflect exceeded {timeout_s:.0f}s — killing process group, giving up",
            file=sys.stderr,
        )
        kill_group(proc)
        return 1
    except BaseException:
        # Any OTHER abnormal exit (KeyboardInterrupt, SystemExit, an unexpected
        # error) must not leave the detached group running after the flock
        # releases to race the next reflect — mirror subprocess.run's
        # kill-on-exception. Normal completion returns above and kills nothing.
        kill_group(proc)
        raise
    # flock auto-releases when fh closes / this process dies (clean OR crash) —
    # no TTL, no stale-lock reclaim (matches sync.py:maybe_pull doctrine).


def kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's whole process group, give it a few seconds to drain,
    then SIGKILL any survivors. Guards against the group already being gone
    (ProcessLookupError) at every step."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
    # Always SIGKILL the group even when the wait returned: proc.wait only
    # confirms the DIRECT child died — a SIGTERM-trapping grandchild would
    # otherwise survive. ProcessLookupError means the group is already gone.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    sys.exit(main())
