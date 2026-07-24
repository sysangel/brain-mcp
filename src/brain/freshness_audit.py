"""brain-freshness-audit: the first resident maintenance loop for the brain VM.

Check-first loop (autonomous-loops-doctrine): a mechanical CHECK sweeps every
`memories/<project>-state.md` note and reuses the SessionStart STALE rule
verbatim to decide which notes have fallen behind their project; an ACTOR then
spawns ONE targeted Opus refresh agent per stale note to rewrite it in-format
and auto-land the result. Determinism lives here in script code; the agent only
does the judgment step (rewrite / flag-for-human).

CHECK — for each `*-state.md` note:
  * <project> is the filename with the `-state.md` suffix removed.
  * the project repo is `~/src/<project>` (== BRAIN_DIR.parent/<project>).
    - repo present  -> compute repo_drift against the note's written-at.
    - repo ABSENT   -> skip the drift component (normal on the VM, which only
      checks out brain + skills); staleness rests on episodes_since alone. This
      is NOT a needsUser event.
  * episodes_since comes from the read-only index db (cwd-prefix match).
  * STALE iff `repo_drift OR episodes_since >= SESSION_EPISODES_BANNER` — the
    SessionStart rule, imported (not reimplemented) from session_start.

ACTOR — for each stale note (capped, serialized): `claude -p --model opus` in
BRAIN_DIR with a tight allowlist, told to rewrite THAT note in-format grounded
in recall/get_episode evidence (and the real repo when present) and auto-land
it (commit + push). Child-process hygiene mirrors autoreflect: BRAIN_NO_AUTOREFLECT=1
on the child, a bounded wait, and a process-group kill on timeout (kill_group
reused from autoreflect, not copied).

GATES (locked spec):
  * rewrites auto-land (the agent commits + pushes).
  * contradictions the agent cannot reconcile and archival candidates (project
    looks finished/abandoned) -> the agent writes a needsUser proposal instead
    of rewriting.
  * genuine mapping failures (a filename that yields no sensible project) ->
    the auditor itself emits a needsUser proposal. Per autonomous-loops-doctrine
    ("loops write proposals/, a human promotes"; no needsUser location/format is
    named there) these land as `proposals/needs-user-<UTCdate>-<note>.md`,
    committed + pushed so a Mac session surfaces them.

SERIALIZATION — a single non-blocking flock (cache/.freshness-audit.lock, the
autoreflect single-flight pattern) means overlapping timer fires can't
double-audit. Refresh agents are capped per run (BRAIN_AUDIT_MAX_REFRESH,
default 3) to bound cost; anything over the cap is logged and skipped.

`--dry-run` prints the stale set + planned actions and returns without taking
the lock, writing a file, or spawning anything — a safe read-only smoke check.
"""

from __future__ import annotations

import fcntl
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .autoreflect import REFLECT_ALLOWED_TOOLS, kill_group, resolve_claude
from .config import (
    BRAIN_DIR,
    CACHE_DIR,
    MEMORIES_DIR,
    SESSION_EPISODES_BANNER,
)
from .remember import commit_paths
from .session_start import parse_note, repo_drift
from . import db, sync

# --- constants (kept local to this module, never in config.py) --------------

# proposals/ is the doctrine-mandated write surface for autonomous loops; config
# has no PROPOSALS_DIR, so derive it here.
PROPOSALS_DIR = BRAIN_DIR / "proposals"

# Single-flight flock so two overlapping timer runs can't double-audit. Same
# non-blocking-flock pattern as autoreflect's REFLECT_LOCK_PATH / sync's lock.
AUDIT_LOCK_PATH = CACHE_DIR / ".freshness-audit.lock"

# Cap refresh agents per run to bound cost; over-cap notes are logged + skipped.
DEFAULT_MAX_REFRESH = 3
# Per-refresh-agent wall-clock bound; a hung `claude` is process-group-killed.
DEFAULT_REFRESH_TIMEOUT_S = 1800.0

# The refresh agent's tool allowlist — REFLECT_ALLOWED_TOOLS (git add/commit/log/
# status/diff/mv/rm, uv, sqlite3, hostname) plus the two brain MCP evidence tools.
# Read/Write/Edit are deliberately NOT listed: --permission-mode acceptEdits
# already auto-accepts reads and in-repo edits (this is exactly how the reflect
# agent rewrites memories/ with no Read/Write/Edit in its allowlist). Pinned by a
# test so a silent broadening of the surface fails CI.
AUDIT_ALLOWED_TOOLS = [
    *REFLECT_ALLOWED_TOOLS,
    "mcp__brain__recall",
    "mcp__brain__get_episode",
]

_STATE_SUFFIX = "-state.md"


# --- small env / time helpers ------------------------------------------------

def _log(msg: str) -> None:
    print(f"[freshness-audit] {msg}")


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def max_refresh() -> int:
    """BRAIN_AUDIT_MAX_REFRESH (non-negative int) or DEFAULT_MAX_REFRESH. A bad
    value never crashes the timer-driven run — it falls back to the default."""
    raw = os.environ.get("BRAIN_AUDIT_MAX_REFRESH")
    if raw is None:
        return DEFAULT_MAX_REFRESH
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_MAX_REFRESH
    return val if val >= 0 else DEFAULT_MAX_REFRESH


def refresh_timeout() -> float:
    """BRAIN_AUDIT_REFRESH_TIMEOUT (finite seconds > 0) or the default. nan/inf
    would disable the bound and let a hung claude wedge the run; <= 0 would kill
    instantly — both fall back to DEFAULT_REFRESH_TIMEOUT_S (mirrors autoreflect)."""
    raw = os.environ.get("BRAIN_AUDIT_REFRESH_TIMEOUT")
    if raw is None:
        return DEFAULT_REFRESH_TIMEOUT_S
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_REFRESH_TIMEOUT_S
    if not math.isfinite(val) or val <= 0:
        return DEFAULT_REFRESH_TIMEOUT_S
    return val


def _same_path(a: Path, b: Path) -> bool:
    """Path equality tolerant of symlinks (BRAIN_DIR may be a symlinked root)."""
    if a == b:
        return True
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def episodes_since_for_project(epoch: float, project: str) -> int | None:
    """Host-independent count of non-sidechain episodes for <project> recorded
    after `epoch`.

    Auditor-side SUFFIX match — session_start's own PREFIX match against the live
    root is deliberately left untouched. Episodes store the WRITING host's
    absolute cwd (Mac `/Users/<u>/src/<project>`); on the VM the local root is
    `/home/<u>/src/...`, so a prefix match on `BRAIN_DIR.parent/<project>` can
    never hit and the freshness signal would be structurally 0. Matching the
    `.../src/<project>` suffix instead is host-independent: it counts cwd equal
    to `<...>/src/<project>` or under `<...>/src/<project>/`. Lexical ISO ts
    compare (same invariant session_start/reflect rely on). None when the index
    is unavailable."""
    conn = db.connect_ro()
    if conn is None:
        return None
    iso = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Escape LIKE metacharacters so a project slug can't smuggle wildcards.
    tail = f"/src/{project}"
    esc = tail.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    try:
        row = conn.execute(
            "SELECT count(*) AS n FROM episodes "
            "WHERE is_sidechain = 0 AND ts > ? "
            "AND (cwd LIKE ? ESCAPE '\\' OR cwd LIKE ? ESCAPE '\\')",
            (iso, f"%{esc}", f"%{esc}/%"),
        ).fetchone()
        return int(row["n"])
    except Exception:
        return None
    finally:
        conn.close()


# --- CHECK phase -------------------------------------------------------------

@dataclass
class NoteAudit:
    """One state note's freshness verdict."""

    note_path: Path
    project: str
    root: Path
    mapping_ok: bool     # False -> filename yields no sensible project (needsUser)
    repo_exists: bool    # the project repo is checked out locally
    epoch: float | None  # note written-at (None if unreadable/uncommitted)
    n_since: int | None  # non-sidechain episodes for this project after the note
    drift: bool          # project repo HEAD newer than the note (repo present only)
    stale: bool          # the SessionStart STALE rule verdict


def note_project(note_path: Path) -> str:
    """Derive <project> from a `<project>-state.md` filename (suffix stripped,
    trimmed). `project-alpha-state.md` -> `project-alpha`."""
    name = note_path.name
    stem = name[: -len(_STATE_SUFFIX)] if name.endswith(_STATE_SUFFIX) else note_path.stem
    return stem.strip()


def project_root(project: str) -> Path:
    """The project repo path for <project>: `~/src/<project>`, expressed as
    BRAIN_DIR.parent/<project> so it tracks BRAIN_DIR overrides. (brain itself
    -> BRAIN_DIR; a sibling like project-alpha -> ~/src/project-alpha.)"""
    return BRAIN_DIR.parent / project


def _mapping_ok(project: str) -> bool:
    """A project name is usable when it is non-empty and not a path fragment
    (`.`, `..`, or containing a separator) that would escape ~/src."""
    if not project:
        return False
    if project in (".", ".."):
        return False
    return "/" not in project and os.sep not in project


def is_stale(n_since: int | None, drift: bool) -> bool:
    """The SessionStart STALE rule, verbatim: repo_drift OR
    episodes_since >= SESSION_EPISODES_BANNER. A None episode count (no index /
    no reference time) reads as 0, exactly like session_start's banner."""
    return bool(drift) or (n_since or 0) >= SESSION_EPISODES_BANNER


def audit_note(note_path: Path) -> NoteAudit:
    """Compute the freshness verdict for one `*-state.md` note. Reuses
    session_start.parse_note (note epoch), episodes_since (index count), and
    repo_drift (repo HEAD vs note) so the STALE rule has ONE definition. The
    drift component is skipped when the project repo is absent locally (the VM
    case); episodes_since is still counted — it is a cwd-string query against the
    index, independent of the filesystem."""
    project = note_project(note_path)
    mapping_ok = _mapping_ok(project)
    root = project_root(project) if mapping_ok else note_path.parent
    repo_exists = mapping_ok and (root / ".git").exists()

    parsed = parse_note(note_path)
    if parsed is None:
        epoch = commit_epoch = None
    else:
        _fm, _body, epoch, commit_epoch = parsed

    n_since = (
        episodes_since_for_project(epoch, project)
        if (mapping_ok and epoch is not None)
        else None
    )
    drift = False
    # Skip the drift component for BRAIN_DIR itself: the brain repo commits on
    # every episode emit / proposal / auto-refresh, so comparing brain-state
    # against brain HEAD would re-stale the note within a day (self-oscillation).
    # brain-state freshness rests on episodes_since alone, like the repo-absent
    # case. (repo_drift already absorbs the ordinary write->commit slack; this is
    # the stronger guard for the one repo the auditor itself keeps mutating.)
    if repo_exists and epoch is not None and not _same_path(root, BRAIN_DIR):
        drift_epochs = [e for e in (epoch, commit_epoch) if e is not None]
        drift = repo_drift(root, max(drift_epochs)) if drift_epochs else False

    return NoteAudit(
        note_path=note_path,
        project=project,
        root=root,
        mapping_ok=mapping_ok,
        repo_exists=repo_exists,
        epoch=epoch,
        n_since=n_since,
        drift=drift,
        stale=is_stale(n_since, drift),
    )


def sweep(memories_dir: Path | None = None) -> list[NoteAudit]:
    """Audit every `*-state.md` note under `memories_dir` (default MEMORIES_DIR),
    sorted by filename for stable ordering (the cap then bites deterministically)."""
    md = memories_dir if memories_dir is not None else MEMORIES_DIR
    return [audit_note(p) for p in sorted(md.glob(f"*{_STATE_SUFFIX}"))]


def select_for_refresh(
    stale: list[NoteAudit], cap: int
) -> tuple[list[NoteAudit], list[NoteAudit]]:
    """Split the stale notes into (to_refresh, skipped_over_cap). cap < 0 means
    no cap; the input order (filename-sorted from sweep) decides who wins a slot."""
    if cap < 0:
        return list(stale), []
    return stale[:cap], stale[cap:]


# --- needsUser proposals (doctrine: loops write proposals/) ------------------

def needs_user_slug(note_path: Path) -> str:
    """The <project> slug used in the needsUser filename."""
    name = note_path.name
    return name[: -len(_STATE_SUFFIX)] if name.endswith(_STATE_SUFFIX) else note_path.stem


def needs_user_path(note_path: Path, day: str | None = None) -> Path:
    """`proposals/needs-user-<UTCdate>-<note>.md` — the doctrine fallback path
    (autonomous-loops-doctrine names no needsUser location, only proposals/)."""
    d = day or _utc_date()
    return PROPOSALS_DIR / f"needs-user-{d}-{needs_user_slug(note_path)}.md"


def _render_needs_user(note_path: Path, reason: str, detail: str, day: str) -> str:
    slug = needs_user_slug(note_path)
    return (
        "---\n"
        f"name: needs-user-{day}-{slug}\n"
        f'description: "freshness-audit needs a human decision on '
        f'memories/{note_path.name} ({reason})"\n'
        "metadata:\n"
        "  type: proposal\n"
        "---\n\n"
        "**Freshness auditor needs a human decision.**\n\n"
        f"- **Note:** memories/{note_path.name}\n"
        f"- **Reason:** {reason}\n"
        f"- **Detected:** {_utc_iso()} by brain-freshness-audit\n\n"
        f"{detail.strip()}\n\n"
        "Resolve by reviewing the note and taking the action above, then delete "
        "this proposal.\n"
    )


def write_needs_user(
    note_path: Path, reason: str, detail: str, *, commit: bool = True
) -> Path:
    """Write a needsUser proposal for `note_path` and (by default) land it. The
    on-disk markdown is the durable artifact; commit/push are best-effort."""
    day = _utc_date()
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    path = needs_user_path(note_path, day)
    path.write_text(_render_needs_user(note_path, reason, detail, day), encoding="utf-8")
    if commit:
        _land([path], f"freshness-audit: needs-user ({reason}) for {note_path.name}")
    return path


def has_pending_needs_user(note_path: Path) -> bool:
    """True when an unresolved needsUser proposal for this note's slug already
    exists (any date). The date shape is anchored (`????-??-??`) so slug `bar`
    is NOT matched by a `needs-user-<date>-foo-bar.md` proposal for a different
    note (`needs-user-*-bar.md` would collide; this glob does not)."""
    slug = needs_user_slug(note_path)
    return any(PROPOSALS_DIR.glob(f"needs-user-????-??-??-{slug}.md"))


def maybe_emit_needs_user(note_path: Path, reason: str, detail: str) -> Path | None:
    """Emit a needsUser proposal unless one for this note is already pending
    (any date) — so a still-unresolved mapping failure isn't re-filed every run.
    Returns the written path, or None when suppressed as a duplicate."""
    if has_pending_needs_user(note_path):
        return None
    return write_needs_user(note_path, reason, detail)


def _land(paths: list, message: str) -> bool:
    """Commit `paths` under the brain repo and push. commit_paths never raises;
    push is best-effort (the commit is the durable artifact). Returns True when
    the commit succeeded (committed or a clean no-op)."""
    ok, _committed, _err = commit_paths(paths, message)
    if ok and sync.enabled():
        sync.push()
    return ok


# --- ACTOR phase -------------------------------------------------------------

def build_prompt(na: NoteAudit) -> str:
    """The `claude -p` instruction for a single note's refresh agent. Names the
    exact note, the required frontmatter + section shape, the evidence to ground
    in, the auto-land step, and the needsUser escape hatch for contradictions /
    archival candidates it must not silently paper over."""
    rel = f"memories/{na.note_path.name}"
    proposal = f"proposals/needs-user-{_utc_date()}-{needs_user_slug(na.note_path)}.md"
    if na.repo_exists:
        repo_line = (
            f"The project repo IS checked out at {na.root} — inspect its real "
            "state with `git -C ... log/status/diff` and reconcile the note to it."
        )
    else:
        repo_line = (
            f"The project repo ({na.root}) is NOT checked out here — ground the "
            "rewrite in brain evidence (recall/get_episode) alone; do not invent "
            "repo state."
        )
    reasons = []
    if na.drift:
        reasons.append("the project repo has commits newer than the note")
    if (na.n_since or 0) >= SESSION_EPISODES_BANNER:
        reasons.append(f"{na.n_since} episodes were recorded since the note was written")
    why = "; ".join(reasons) or "the note has fallen behind"

    return (
        "You are the brain freshness auditor's refresh agent. Your entire job is "
        f"to bring ONE stale state note up to date: {rel} (project "
        f"'{na.project}'). It was flagged stale because {why}.\n\n"
        "1. Read the current note to learn its exact frontmatter and section "
        "shape.\n"
        "2. Gather evidence with the brain MCP tools: recall for this project's "
        "recent episodes/summaries/memories, then get_episode on the most "
        f"relevant hits. {repo_line}\n"
        "3. Rewrite the note IN-FORMAT — same frontmatter keys (name, "
        "description, metadata.type, date, written), and the same section shape "
        "the note already uses (Task / Where the work is / Decisions / Next "
        "actions / Pointers). Update `date:` to today and `written:` to the "
        "current UTC timestamp. Keep it a distilled status brief, not a "
        "transcript. Preserve stable pointers and [[wikilinks]].\n"
        "4. Auto-land it: `git add "
        f"{rel}` then `git commit` with a message like "
        f"'freshness-audit: refresh {na.note_path.name}', then "
        "`uv run brain-sync push`.\n\n"
        "STOP-AND-ASK gate — do NOT rewrite, and instead write a needsUser "
        f"proposal at {proposal} (frontmatter name/description/metadata.type: "
        "proposal; a short body stating the note, the reason, and what you "
        "found), then `git add`/`git commit`/`brain-sync push` THAT file and "
        "leave the state note unchanged — when either holds:\n"
        "  - the evidence CONTRADICTS the note in a way you cannot confidently "
        "reconcile (conflicting facts, unclear which is current); or\n"
        "  - the project looks FINISHED or ABANDONED (an archival candidate) — a "
        "human should decide whether to archive the note rather than have you "
        "keep refreshing a dead project.\n"
        f"If a needsUser proposal for this note already exists ({proposal} or any "
        f"proposals/needs-user-*-{needs_user_slug(na.note_path)}.md), STOP "
        "immediately — do not rewrite the note and do not file another proposal.\n"
        "Land exactly one artifact: either the refreshed note or the needsUser "
        "proposal, never both."
    )


def refresh_note(na: NoteAudit, *, claude: str, timeout_s: float, log=_log) -> int:
    """Spawn ONE refresh agent for a stale note and wait, bounded. Mirrors
    autoreflect's child hygiene: BRAIN_NO_AUTOREFLECT=1 on the child (so its
    spawned MCP server never triggers its own reflect), its own process group,
    and a process-group kill (kill_group, reused from autoreflect) on timeout or
    abnormal exit so no grandchild survives to mutate BRAIN_DIR after we move on."""
    cmd = [
        claude, "-p", build_prompt(na),
        "--model", "opus",
        "--permission-mode", "acceptEdits",
        "--allowedTools", *AUDIT_ALLOWED_TOOLS,
    ]
    env = {**os.environ, "BRAIN_NO_AUTOREFLECT": "1"}
    proc = subprocess.Popen(cmd, cwd=str(BRAIN_DIR), env=env, start_new_session=True)
    try:
        rc = proc.wait(timeout=timeout_s)
        log(f"refresh {na.note_path.name}: exit {rc}")
        return rc
    except subprocess.TimeoutExpired:
        log(f"refresh {na.note_path.name} exceeded {timeout_s:.0f}s — killing process group")
        kill_group(proc)
        return 1
    except BaseException:
        # Any other abnormal exit must not leave the detached group running.
        kill_group(proc)
        raise


# --- orchestration -----------------------------------------------------------

def _dry_run_report(audits: list[NoteAudit], cap: int) -> None:
    stale_all = [a for a in audits if a.stale]
    pending_paths = {
        a.note_path for a in stale_all if has_pending_needs_user(a.note_path)
    }
    stale = [a for a in stale_all if a.note_path not in pending_paths]
    mapping_failures = [a for a in audits if not a.mapping_ok]
    to_refresh, skipped = select_for_refresh(stale, cap)
    _log(
        f"DRY RUN — scanned {len(audits)} state note(s); {len(stale_all)} stale "
        f"({len(pending_paths)} needs-user pending), {len(mapping_failures)} "
        f"mapping-failure(s); cap={cap}. Spawning nothing."
    )
    for a in audits:
        repo = "repo:present" if a.repo_exists else "repo:absent"
        ev = f"episodes_since={a.n_since} drift={a.drift}"
        if not a.mapping_ok:
            _log(f"  {a.note_path.name}: MAPPING-FAILURE -> would emit {needs_user_path(a.note_path).name}")
        elif a.note_path in pending_paths:
            _log(f"  {a.note_path.name}: STALE ({ev}; {repo}) -> needs-user pending, would skip")
        elif a in to_refresh:
            _log(f"  {a.note_path.name}: STALE ({ev}; {repo}) -> would refresh")
        elif a in skipped:
            _log(f"  {a.note_path.name}: STALE ({ev}; {repo}) -> OVER CAP, would skip")
        else:
            _log(f"  {a.note_path.name}: fresh ({ev}; {repo})")


def run(argv: list[str] | None = None) -> int:
    """Console entry. `--dry-run` prints the plan and returns without locking,
    writing, or spawning. Otherwise: take the single-flight flock, file needsUser
    proposals for mapping failures, then spawn one capped, serialized refresh
    agent per stale note."""
    args = list(sys.argv[1:] if argv is None else argv)
    cap = max_refresh()
    audits = sweep()

    if "--dry-run" in args:
        _dry_run_report(audits, cap)
        return 0

    stale_all = [a for a in audits if a.stale]
    # A stale note with a still-unresolved needsUser proposal is NOT a refresh
    # target — a human owes a decision on it, and re-refreshing would either
    # re-file the same proposal or paper over the flagged contradiction. Drop it
    # before selection so it also never consumes a scarce cap slot.
    pending_paths = {
        a.note_path for a in stale_all if has_pending_needs_user(a.note_path)
    }
    pending = [a for a in stale_all if a.note_path in pending_paths]
    stale = [a for a in stale_all if a.note_path not in pending_paths]
    mapping_failures = [a for a in audits if not a.mapping_ok]
    to_refresh, skipped = select_for_refresh(stale, cap)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(AUDIT_LOCK_PATH, "w")  # held for this process's lifetime
    try:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _log("an audit is already running — skipping")
            return 0

        _log(
            f"scanned {len(audits)} note(s): {len(stale_all)} stale "
            f"({len(pending)} needs-user pending), "
            f"{len(mapping_failures)} mapping-failure(s), cap={cap}"
        )

        for a in pending:
            _log(f"needs-user pending — skipped: {a.note_path.name}")

        # Deterministic gate: mapping failures are the auditor's own needsUser.
        for a in mapping_failures:
            emitted = maybe_emit_needs_user(
                a.note_path,
                "mapping-failure",
                f"The state-note filename '{a.note_path.name}' does not yield a "
                f"usable project name (derived: '{a.project}'), so it cannot be "
                "mapped to a project repo or refreshed automatically. Rename or "
                "remove the note, or map it by hand.",
            )
            if emitted is not None:
                _log(f"needs-user (mapping-failure): {emitted.name}")

        for a in skipped:
            _log(f"over cap ({cap}) — skipped refresh this run: {a.note_path.name}")

        if not to_refresh:
            _log("no stale notes to refresh")
            return 0

        claude = resolve_claude()
        if claude is None:
            _log("claude binary not found — cannot spawn refresh agents")
            return 1

        timeout_s = refresh_timeout()
        # Serial: each agent lands its own commit before the next starts, so
        # concurrent git index.lock contention on BRAIN_DIR can't arise. Track the
        # worst agent exit so a failed refresh propagates a nonzero run status
        # (the timer surfaces it in journalctl) rather than a silent success.
        worst = 0
        for a in to_refresh:
            rc = refresh_note(a, claude=claude, timeout_s=timeout_s)
            if rc != 0:
                worst = rc
        return worst
    finally:
        lock_fh.close()  # releases the flock (clean or crash)


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
