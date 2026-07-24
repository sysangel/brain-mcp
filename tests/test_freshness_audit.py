"""brain.freshness_audit: the state-note freshness auditor.

Hermetic — tmp dirs only, the real repo/db are never touched, and the `claude`
spawn is always stubbed. Covers the staleness decision (drift-only, episodes-only,
both, neither, and the missing-repo episodes-only path), note->project derivation,
cap enforcement, needsUser emission, and the read-only `--dry-run` mode.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from brain import db
from brain import freshness_audit as fa


def _audit(name, *, stale=True, mapping_ok=True, n_since=99, drift=False):
    return fa.NoteAudit(
        note_path=Path(f"memories/{name}-state.md"),
        project=name,
        root=Path("/x") / name,
        mapping_ok=mapping_ok,
        repo_exists=False,
        epoch=1.0,
        n_since=n_since,
        drift=drift,
        stale=stale,
    )


# --- note -> project derivation ---------------------------------------------

def test_note_project_derivation():
    assert fa.note_project(Path("memories/project-alpha-state.md")) == "project-alpha"
    assert fa.note_project(Path("/a/project-beta-state.md")) == "project-beta"
    assert (
        fa.note_project(Path("/a/project-gamma-onboarding-state.md"))
        == "project-gamma-onboarding"
    )


def test_project_root_maps_under_src():
    # ~/src/<project> == BRAIN_DIR.parent/<project>; brain itself -> BRAIN_DIR.
    assert fa.project_root("project-alpha") == fa.BRAIN_DIR.parent / "project-alpha"
    assert fa.project_root(fa.BRAIN_DIR.name) == fa.BRAIN_DIR


def test_mapping_ok_rejects_empty_and_path_fragments():
    assert fa._mapping_ok("project-alpha")
    assert not fa._mapping_ok("")
    assert not fa._mapping_ok(".")
    assert not fa._mapping_ok("..")
    assert not fa._mapping_ok("a/b")


# --- staleness decision ------------------------------------------------------

def test_is_stale_matrix(monkeypatch):
    monkeypatch.setattr(fa, "SESSION_EPISODES_BANNER", 10)
    assert fa.is_stale(0, False) is False        # neither
    assert fa.is_stale(9, False) is False        # below banner, no drift
    assert fa.is_stale(10, False) is True         # episodes-only (at threshold)
    assert fa.is_stale(0, True) is True           # drift-only
    assert fa.is_stale(10, True) is True          # both
    assert fa.is_stale(None, False) is False      # None episode count reads as 0
    assert fa.is_stale(None, True) is True         # drift still fires


def test_audit_note_missing_repo_uses_episodes_only(tmp_path, monkeypatch):
    """Repo absent (the VM case): drift is skipped entirely and staleness rests
    on episodes_since alone — proven by making repo_drift raise if ever called."""
    note = tmp_path / "widget-state.md"
    note.write_text("body")
    monkeypatch.setattr(fa, "project_root", lambda project: tmp_path / "no-such-repo")
    monkeypatch.setattr(fa, "parse_note", lambda p: ("fm", "body", 1000.0, 1000.0))
    monkeypatch.setattr(fa, "SESSION_EPISODES_BANNER", 10)

    def _boom(*a, **k):
        raise AssertionError("repo_drift must not run when the repo is absent")

    monkeypatch.setattr(fa, "repo_drift", _boom)

    monkeypatch.setattr(fa, "episodes_since_for_project", lambda epoch, project: 12)
    na = fa.audit_note(note)
    assert na.repo_exists is False
    assert na.drift is False
    assert na.n_since == 12
    assert na.stale is True  # episodes-only crossed the banner

    monkeypatch.setattr(fa, "episodes_since_for_project", lambda epoch, project: 3)
    na2 = fa.audit_note(note)
    assert na2.stale is False  # below banner, drift skipped -> fresh


def test_audit_note_present_repo_uses_drift(tmp_path, monkeypatch):
    repo = tmp_path / "src" / "project-alpha"
    (repo / ".git").mkdir(parents=True)
    note = tmp_path / "project-alpha-state.md"
    note.write_text("body")
    monkeypatch.setattr(fa, "project_root", lambda project: repo)
    monkeypatch.setattr(fa, "parse_note", lambda p: ("fm", "body", 1000.0, 1000.0))
    monkeypatch.setattr(fa, "episodes_since_for_project", lambda epoch, project: 0)
    monkeypatch.setattr(fa, "repo_drift", lambda root, epoch: True)  # drift fires
    monkeypatch.setattr(fa, "SESSION_EPISODES_BANNER", 10)
    na = fa.audit_note(note)
    assert na.repo_exists is True
    assert na.drift is True
    assert na.stale is True  # drift-only path with a present repo


# --- cap enforcement ---------------------------------------------------------

def test_select_for_refresh_caps_and_reports_skipped():
    items = [f"a{i}" for i in range(5)]
    to, skipped = fa.select_for_refresh(items, 3)
    assert to == items[:3]
    assert skipped == items[3:]
    # negative cap means no cap
    to2, skipped2 = fa.select_for_refresh(items, -1)
    assert to2 == items and skipped2 == []


def test_run_caps_refresh_agents(tmp_path, monkeypatch, capsys):
    audits = [_audit(f"p{i}") for i in range(5)]  # all stale, mapping_ok
    monkeypatch.setattr(fa, "sweep", lambda memories_dir=None: audits)
    monkeypatch.setattr(fa, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fa, "AUDIT_LOCK_PATH", tmp_path / ".freshness-audit.lock")
    monkeypatch.setattr(fa, "resolve_claude", lambda: "claude")
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "2")

    calls = []
    monkeypatch.setattr(fa, "refresh_note", lambda na, **k: calls.append(na.project) or 0)

    rc = fa.run([])
    assert rc == 0
    assert calls == ["p0", "p1"]  # capped at 2, in filename order
    out = capsys.readouterr().out
    assert "over cap" in out
    for over in ("p2", "p3", "p4"):
        assert over in out  # the skipped notes are logged


# --- needsUser emission ------------------------------------------------------

def test_write_needs_user_emits_and_lands(tmp_path, monkeypatch):
    monkeypatch.setattr(fa, "PROPOSALS_DIR", tmp_path / "proposals")
    landed = []
    monkeypatch.setattr(fa, "_land", lambda paths, msg: landed.append((paths, msg)) or True)

    note = Path("memories/gizmo-state.md")
    path = fa.write_needs_user(note, "mapping-failure", "the detail body")
    assert path.exists()
    assert path.name.endswith("-gizmo.md") and path.name.startswith("needs-user-")
    text = path.read_text()
    assert "mapping-failure" in text
    assert "memories/gizmo-state.md" in text
    assert "type: proposal" in text
    assert "the detail body" in text
    assert landed and landed[0][0] == [path]  # committed via _land


def test_maybe_emit_needs_user_dedups_across_dates(tmp_path, monkeypatch):
    proposals = tmp_path / "proposals"
    proposals.mkdir(parents=True)
    monkeypatch.setattr(fa, "PROPOSALS_DIR", proposals)
    monkeypatch.setattr(fa, "_land", lambda paths, msg: True)

    note = Path("memories/gizmo-state.md")
    # A still-unresolved proposal from ANY prior date suppresses re-filing.
    (proposals / "needs-user-2020-01-01-gizmo.md").write_text("old, unresolved")
    assert fa.maybe_emit_needs_user(note, "mapping-failure", "d") is None

    # A different note is unaffected.
    other = Path("memories/gadget-state.md")
    emitted = fa.maybe_emit_needs_user(other, "mapping-failure", "d")
    assert emitted is not None and emitted.exists()


def test_run_files_needs_user_for_mapping_failure(tmp_path, monkeypatch, capsys):
    bad = fa.NoteAudit(
        note_path=Path("memories/-state.md"),
        project="",
        root=Path("/x"),
        mapping_ok=False,
        repo_exists=False,
        epoch=None,
        n_since=None,
        drift=False,
        stale=False,
    )
    monkeypatch.setattr(fa, "sweep", lambda memories_dir=None: [bad])
    monkeypatch.setattr(fa, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fa, "AUDIT_LOCK_PATH", tmp_path / ".freshness-audit.lock")

    emitted = []
    monkeypatch.setattr(
        fa,
        "maybe_emit_needs_user",
        lambda note, reason, detail: emitted.append((note, reason))
        or (tmp_path / "proposals" / "x.md"),
    )
    refresh_calls = []
    monkeypatch.setattr(fa, "refresh_note", lambda na, **k: refresh_calls.append(na) or 0)

    rc = fa.run([])
    assert rc == 0
    assert emitted and emitted[0][1] == "mapping-failure"
    assert refresh_calls == []  # a mapping failure is never a refresh target


# --- actor: stubbed claude spawn --------------------------------------------

class _FakeProc:
    def __init__(self, rc=0):
        self._rc = rc
        self.pid = 424242

    def wait(self, timeout=None):
        return self._rc


def test_refresh_note_spawns_stubbed_claude(monkeypatch):
    captured = {}

    def fake_popen(cmd, cwd=None, env=None, start_new_session=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["new_session"] = start_new_session
        return _FakeProc(0)

    monkeypatch.setattr(fa.subprocess, "Popen", fake_popen)
    na = _audit("project-alpha", n_since=15, drift=False)
    rc = fa.refresh_note(na, claude="/bin/claude", timeout_s=5, log=lambda m: None)
    assert rc == 0

    cmd = captured["cmd"]
    assert cmd[0] == "/bin/claude"
    assert "-p" in cmd and "--model" in cmd and "opus" in cmd
    assert "mcp__brain__recall" in cmd  # tight allowlist passed through
    assert captured["env"]["BRAIN_NO_AUTOREFLECT"] == "1"  # recursion guard
    assert captured["cwd"] == str(fa.BRAIN_DIR)
    assert captured["new_session"] is True


def test_build_prompt_names_note_format_and_escape_hatch():
    na = _audit("project-alpha", n_since=15, drift=False)
    p = fa.build_prompt(na)
    assert "memories/project-alpha-state.md" in p
    # in-format rewrite: the section shape is named
    assert "Next actions" in p and "Pointers" in p
    # repo-absent wording (repo_exists=False in _audit)
    assert "NOT checked out" in p
    # needsUser escape hatch path is offered
    assert "needs-user-" in p


# --- dry run: reads, never writes or spawns ---------------------------------

def test_dry_run_reports_and_spawns_nothing(tmp_path, monkeypatch, capsys):
    md = tmp_path / "memories"
    md.mkdir()
    (md / "alpha-state.md").write_text("body")
    (md / "beta-state.md").write_text("body")
    monkeypatch.setattr(fa, "MEMORIES_DIR", md)
    monkeypatch.setattr(fa, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(fa, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fa, "AUDIT_LOCK_PATH", tmp_path / ".freshness-audit.lock")

    def fake_audit(p):
        stale = p.name.startswith("alpha")
        return fa.NoteAudit(
            note_path=p,
            project=fa.note_project(p),
            root=Path("/x"),
            mapping_ok=True,
            repo_exists=False,
            epoch=1.0,
            n_since=15 if stale else 0,
            drift=False,
            stale=stale,
        )

    monkeypatch.setattr(fa, "audit_note", fake_audit)

    def _no_spawn(*a, **k):
        raise AssertionError("--dry-run must not spawn a refresh agent")

    monkeypatch.setattr(fa, "refresh_note", _no_spawn)

    rc = fa.run(["--dry-run"])
    assert rc == 0

    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "alpha-state.md" in out and "would refresh" in out
    assert "beta-state.md" in out and "fresh" in out

    # Wrote nothing, took no lock.
    assert not (tmp_path / ".freshness-audit.lock").exists()
    assert not (tmp_path / "proposals").exists()


# --- env parsing -------------------------------------------------------------

def test_max_refresh_env_and_fallbacks(monkeypatch):
    monkeypatch.delenv("BRAIN_AUDIT_MAX_REFRESH", raising=False)
    assert fa.max_refresh() == fa.DEFAULT_MAX_REFRESH
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "5")
    assert fa.max_refresh() == 5
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "0")
    assert fa.max_refresh() == 0
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "-1")  # invalid -> default
    assert fa.max_refresh() == fa.DEFAULT_MAX_REFRESH
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "junk")  # unparseable -> default
    assert fa.max_refresh() == fa.DEFAULT_MAX_REFRESH


def test_refresh_timeout_env_and_fallbacks(monkeypatch):
    monkeypatch.delenv("BRAIN_AUDIT_REFRESH_TIMEOUT", raising=False)
    assert fa.refresh_timeout() == fa.DEFAULT_REFRESH_TIMEOUT_S
    monkeypatch.setenv("BRAIN_AUDIT_REFRESH_TIMEOUT", "60")
    assert fa.refresh_timeout() == 60.0
    monkeypatch.setenv("BRAIN_AUDIT_REFRESH_TIMEOUT", "0")  # <= 0 -> default
    assert fa.refresh_timeout() == fa.DEFAULT_REFRESH_TIMEOUT_S
    monkeypatch.setenv("BRAIN_AUDIT_REFRESH_TIMEOUT", "nan")  # non-finite -> default
    assert fa.refresh_timeout() == fa.DEFAULT_REFRESH_TIMEOUT_S


# --- allowlist is pinned (silent broadening fails CI) ------------------------

def test_audit_allowed_tools_is_pinned():
    # Exact contents — REFLECT_ALLOWED_TOOLS + only the two brain MCP evidence
    # tools. No Read/Write/Edit (acceptEdits covers in-repo writes). If this ever
    # changes the surface silently broadens, so the pin must be updated on purpose.
    assert fa.AUDIT_ALLOWED_TOOLS == [
        "Bash(uv:*)",
        "Bash(sqlite3:*)",
        "Bash(git add:*)",
        "Bash(git commit:*)",
        "Bash(git log:*)",
        "Bash(git status:*)",
        "Bash(git diff:*)",
        "Bash(git mv:*)",
        "Bash(git rm:*)",
        "Bash(git -C:*)",
        "Bash(hostname:*)",
        "mcp__brain__recall",
        "mcp__brain__get_episode",
    ]


# --- BLOCKER 1: host-independent suffix match (VM-shaped fixture) -------------

_seed_n = 0


def _seed_episode(conn, cwd, ts, *, is_sidechain=0):
    global _seed_n
    _seed_n += 1
    conn.execute(
        "INSERT INTO episodes(stable_id, content_hash, session_id, uuid, ts, cwd, "
        "git_branch, is_sidechain, source_file, text) "
        "VALUES (?, 'h', ?, ?, ?, ?, 'main', ?, 'f.jsonl', 'body')",
        (f"ep_{_seed_n:016d}", f"s{_seed_n}", f"u{_seed_n}", ts, cwd, is_sidechain),
    )


def test_episodes_since_for_project_suffix_match_is_host_independent(tmp_path, monkeypatch):
    """The db holds Mac-absolute cwds; BRAIN_DIR is a NON-Mac path. A prefix match
    on the local root would count zero — the suffix match must still find them."""
    dbp = tmp_path / "brain.db"
    conn = db.connect(dbp)
    # Mac-style cwds written on the laptop:
    _seed_episode(conn, "/Users/you/src/project-alpha", "2026-07-02T00:00:00Z")
    _seed_episode(conn, "/Users/you/src/project-alpha/packages/core", "2026-07-03T00:00:00Z")
    # A different project that shares a prefix segment must NOT be counted:
    _seed_episode(conn, "/Users/you/src/project-alpha-tools", "2026-07-03T00:00:00Z")
    _seed_episode(conn, "/Users/you/src/other", "2026-07-03T00:00:00Z")
    # Older than the note -> excluded; sidechain -> excluded:
    _seed_episode(conn, "/Users/you/src/project-alpha", "2026-06-01T00:00:00Z")
    _seed_episode(conn, "/Users/you/src/project-alpha", "2026-07-04T00:00:00Z", is_sidechain=1)
    conn.commit()
    conn.close()

    # VM shape: a non-Mac BRAIN_DIR, and connect_ro pointed at the tmp db.
    # Capture the real connect_ro BEFORE patching so the stub can't recurse.
    real_connect_ro = db.connect_ro
    monkeypatch.setattr(fa, "BRAIN_DIR", Path("/home/you/src/brain"))
    monkeypatch.setattr(fa.db, "connect_ro", lambda *a, **k: real_connect_ro(dbp))

    from datetime import datetime, timezone

    epoch = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()
    # Only the two project-alpha / project-alpha-subdir episodes newer than the note count.
    assert fa.episodes_since_for_project(epoch, "project-alpha") == 2
    # A project with no episodes -> 0, never None-from-crash.
    assert fa.episodes_since_for_project(epoch, "nonesuch") == 0


# --- BLOCKER 2: brain-state must not self-oscillate --------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def test_brain_state_no_oscillation_after_auto_commit(tmp_path, monkeypatch):
    """After a fresh commit to the brain repo (an episode emit / auto-refresh),
    brain-state must NOT read stale on the next sweep: for root == BRAIN_DIR the
    drift component is skipped, so freshness rests on episodes_since alone."""
    repo = tmp_path / "src" / "brain"
    memories = repo / "memories"
    memories.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    note = memories / "brain-state.md"
    note.write_text("---\nname: brain-state\nwritten: 2020-01-01T00:00:00Z\n---\n\nbody\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "note")
    # Simulate a later auto-commit moving HEAD well past the note's written stamp.
    (repo / "later.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "auto-commit")

    monkeypatch.setattr(fa, "BRAIN_DIR", repo)
    monkeypatch.setattr(fa, "MEMORIES_DIR", memories)
    # Real repo_drift runs (not stubbed) — the guard must keep it from firing.
    monkeypatch.setattr(fa, "episodes_since_for_project", lambda epoch, project: 2)
    monkeypatch.setattr(fa, "SESSION_EPISODES_BANNER", 10)

    audits = fa.sweep()
    brain = next(a for a in audits if a.project == "brain")
    assert brain.root == repo and brain.repo_exists is True
    assert brain.drift is False   # skipped for BRAIN_DIR -> no oscillation
    assert brain.stale is False   # episodes below banner, no drift


# --- BLOCKER 3: pending needsUser suppresses re-refresh, frees the cap slot ---

def test_pending_needs_user_skips_refresh_and_frees_cap(tmp_path, monkeypatch, capsys):
    a = _audit("aaa")  # stale, has a pending proposal
    b = _audit("bbb")  # stale, no proposal
    c = _audit("ccc")  # stale, no proposal
    monkeypatch.setattr(fa, "sweep", lambda memories_dir=None: [a, b, c])
    monkeypatch.setattr(fa, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fa, "AUDIT_LOCK_PATH", tmp_path / ".freshness-audit.lock")
    monkeypatch.setattr(fa, "resolve_claude", lambda: "claude")
    monkeypatch.setattr(
        fa, "has_pending_needs_user", lambda note: note.name.startswith("aaa")
    )
    monkeypatch.setenv("BRAIN_AUDIT_MAX_REFRESH", "1")  # single slot

    calls = []
    monkeypatch.setattr(fa, "refresh_note", lambda na, **k: calls.append(na.project) or 0)

    rc = fa.run([])
    assert rc == 0
    # `aaa` is first but pending -> dropped; the one cap slot goes to `bbb`, not
    # consumed by `aaa`. `ccc` is over cap.
    assert calls == ["bbb"]
    out = capsys.readouterr().out
    assert "needs-user pending — skipped: aaa-state.md" in out
    assert "over cap" in out and "ccc" in out


def test_run_propagates_failed_refresh_rc(tmp_path, monkeypatch):
    a = _audit("aaa")
    b = _audit("bbb")
    monkeypatch.setattr(fa, "sweep", lambda memories_dir=None: [a, b])
    monkeypatch.setattr(fa, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(fa, "AUDIT_LOCK_PATH", tmp_path / ".freshness-audit.lock")
    monkeypatch.setattr(fa, "has_pending_needs_user", lambda note: False)
    monkeypatch.setattr(fa, "resolve_claude", lambda: "claude")
    monkeypatch.delenv("BRAIN_AUDIT_MAX_REFRESH", raising=False)
    # First agent fails, second succeeds — run() must still report nonzero.
    monkeypatch.setattr(fa, "refresh_note", lambda na, **k: 1 if na.project == "aaa" else 0)
    assert fa.run([]) != 0


# --- minor 4: dedup glob must not collide on a slug suffix -------------------

def test_dedup_glob_does_not_collide_on_slug_suffix(tmp_path, monkeypatch):
    proposals = tmp_path / "proposals"
    proposals.mkdir(parents=True)
    monkeypatch.setattr(fa, "PROPOSALS_DIR", proposals)
    monkeypatch.setattr(fa, "_land", lambda paths, msg: True)

    # An unrelated proposal for note `foo-bar` must not suppress note `bar`.
    (proposals / "needs-user-2020-01-01-foo-bar.md").write_text("unrelated")
    assert fa.has_pending_needs_user(Path("memories/bar-state.md")) is False
    emitted = fa.maybe_emit_needs_user(Path("memories/bar-state.md"), "mapping-failure", "d")
    assert emitted is not None and emitted.name.endswith("-bar.md")

    # But the exact slug (any date) IS caught.
    assert fa.has_pending_needs_user(Path("memories/foo-bar-state.md")) is True
