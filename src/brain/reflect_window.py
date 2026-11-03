"""Cross-host reflect window: committed watermark + push-atomic claim.

The reflect review window used to start at `cache/last-reflect` — host-local
and gitignored — so the Mac and the VM each advanced only their own stamp and
silently re-reviewed each other's windows (observed live 2026-07-29: VM pass
13:12, Mac pass 13:46, same window). This module moves the window floor into
the repo so every host shares it:

- `state/last-reflect` (committed): the watermark. Advanced by `complete` at
  the end of a successful pass; any host's reflect moves it for all hosts.
- `state/reflect-claim` (committed): a soft lease. `claim` writes it, commits
  it, and PUSHES it before the pass opens; git's non-fast-forward rejection
  makes that push the atomic arbiter when two hosts race — the loser undoes
  its claim commit and aborts. A crashed pass leaves its claim behind, which
  expires after REFLECT_CLAIM_TTL_S (> the reflect child timeout), so a
  window can be re-reviewed but never lost.

`claim` hard-fails when the pre-claim pull fails while sync is enabled: a host
that cannot see the true floor must not open a window (that blind spot is
exactly what caused the double review).

CLI (`brain-reflect-window`):
    claim      pull (hard-fail), check/take the lease, push it. stdout is
               exactly one line — the window-start ISO — on success; nonzero
               exit means do not reflect (reason on stderr).
    complete   advance the watermark, drop the claim, mirror the legacy
               cache stamp, and stage state/ so the reflect commit carries
               it. File ops only — no commit, no push.
    status     show watermark, claim, and legacy stamp.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sync
from .config import (
    BRAIN_DIR,
    LAST_REFLECT_PATH,
    REFLECT_CLAIM_PATH,
    REFLECT_CLAIM_TTL_S,
    REFLECT_WATERMARK_PATH,
    STATE_DIR,
)

# Floor when no watermark exists anywhere (first run on a fresh clone).
DEFAULT_WINDOW_H = 48.0

_CLAIM_SUBJECT = "reflect-claim:"


def _log(msg: str) -> None:
    print(f"[reflect-window] {msg}", file=sys.stderr)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_stamp_file(path: Path) -> datetime | None:
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    return _parse_iso(raw) if raw else None


def _hostname() -> str:
    return platform.node().split(".")[0] or "unknown"


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(BRAIN_DIR), *args], capture_output=True, text=True
    )


def read_watermark() -> datetime | None:
    """Newest known reflect completion across hosts: the committed watermark,
    with the legacy per-host cache stamp as a migration fallback. Take the max
    — mid-migration a host's legacy stamp can be newer than the repo's."""
    stamps = [
        _parse_stamp_file(REFLECT_WATERMARK_PATH),
        _parse_stamp_file(LAST_REFLECT_PATH),
    ]
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def parse_claim() -> tuple[str, datetime] | None:
    """The current lease as (host, started), or None when absent/garbled.
    A garbled claim reads as absent — claimable — rather than wedging every
    future reflect on both hosts."""
    try:
        raw = REFLECT_CLAIM_PATH.read_text().strip()
    except OSError:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2:
        return None
    started = _parse_iso(parts[1])
    return (parts[0], started) if started is not None else None


def _undo_claim_commit() -> None:
    """Roll back OUR just-made claim commit after a lost push race. Mixed reset
    (never --hard: it would clobber unrelated working-tree edits) only if HEAD
    is actually a claim commit, then drop the working-tree claim file."""
    head = _git(["log", "-1", "--format=%s"]).stdout.strip()
    if head.startswith(_CLAIM_SUBJECT):
        _git(["reset", "--quiet", "HEAD~1"])
    REFLECT_CLAIM_PATH.unlink(missing_ok=True)


def claim() -> int:
    if sync.enabled() and not sync.pull(_log, drain=False, allow_rebase=True):
        _log("pull failed — refusing to open a review window unsynced")
        return 1

    now = _now()
    held = parse_claim()
    if held is not None:
        host, started = held
        age_s = (now - started).total_seconds()
        if host != _hostname() and 0 <= age_s < REFLECT_CLAIM_TTL_S:
            _log(f"claim held by {host} since {_iso_z(started)} — skipping")
            return 1
        # Our own leftover (a crashed local pass) or an expired lease: take over.

    floor = read_watermark() or (now - timedelta(hours=DEFAULT_WINDOW_H))

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REFLECT_CLAIM_PATH.write_text(f"{_hostname()} {_iso_z(now)}\n")
    rel = REFLECT_CLAIM_PATH.relative_to(BRAIN_DIR)
    _git(["add", "--", str(rel)])
    # An own-host retake within the same second rewrites identical content —
    # nothing to commit, but the lease is already recorded; only commit a
    # real change.
    if _git(["status", "--porcelain", "--", str(rel)]).stdout.strip():
        cm = _git(
            [
                "commit",
                "--only",
                "-m",
                f"{_CLAIM_SUBJECT} {_hostname()} {_iso_z(now)}",
                "--",
                str(rel),
            ]
        )
        if cm.returncode != 0:
            REFLECT_CLAIM_PATH.unlink(missing_ok=True)
            _log(f"claim commit failed: {' '.join(cm.stderr.split())[:200]}")
            return 1

    # The push is the arbiter: a non-ff rejection here means another host got
    # its claim in first (sync.push's ff-only recovery pull cannot absorb our
    # local claim commit, so a true race stays rejected).
    if sync.enabled() and not sync.push(_log):
        _undo_claim_commit()
        _log("claim push rejected — another host is reflecting; skipping")
        return 1

    print(_iso_z(floor))
    return 0


def complete() -> int:
    now_iso = _iso_z(_now())
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    REFLECT_WATERMARK_PATH.write_text(now_iso + "\n")
    # Legacy mirror: local tooling that still reads cache/last-reflect keeps
    # working, and read_watermark's max() stays monotonic on this host.
    try:
        LAST_REFLECT_PATH.parent.mkdir(parents=True, exist_ok=True)
        LAST_REFLECT_PATH.write_text(now_iso)
    except OSError as e:
        _log(f"legacy stamp mirror failed (non-fatal): {e}")
    REFLECT_CLAIM_PATH.unlink(missing_ok=True)
    # Stage watermark + claim removal; the reflect pass's single results commit
    # carries them (complete itself never commits or pushes).
    _git(["add", "-A", "--", str(STATE_DIR.relative_to(BRAIN_DIR))])
    print(now_iso)
    return 0


def status() -> int:
    wm = _parse_stamp_file(REFLECT_WATERMARK_PATH)
    legacy = _parse_stamp_file(LAST_REFLECT_PATH)
    held = parse_claim()
    print(f"watermark: {_iso_z(wm) if wm else 'none'}")
    print(f"legacy:    {_iso_z(legacy) if legacy else 'none'}")
    print(f"claim:     {held[0] + ' ' + _iso_z(held[1]) if held else 'none'}")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "claim":
        return claim()
    if cmd == "complete":
        return complete()
    if cmd == "status":
        return status()
    print("usage: brain-reflect-window claim|complete|status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
