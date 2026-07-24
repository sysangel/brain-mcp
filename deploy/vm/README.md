# VM deployment

User-level systemd units for a resident brain on an always-on box (a small
VPS/droplet). This is the split-host half of the design: an emitting
workstation writes episodes + memories and pushes them; this box pulls, indexes
from the committed episode files (never from its own transcripts), and runs the
maintenance loops (debt-gated reflect + freshness audit).

Prereqs (one-time provisioning): `uv` + `claude` on `PATH` (e.g. `~/.local/bin`),
a read-write deploy key for your brain repo, the repo cloned at `~/src/brain-mcp`
(`uv sync` run), the skills repo symlinked at `~/.claude/skills`,
`CLAUDE_CODE_OAUTH_TOKEN` in `~/.config/claude.env` (mode 0600),
`loginctl enable-linger <your-user>` (so user units run without an active
login), and `sqlite3`.

Install / update units (`<vm>` is your SSH host):

    scp deploy/vm/brain-reflect.{service,timer} <vm>:.config/systemd/user/
    scp deploy/vm/brain-freshness-audit.{service,timer} <vm>:.config/systemd/user/
    ssh <vm> 'systemctl --user daemon-reload && systemctl --user enable --now brain-reflect.timer brain-freshness-audit.timer'

The freshness auditor (`brain-freshness-audit.timer`, daily) is the first
resident maintenance loop: it ingests, then sweeps `memories/*-state.md`,
reuses the SessionStart STALE rule to find notes that have fallen behind their
project, and spawns one Opus refresh agent per stale note to rewrite it
in-format and auto-land it. Cost is bounded by `BRAIN_AUDIT_MAX_REFRESH`
(default 3) refresh agents per run; contradictions, archival candidates, and
mapping failures become `proposals/needs-user-<date>-<note>.md` for a human.
Dry-run smoke check (read-only, spawns nothing):

    ssh <vm> 'uv run --directory ~/src/brain-mcp brain-freshness-audit --dry-run'

First-time seeding (before enabling the timer): run the initial episode
ingest manually — embedding the full backfill can take hours on a 2-vCPU box
and would blow the unit's `TimeoutStartSec`:

    ssh <vm> 'BRAIN_INGEST_SOURCE=episodes BRAIN_EMBED_THREADS=2 nohup uv run --directory ~/src/brain-mcp brain-ingest > ~/ingest-seed.log 2>&1 &'

Also seed `cache/last-reflect` from the workstation's value first, or the box's
first `--if-debt` run sees every historical session as unconsolidated debt and
fires one giant catch-up reflect:

    scp ~/src/brain-mcp/cache/last-reflect <vm>:src/brain-mcp/cache/last-reflect

Writer discipline: this box owns consolidation writes (reflect: state notes,
summaries, digests). The workstation owns session-time writes (remember,
episode emission) and must NOT reflect — its MCP server config sets
`BRAIN_NO_AUTOREFLECT=1` (in the Claude Code config's `mcpServers.brain.env`).

Logs: `journalctl --user -u brain-reflect.service -n 50`.
