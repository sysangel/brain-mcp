"""Sync's load-bearing invariant: no force-push, ever (brain.sync).

The module docstring is explicit — "No force-push, ever ... No code path
constructs --force / --force-with-lease." This test pins that two ways, both
network-free (no real git runs):

  1. Static: every `_git([...])` call site in sync.py is parsed from the AST;
     no literal git argument is a force flag.
  2. Behavioral: subprocess.run is replaced with a recorder so the REAL _git
     builds the actual argv, then push() (success + non-ff recovery) and
     pull() (down to the rebase fallback) are driven; no argv the module hands
     to git contains a force token.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from brain import sync

def _is_force(tok: str) -> bool:
    # Every shape git accepts to force an update:
    #   --force / --force-with-lease / --force-if-includes  (long flags)
    #   -f                                                  (short flag)
    #   +main, +refs/heads/main:...                         (force-push refspec)
    t = tok.strip()
    return (
        t.startswith("--force")
        or t == "-f"
        or (len(t) > 1 and t.startswith("+"))
    )


# ------------------------------------------------------------------- static ---

def test_no_git_call_site_constructs_a_force_flag():
    src = Path(sync.__file__).read_text()
    tree = ast.parse(src)
    call_sites = scanned = skipped = 0
    blind_spots = []  # non-literal git args the static scan can't see through
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_git"):
            continue
        assert node.args and isinstance(node.args[0], ast.List), \
            "every _git call passes a literal arg list"
        call_sites += 1
        for elt in node.args[0].elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                scanned += 1
                assert not _is_force(elt.value), f"force flag at a _git call: {elt.value!r}"
            else:
                # A variable/f-string arg (e.g. the `ref` passed to
                # `rev-parse --verify`): the static scan can't prove it's safe,
                # so record it as a visible blind spot rather than silently
                # dropping it. The behavioral test below is what actually covers
                # the runtime argv for the push/pull paths.
                skipped += 1
                blind_spots.append(ast.dump(elt))
    assert call_sites > 0  # the scan actually found the call sites
    # Pin the coverage so a newly-introduced non-literal git arg (a fresh blind
    # spot) trips this assertion and forces a human to re-check it by hand.
    assert (call_sites, scanned, skipped) == (16, 43, 1), (
        f"scan coverage changed: {call_sites} sites, {scanned} literal args "
        f"scanned, {skipped} skipped (blind spots: {blind_spots})"
    )


# --------------------------------------------------------------- behavioral ---

class _Recorder:
    """Interprets git subcommands enough to walk push()/pull() branches while
    recording every argv the module constructs."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._push_seen = 0

    def __call__(self, cmd, **kwargs):
        args = list(cmd[3:])  # strip the ["git", "-C", <dir>] prefix
        self.calls.append(args)
        sub = args[0]
        rc, out, err = 0, "", ""
        if sub == "push":
            self._push_seen += 1
            if self._push_seen == 1:  # first push -> non-ff, to drive recovery
                rc, err = 1, "! [rejected] main -> main (non-fast-forward)\nfetch first"
        elif sub == "rev-parse":
            if "--absolute-git-dir" in args:
                out = ""  # fall back to BRAIN_DIR/.git (real, no rebase-merge dir)
            elif args[-1] == "HEAD":
                out = "aaaaaaa"
            elif args[-1] == "origin/main":
                out = "bbbbbbb"  # diverged from HEAD -> not up to date
        elif sub == "status":
            out = ""  # clean tree
        elif sub == "merge":
            rc = 1  # ff-only fails -> fall through to the rebase branch
        elif sub == "rev-list":
            out = "0"  # not ahead -> no drain push
        return subprocess.CompletedProcess(cmd, rc, out, err)


def _drive(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(subprocess, "run", rec)  # real _git builds argv, we record
    monkeypatch.setattr(sync, "enabled", lambda: True)
    monkeypatch.setattr(sync, "_pull_succeeded", lambda log, drain: None)  # no cache writes
    return rec


def test_push_and_pull_never_hand_git_a_force_flag(monkeypatch):
    rec = _drive(monkeypatch)

    assert sync.push() is True          # first push non-ff -> recovery pull -> retry ok
    sync.pull(allow_rebase=True)        # drives down to the rebase fallback

    assert rec.calls, "the recorder captured no git invocations"
    for argv in rec.calls:
        for tok in argv:
            assert not _is_force(tok), f"sync handed git a force flag: {argv}"

    # Sanity: the branches we meant to exercise were actually reached.
    flat = [tuple(a) for a in rec.calls]
    assert ("push", "origin", "main") in flat            # the exact, unforced push
    assert any(a and a[0] == "rebase" for a in rec.calls)  # rebase fallback taken
