"""brain.reflect_window: the committed cross-host reflect watermark + claim.

Hermetic — every test runs against a tmp git repo monkeypatched in as
BRAIN_DIR; sync is stubbed (enabled/pull/push), so no network and no flock.
Covers watermark resolution (committed vs legacy vs neither), the claim
lifecycle (grant, other-host refusal, TTL expiry, own-host takeover), the
hard-fail pull gate, the lost-push undo, and complete's stamp/stage effects.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import reflect_window as rw


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "brain"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    monkeypatch.setattr(rw, "BRAIN_DIR", repo)
    monkeypatch.setattr(rw, "STATE_DIR", repo / "state")
    monkeypatch.setattr(rw, "REFLECT_WATERMARK_PATH", repo / "state" / "last-reflect")
    monkeypatch.setattr(rw, "REFLECT_CLAIM_PATH", repo / "state" / "reflect-claim")
    monkeypatch.setattr(rw, "LAST_REFLECT_PATH", repo / "cache" / "last-reflect")
    monkeypatch.setattr(rw.sync, "enabled", lambda: False)
    monkeypatch.setattr(rw, "_hostname", lambda: "mac")
    return repo


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _head_subject(repo) -> str:
    return _git(repo, "log", "-1", "--format=%s").stdout.strip()


# --- watermark resolution -----------------------------------------------------

def test_read_watermark_prefers_newest_of_committed_and_legacy(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    assert rw.read_watermark() is None  # neither exists

    _write(repo / "cache" / "last-reflect", "2026-07-01T00:00:00Z")
    assert rw.read_watermark() == datetime(2026, 7, 1, tzinfo=timezone.utc)

    # Committed watermark older than legacy -> legacy (this host's) wins.
    _write(repo / "state" / "last-reflect", "2026-06-01T00:00:00Z\n")
    assert rw.read_watermark() == datetime(2026, 7, 1, tzinfo=timezone.utc)

    # Committed newer (another host reflected) -> committed wins.
    _write(repo / "state" / "last-reflect", "2026-07-02T12:00:00Z\n")
    assert rw.read_watermark() == datetime(2026, 7, 2, 12, tzinfo=timezone.utc)


def test_garbled_stamps_read_as_absent(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    _write(repo / "state" / "last-reflect", "not a date")
    _write(repo / "cache" / "last-reflect", "")
    assert rw.read_watermark() is None
    _write(repo / "state" / "reflect-claim", "one-token-only")
    assert rw.parse_claim() is None


# --- claim: grant paths -------------------------------------------------------

def test_claim_prints_floor_and_commits_lease(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    _write(repo / "cache" / "last-reflect", "2026-07-01T00:00:00Z")

    assert rw.claim() == 0
    assert capsys.readouterr().out.strip() == "2026-07-01T00:00:00Z"  # the floor
    host, started = rw.parse_claim()
    assert host == "mac"
    assert _head_subject(repo).startswith("reflect-claim: mac ")
    # The lease commit contains ONLY the claim file.
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert files == ["state/reflect-claim"]


def test_claim_defaults_to_48h_floor_when_no_watermark(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    assert rw.claim() == 0
    floor = datetime.fromisoformat(capsys.readouterr().out.strip().replace("Z", "+00:00"))
    age_h = (datetime.now(timezone.utc) - floor).total_seconds() / 3600
    assert abs(age_h - rw.DEFAULT_WINDOW_H) < 0.1


def test_claim_takes_over_expired_and_own_host_leases(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    # Expired other-host lease (older than TTL) -> takeover.
    old = datetime.now(timezone.utc) - timedelta(seconds=rw.REFLECT_CLAIM_TTL_S + 60)
    _write(repo / "state" / "reflect-claim", f"vm {rw._iso_z(old)}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "old lease")
    assert rw.claim() == 0
    assert rw.parse_claim()[0] == "mac"

    # Fresh OWN lease (a crashed local pass) -> takeover, not deadlock.
    _write(repo / "state" / "reflect-claim", f"mac {rw._iso_z(datetime.now(timezone.utc))}\n")
    assert rw.claim() == 0


# --- claim: refusal paths -----------------------------------------------------

def test_claim_refused_while_other_host_lease_is_fresh(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    _write(
        repo / "state" / "reflect-claim",
        f"vm {rw._iso_z(datetime.now(timezone.utc) - timedelta(seconds=60))}\n",
    )
    before = _head_subject(repo)
    assert rw.claim() == 1
    assert "held by vm" in capsys.readouterr().err
    assert _head_subject(repo) == before  # no commit made
    assert rw.parse_claim()[0] == "vm"  # lease untouched


def test_claim_hard_fails_when_pull_fails(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(rw.sync, "enabled", lambda: True)
    monkeypatch.setattr(rw.sync, "pull", lambda *a, **k: False)
    before = _head_subject(repo)
    assert rw.claim() == 1
    assert "refusing to open a review window unsynced" in capsys.readouterr().err
    assert _head_subject(repo) == before
    assert not (repo / "state" / "reflect-claim").exists()


def test_claim_lost_push_race_undoes_lease_commit(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(rw.sync, "enabled", lambda: True)
    monkeypatch.setattr(rw.sync, "pull", lambda *a, **k: True)
    monkeypatch.setattr(rw.sync, "push", lambda *a, **k: False)
    # An unrelated dirty tracked file must SURVIVE the undo (mixed reset, never --hard).
    (repo / "seed.txt").write_text("mid-edit")
    before = _head_subject(repo)

    assert rw.claim() == 1
    assert "another host is reflecting" in capsys.readouterr().err
    assert _head_subject(repo) == before  # lease commit rolled back
    assert not (repo / "state" / "reflect-claim").exists()
    assert (repo / "seed.txt").read_text() == "mid-edit"


def test_undo_never_resets_a_foreign_head(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    before = _head_subject(repo)  # "seed" — not a claim commit
    rw._undo_claim_commit()
    assert _head_subject(repo) == before


# --- complete -----------------------------------------------------------------

def test_complete_advances_watermark_drops_claim_and_stages(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path, monkeypatch)
    _write(repo / "state" / "reflect-claim", "mac 2026-07-29T00:00:00Z\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "reflect-claim: mac 2026-07-29T00:00:00Z")

    assert rw.complete() == 0
    iso = capsys.readouterr().out.strip()
    assert (repo / "state" / "last-reflect").read_text().strip() == iso
    assert (repo / "cache" / "last-reflect").read_text().strip() == iso  # legacy mirror
    assert not (repo / "state" / "reflect-claim").exists()
    # Watermark write + claim removal are STAGED for the reflect results commit.
    staged = _git(repo, "diff", "--cached", "--name-status").stdout.split()
    assert staged == ["A", "state/last-reflect", "D", "state/reflect-claim"]
