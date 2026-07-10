"""Bounded child on auto-reflect: a hung `claude` must not hold the flock
forever. Drives `main()` with a fake sleeping `claude` on a tiny timeout and
asserts TimeoutExpired is caught, nonzero is returned, and we don't wait out
the child's full sleep.

`main()` now launches the child in its own process group (start_new_session)
and on timeout SIGTERMs the WHOLE group, so grandchildren (the spawned MCP
server, tool children mid `git commit`) die too instead of surviving to race
the next reflect. A second test proves that: the fake claude backgrounds a
grandchild that writes a sentinel after a delay, and we assert the sentinel
never appears because the group was killed first.

Network-free: the fake binary is a local shell script; no real claude runs."""

import stat
import sys
import time

from brain import autoreflect


def _make_fake_claude(tmp_path, sleep_s=30, sentinel=None, sentinel_delay=0):
    """Write a fake `claude`. It sleeps `sleep_s` (so main() times out on it).
    If `sentinel` is given, it first backgrounds a grandchild that, after
    `sentinel_delay`s, writes the sentinel file — the marker we use to detect a
    grandchild that outlived the kill."""
    script = tmp_path / "claude"
    lines = ["#!/bin/sh"]
    if sentinel is not None:
        # Subshell in the SAME process group as the script: killpg reaches it.
        lines.append(f'( sleep {sentinel_delay}; touch "{sentinel}" ) &')
    lines.append(f"sleep {sleep_s}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_hung_child_times_out_and_returns_nonzero(tmp_path, monkeypatch):
    fake = _make_fake_claude(tmp_path)
    lock = tmp_path / ".reflect.lock"

    monkeypatch.setattr(autoreflect, "REFLECT_LOCK_PATH", lock)
    monkeypatch.setattr(sys, "argv", ["brain-autoreflect", "--force"])
    monkeypatch.setenv("BRAIN_CLAUDE_BIN", str(fake))  # _resolve_claude short-circuits here
    monkeypatch.setenv("BRAIN_REFLECT_TIMEOUT", "0.5")  # shrink the bound for the test
    monkeypatch.delenv("BRAIN_AUTOREFLECT_DRYRUN", raising=False)

    start = time.monotonic()
    rc = autoreflect.main()
    elapsed = time.monotonic() - start

    assert rc != 0  # TimeoutExpired was caught and demoted to a nonzero return
    # Proof the child was killed rather than awaited: we returned far sooner than
    # the fake's 30s sleep. If the child weren't terminated, wait() would block on it.
    assert elapsed < 10, f"main() waited {elapsed:.1f}s — child not terminated"
    # Flock was released on our return (fh went out of scope / process still live),
    # so a subsequent acquisition must succeed rather than skip.
    assert not _lock_is_held(lock)


def test_timeout_kills_grandchildren(tmp_path, monkeypatch):
    """On timeout, main() must kill the child's whole process group, not just the
    direct child. The fake claude backgrounds a grandchild that writes a sentinel
    after 3s; if the group is killed at the 0.5s timeout, the sentinel never
    appears even though we wait well past the 3s mark."""
    sentinel = tmp_path / "grandchild.sentinel"
    fake = _make_fake_claude(tmp_path, sleep_s=30, sentinel=sentinel, sentinel_delay=3)
    lock = tmp_path / ".reflect.lock"

    monkeypatch.setattr(autoreflect, "REFLECT_LOCK_PATH", lock)
    monkeypatch.setattr(sys, "argv", ["brain-autoreflect", "--force"])
    monkeypatch.setenv("BRAIN_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("BRAIN_REFLECT_TIMEOUT", "0.5")
    monkeypatch.delenv("BRAIN_AUTOREFLECT_DRYRUN", raising=False)

    start = time.monotonic()
    rc = autoreflect.main()
    assert rc != 0

    # Wait past the grandchild's 3s sentinel delay (measured from the same start).
    # A surviving grandchild would fire around t=3s; a killed one never does.
    while time.monotonic() - start < 4.5:
        assert not sentinel.exists(), "grandchild survived the group kill and wrote the sentinel"
        time.sleep(0.1)
    assert not sentinel.exists(), "grandchild survived the group kill and wrote the sentinel"


def _lock_is_held(path):
    """True iff another holder still owns the exclusive flock on `path`."""
    import fcntl

    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
