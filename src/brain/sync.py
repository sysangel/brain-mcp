"""Multi-machine memory sync over a private git remote (see specs/git-sync.md).

This module owns ALL git-remote interaction. Everything else — ingest, the
server, the reflect skill, the remember tool — calls into it at four points:
a pull before ingest (throttled, background), a pull+drain before reflect, a
push after the reflect commit, and a push exposed for the remember tool.

Invariants:
- Sync NEVER blocks or breaks ingest/serve. Offline is the default assumption.
  Every git subprocess runs with GIT_TERMINAL_PROMPT=0, an ssh BatchMode +
  ConnectTimeout=10 command, and a 30s timeout, so a dead network fails fast
  and logs one grep-able line instead of hanging or prompting.
- No force-push, ever. `push()` runs exactly `git push origin main`; the drain
  inside `pull()` is a bare `git push` with no recovery. No code path
  constructs --force / --force-with-lease.
- Only the explicit foreground path (`brain-sync pull`, reflect step 1) may
  rebase (allow_rebase=True; see main() below). Background pulls (server/ingest
  daemon) are ff-only and never push, so a daemon thread can never rewrite local
  unpushed SHAs or move `main` under a concurrent interactive git session.
  Caveat: "foreground" here means the pull path, NOT necessarily human-attended.
  A headless auto-reflect (autoreflect.py -> `claude -p /reflect` -> reflect
  step 1's `brain-sync pull`) runs this same rebasing path unattended. That is
  intentional (the reflect flock single-flights it), but it means an unattended
  rebase of local memory commits can still occur along the auto-reflect path.
- Sync never runs `git add` — it only moves commits made by reflect/remember.
  cache/ is gitignored, so nothing derived can ever reach the remote.
"""

from __future__ import annotations

import datetime
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import (
    BRAIN_DIR,
    CACHE_DIR,
    LAST_SYNC_PATH,
    SYNC_CONFLICT_FLAG,
    SYNC_GIT_TIMEOUT,
    SYNC_LOCK_PATH,
    SYNC_PULL_MIN_INTERVAL,
)

_enabled: bool | None = None  # cached per process


# --------------------------------------------------------------- primitives ---

def _git_env() -> dict:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
    return env


def _git(args: list[str]) -> subprocess.CompletedProcess:
    """Run a git subprocess in the brain repo, timeout-bounded, never prompting.
    May raise subprocess.TimeoutExpired (caught by pull/push and degraded)."""
    return subprocess.run(
        ["git", "-C", str(BRAIN_DIR), *args],
        env=_git_env(),
        timeout=SYNC_GIT_TIMEOUT,
        capture_output=True,
        text=True,
    )


def _emit(log, msg: str) -> None:
    """One grep-able sync line. Routes through the caller's logger when given
    (so it lands in the ingest/reflect stream), else straight to stderr."""
    (log or (lambda s: print(s, file=sys.stderr)))(f"[brain-sync] {msg}")


def _oneline(s: str) -> str:
    return " ".join((s or "").split())[:200]


def _read_epoch(path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _stamp(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(time.time()))


def _rev(ref: str) -> str | None:
    r = _git(["rev-parse", "--verify", "--quiet", ref])
    return r.stdout.strip() or None


def _remote_url() -> str | None:
    r = _git(["remote", "get-url", "origin"])
    return r.stdout.strip() or None if r.returncode == 0 else None


def _has_dirty_tracked() -> bool:
    # --untracked-files=no is load-bearing: plain --porcelain also lists
    # untracked (??) entries, which would wedge every pull into ff-only/skip.
    r = _git(["status", "--porcelain", "--untracked-files=no"])
    return bool(r.stdout.strip())


def _ahead() -> bool:
    r = _git(["rev-list", "--count", "origin/main..HEAD"])
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def _conflicted_paths() -> list[str]:
    r = _git(["diff", "--name-only", "--diff-filter=U"])
    return [p for p in r.stdout.splitlines() if p.strip()]


def _rebase_in_progress() -> bool:
    """True when a rebase (ours or the user's own, mid-flight) is unfinished.
    Load-bearing: we must never `git rebase --abort` a rebase we did not start —
    that would roll back the user's in-progress work."""
    r = _git(["rev-parse", "--absolute-git-dir"])
    git_dir = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else BRAIN_DIR / ".git"
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _is_non_ff(stderr: str) -> bool:
    s = stderr or ""
    return "rejected" in s and ("non-fast-forward" in s or "fetch first" in s)


def _write_conflict_flag(paths: list[str], local: str | None, remote: str | None) -> None:
    SYNC_CONFLICT_FLAG.parent.mkdir(parents=True, exist_ok=True)
    SYNC_CONFLICT_FLAG.write_text(
        "sync conflict — resolve with ordinary git, then `brain-sync pull`.\n"
        "conflicting paths:\n"
        + "".join(f"  {p}\n" for p in paths)
        + f"local:  {local}\nremote: {remote}\n"
    )


# ------------------------------------------------------------------- public ---

def enabled() -> bool:
    """True when BRAIN_SYNC != "0" and an `origin` remote is configured.
    Cached per process — a clone with no remote behaves exactly as today."""
    global _enabled
    if _enabled is None:
        if os.environ.get("BRAIN_SYNC") == "0":
            _enabled = False
        else:
            _enabled = _remote_url() is not None
    return _enabled


def _pull_succeeded(log, drain: bool) -> None:
    """Post-success bookkeeping: clear any conflict flag, stamp the throttle
    marker, and (only on the explicit path) drain a bare push if ahead."""
    if SYNC_CONFLICT_FLAG.exists():
        SYNC_CONFLICT_FLAG.unlink()
    _stamp(LAST_SYNC_PATH)
    if drain and _ahead():
        # Bare push with NO non-ff recovery — this is what breaks the
        # push -> pull -> drain-push -> pull ... recursion. A rejected drain
        # just waits for the next cycle.
        r = _git(["push", "origin", "main"])
        if r.returncode != 0:
            _emit(log, f"drain push rejected: {_oneline(r.stderr)} — will retry next cycle")


def pull(log=None, drain: bool = True, allow_rebase: bool = True) -> bool:
    """Fetch and integrate origin/main. ff-only first; rebase fallback only when
    `allow_rebase` (the explicit foreground path). On success clears the conflict
    flag, stamps the throttle marker, and — when `drain` and ahead — bare-pushes.
    Returns True on success/no-op, False on any degraded outcome. Never raises."""
    if not enabled():
        return True
    try:
        fetch = _git(["fetch", "origin"])
        if fetch.returncode != 0:
            _emit(log, f"pull failed (offline?): fetch: {_oneline(fetch.stderr)} — continuing local-only")
            return False

        local, remote = _rev("HEAD"), _rev("origin/main")
        if remote is None:
            _emit(log, "pull failed (offline?): no origin/main — continuing local-only")
            return False

        # A rebase already in progress (the user's own, mid-flight) is
        # untouchable: we must not merge, rebase, abort, or push under a detached
        # rebase HEAD — any of those would corrupt or discard the user's work.
        # Skip entirely; a later cycle retries once the human finishes it
        # (Design §1: "just another logged, degraded pull"). Applies to every
        # caller, background or explicit.
        if _rebase_in_progress():
            _emit(log, "pull skipped: a rebase is already in progress — continuing local-only")
            return False

        if local == remote:
            _pull_succeeded(log, drain)  # already up to date; still may drain
            return True

        dirty = _has_dirty_tracked()
        ff = _git(["merge", "--ff-only", "origin/main"])
        if ff.returncode == 0:
            # ff-only also succeeds as a no-op when local is strictly ahead
            # ("Already up to date"); only claim a fast-forward if HEAD moved.
            moved = _rev("HEAD") != local
            _emit(log, "fast-forwarded to origin/main" if moved else "already up to date (local ahead)")
            _pull_succeeded(log, drain)
            return True

        # ff-only failed: diverged (non-ff), or an ff blocked by dirty files.
        if dirty:
            # A dirty tree is never rebased or autostashed — mid-edit
            # reflect/remember state must not be juggled. Retry next cycle.
            _emit(log, "pull skipped: dirty tree — retry next cycle")
            return False
        if not allow_rebase:
            _emit(log, "non-ff pull skipped (background ff-only) — rebases on explicit brain-sync pull")
            return False

        rb = _git(["rebase", "origin/main"])
        if rb.returncode == 0:
            _emit(log, "rebased onto origin/main")
            _pull_succeeded(log, drain)
            return True

        # Our rebase did not finish. Distinguish a REAL stopped-on-conflict
        # rebase from one that never started (index.lock held by a concurrent
        # git; the pre-check above already excluded a pre-existing rebase). Only
        # unmerged (U) paths prove a content conflict — and memories/proposals
        # auto-resolve via merge=union, so anything left is a non-union path
        # (src/, skills/, README, ...): abort and flag, never auto-resolve code
        # headless. Nothing is lost — local stays on main, remote on origin/main.
        conflicts = _conflicted_paths()
        if conflicts:
            _git(["rebase", "--abort"])
            _write_conflict_flag(conflicts, local, remote)
            _emit(
                log,
                f"rebase conflict on {', '.join(conflicts)} — aborted, wrote "
                f"{SYNC_CONFLICT_FLAG.name}; resolve manually",
            )
            return False
        # No conflict: the rebase either never started, or (only ours by now)
        # stopped for a non-conflict reason. Abort only our own mid-flight rebase
        # so the tree is never left wedged; otherwise there is nothing to abort.
        # Degrade like any other failure — no persistent conflict flag.
        if _rebase_in_progress():
            _git(["rebase", "--abort"])
        _emit(
            log,
            f"non-ff pull skipped: rebase did not complete ({_oneline(rb.stderr)}) "
            "— continuing local-only",
        )
        return False
    except subprocess.TimeoutExpired:
        _emit(log, "pull failed (offline?): git timed out — continuing local-only")
        return False
    except Exception as e:  # exception-proof: never break a caller
        _emit(log, f"pull failed (offline?): {e} — continuing local-only")
        return False


def maybe_pull(log=None) -> None:
    """Throttled, flock-guarded, exception-proof background pull for the ingest
    entrypoint and the server's _ensure_fresh_index. ff-only, never pushes,
    never rebases. Skips when a conflict flag is set, when a recent pull already
    ran (<900s), or when another process holds the lock."""
    try:
        if not enabled():
            return
        if SYNC_CONFLICT_FLAG.exists():
            return  # a human must resolve; don't thrash
        last = _read_epoch(LAST_SYNC_PATH)
        if last is not None and (time.time() - last) < SYNC_PULL_MIN_INTERVAL:
            return
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SYNC_LOCK_PATH, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return  # another process is already pulling; skip silently
            pull(log, drain=False, allow_rebase=False)
    except Exception as e:  # never block ingest/serve on sync
        _emit(log, f"pull failed (offline?): {e} — continuing local-only")


def push(log=None) -> bool:
    """`git push origin main`. On a non-ff rejection, recover with a
    non-draining, ff-only pull (never rebase — unattended recovery must not
    rewrite history) and retry the push exactly once; a diverged remote leaves
    the retry rejected -> log and degrade to local-only. Returns True on
    success/no-op. Never raises."""
    if not enabled():
        return True
    try:
        r = _git(["push", "origin", "main"])
        if r.returncode == 0:
            _emit(log, "pushed origin/main")
            return True
        if not _is_non_ff(r.stderr):
            _emit(log, f"push failed (offline?): {_oneline(r.stderr)} — continuing local-only")
            return False
        # Non-ff rejection: the recovery pull passes drain=False so it can never
        # drain-push back into push() (no push<->pull recursion), and
        # allow_rebase=False so an unattended push recovery is ff-only — a
        # diverged remote degrades to local-only rather than silently rebasing
        # origin/main under the user. The retry then just no-ops.
        _emit(log, "push rejected (non-ff): pulling then retrying once")
        pull(log, drain=False, allow_rebase=False)
        r2 = _git(["push", "origin", "main"])
        if r2.returncode == 0:
            _emit(log, "pushed origin/main (after pull)")
            return True
        _emit(log, f"push still rejected: {_oneline(r2.stderr)} — continuing local-only")
        return False
    except subprocess.TimeoutExpired:
        _emit(log, "push failed (offline?): git timed out — continuing local-only")
        return False
    except Exception as e:
        _emit(log, f"push failed (offline?): {e} — continuing local-only")
        return False


def status(log=None) -> dict:
    """Remote URL, ahead/behind counts, last-pull epoch, and conflict-flag
    state. Best-effort fetch first for accurate counts; a failed fetch just
    leaves the counts relative to the last known origin/main."""
    conflict = SYNC_CONFLICT_FLAG.exists()
    last_pull = _read_epoch(LAST_SYNC_PATH)
    if not enabled():
        return {
            "enabled": False,
            "remote": None,
            "ahead": 0,
            "behind": 0,
            "last_pull": last_pull,
            "conflict": conflict,
        }
    try:
        _git(["fetch", "origin"])
    except subprocess.TimeoutExpired:
        pass
    ahead = behind = 0
    r = _git(["rev-list", "--left-right", "--count", "origin/main...HEAD"])
    parts = r.stdout.split()
    if len(parts) == 2:
        try:
            behind, ahead = int(parts[0]), int(parts[1])
        except ValueError:
            pass
    return {
        "enabled": True,
        "remote": _remote_url(),
        "ahead": ahead,
        "behind": behind,
        "last_pull": last_pull,
        "conflict": conflict,
    }


def _print_status() -> None:
    s = status()
    if not s["enabled"]:
        print("sync disabled (no origin)")
        return
    lp = s["last_pull"]
    when = (
        datetime.datetime.fromtimestamp(lp).isoformat(timespec="seconds")
        if lp
        else "never"
    )
    print(f"remote:    {s['remote']}")
    print(f"ahead:     {s['ahead']}")
    print(f"behind:    {s['behind']}")
    print(f"last pull: {when}")
    print(f"conflict:  {SYNC_CONFLICT_FLAG if s['conflict'] else 'none'}")


def main() -> int:
    """CLI: brain-sync pull|push|status. Exit 0 on success/no-op, 1 on a
    degraded failure (callers treat it as advisory, never fatal)."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in ("pull", "push", "status"):
        print("usage: brain-sync pull|push|status", file=sys.stderr)
        return 2
    if not enabled():
        print("sync disabled (BRAIN_SYNC=0)" if os.environ.get("BRAIN_SYNC") == "0"
              else "sync disabled (no origin)")
        return 0
    if cmd == "status":
        _print_status()
        return 0
    ok = pull(None, drain=True, allow_rebase=True) if cmd == "pull" else push(None)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
