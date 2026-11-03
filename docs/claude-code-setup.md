# Set up your own brain for Claude Code

This guide builds a private brain memory server on your own machine and wires
it into Claude Code: the MCP server, the hooks, the skills, and a CLAUDE.md
section that tells Claude how to use it. When you finish, every Claude Code
session will:

- open with the current project's state note already injected,
- see relevant memories next to each prompt you type,
- be able to search past sessions and memories with `recall`, and
- be able to save new durable facts with `remember`.

The default setup is fully local: a SQLite index over a git repo of markdown
files, served to Claude Code over stdio. No cloud account, no server, no
sign-in. It works on Windows, macOS and Linux.

Throughout this guide, `<repo-path>` is the absolute path of your clone, for
example `~/src/brain-mcp` on macOS or Linux, or `C:/Users/<you>/src/brain-mcp`
on Windows. Use forward slashes on Windows too. Claude Code runs hook commands
through Git Bash there, and forward slashes work in every place this guide
uses a path.

## Contents

1. [How the local brain works](#1-how-the-local-brain-works)
2. [Prerequisites](#2-prerequisites)
3. [Clone and install](#3-clone-and-install)
4. [Try it on the example store](#4-try-it-on-the-example-store)
5. [Build your real index](#5-build-your-real-index)
6. [Register the MCP server](#6-register-the-mcp-server)
7. [Install the skills](#7-install-the-skills)
8. [Add the hooks](#8-add-the-hooks)
9. [Schedule a catch-up ingest (optional)](#9-schedule-a-catch-up-ingest-optional)
10. [Add the CLAUDE.md block](#10-add-the-claudemd-block)
11. [Verify it works](#11-verify-it-works)
12. [Troubleshooting](#12-troubleshooting)
13. [Upgrade later to Supabase or a hosted server](#13-upgrade-later-to-supabase-or-a-hosted-server)
14. [What differs from a hosted setup](#14-what-differs-from-a-hosted-setup)

## 1. How the local brain works

Your clone is the store. Memories live as markdown files in
`<repo-path>/memories/`, session digests in `summaries/`, and recorded session
turns in `episodes/`. Each write is committed to the clone's own git history.
`cache/brain.db` is a derived search index that you can delete and rebuild at
any time.

Sessions are recorded by `brain-ingest`, which reads Claude Code's own
transcript files under `~/.claude/projects` (and `~/.claude-work/projects` if
it exists). Nothing pushes sessions anywhere. The index is refreshed in three
ways:

- when the MCP server starts and the index is more than an hour old,
- in the background after each session-start hook, with the same one-hour
  gate, and
- whenever you run `brain-ingest` yourself or on a schedule (step 9).

The local MCP server exposes three tools: `recall`, `get_episode` and
`remember`.

## 2. Prerequisites

1. **Git.** Check with `git --version`. On Windows, Git for Windows also
   provides the Git Bash shell that Claude Code uses for hooks.
2. **uv.** Install it from <https://docs.astral.sh/uv/>, then check with
   `uv --version`.
3. **Python 3.12 or newer.** uv can supply it. Run `uv python install 3.12`
   if `uv python find 3.12` finds nothing.
4. **Claude Code.** Check with `claude --version`.
5. **Disk space.** About 600 MB for the local embedding model, downloaded on
   first use into `<repo-path>/cache/fastembed/`.
6. **sqlite3 CLI (only for `/reflect-local`).** The consolidation skill in
   step 7 queries the index with the `sqlite3` command. macOS ships it. On
   Linux install your distribution's `sqlite3` package. On Windows run
   `winget install SQLite.SQLite` or put `sqlite3.exe` on your `PATH`.

## 3. Clone and install

1. Clone the repo and install its dependencies.

   ```sh
   git clone https://github.com/sysangel/brain-mcp.git ~/src/brain-mcp
   cd ~/src/brain-mcp
   uv sync
   ```

2. Detach the clone from the public repo's `main`. The brain commits your
   memories and recorded sessions to this clone's `main` branch, and its git
   sync feature pushes to whatever remote is named `origin`. Renaming the
   remote keeps your data local and turns sync off.

   ```sh
   git remote rename origin upstream
   git branch --unset-upstream
   ```

   You can still pull code updates later with `git pull upstream main`.
   Your memory commits touch only `memories/`, `summaries/` and `episodes/`,
   so they merge cleanly with upstream code changes.

   If you want an offsite backup or a second machine, create a **private**
   repo and add it as `origin`. Sync then pushes there automatically. See
   step 7 of [Path 1 in the README](../README.md#path-1-local).

## 4. Try it on the example store

This builds a throwaway index over the synthetic data in `examples/`, so you
can confirm the install before touching your own transcripts. Pointing
`BRAIN_CLAUDE_PROJECTS` at an empty directory keeps your real sessions out.

macOS or Linux:

```sh
cd ~/src/brain-mcp
BRAIN_DIR=$PWD/examples BRAIN_CLAUDE_PROJECTS=$(mktemp -d) \
  uv run brain-ingest --full
BRAIN_DIR=$PWD/examples uv run brain-recall "postgres backup"
```

Windows (PowerShell):

```powershell
cd ~/src/brain-mcp
New-Item -ItemType Directory -Force ..\empty-projects | Out-Null
$env:BRAIN_DIR = "$PWD\examples"
$env:BRAIN_CLAUDE_PROJECTS = "..\empty-projects"
uv run brain-ingest --full
uv run brain-recall "postgres backup"
Remove-Item Env:BRAIN_DIR, Env:BRAIN_CLAUDE_PROJECTS
```

On Windows, `BRAIN_CLAUDE_PROJECTS` must not contain a drive letter. The
variable is split on `:`, so `C:\Users\...` becomes two bogus paths. A
relative path works, as shown. See [Troubleshooting](#12-troubleshooting).

The first run downloads the embedding model, so it takes a minute or two.
Expect `memories_total: 3` and `summaries_total: 1`, and a recall result list
whose top hit is the `postgres-nightly-backup` memory.

The example index lives in `examples/cache/` and is ignored by git. Delete it
whenever you like.

## 5. Build your real index

1. Decide whether any projects must stay out of git. `brain-ingest` writes
   each recorded session turn to `episodes/` in your clone and commits it.
   To keep a project's turns out of git (they stay searchable in the index),
   set `BRAIN_EPISODE_EXCLUDE` to a comma-separated list of substrings of
   that project's working directory. `BRAIN_DIGEST_EXCLUDE` also keeps them
   out of session digests. Put the same variables in the MCP server's
   environment in step 6 so later ingests honor them.

2. Make sure `BRAIN_DIR` and `BRAIN_CLAUDE_PROJECTS` are unset, then run the
   first ingest. With `BRAIN_DIR` unset, the store is the clone itself.

   ```sh
   cd ~/src/brain-mcp
   uv run brain-ingest
   ```

   This indexes every Claude Code transcript under `~/.claude/projects`.
   With a lot of history it can take several minutes. Later runs are
   incremental and usually finish in seconds.

3. Search it.

   ```sh
   uv run brain-recall "something you worked on last week"
   ```

Keep `BRAIN_DIR` unset from here on. The session-start hook starts its
background refresh with `uv run --directory <BRAIN_DIR>`, so the store and
the code must be the same directory.

## 6. Register the MCP server

Register it at user scope so every project sees it. The default scope is the
current project only.

```sh
claude mcp add brain --scope user -e BRAIN_NO_AUTOREFLECT=1 -- \
  uv run --directory <repo-path> brain-server
```

On Windows, replace `<repo-path>` with the full forward-slash path, for
example `C:/Users/<you>/src/brain-mcp`. Do not rely on `~` here.

`BRAIN_NO_AUTOREFLECT=1` stops the server from launching a headless
`claude -p` consolidation run on its own. By default that run uses the
hosted-only `/reflect-run` skill, which cannot work against a local store.
Run `/reflect-local` by hand instead (step 7). If you later want automatic
runs, replace the variable with `-e BRAIN_REFLECT_SKILL=/reflect-local`.

Add `-e BRAIN_EPISODE_EXCLUDE=...` and `-e BRAIN_DIGEST_EXCLUDE=...` here if
you chose exclusions in step 5.

The equivalent JSON entry, if you prefer to edit config by hand, goes under
`mcpServers` at the top level of `~/.claude.json` (user scope), or in a
project's `.mcp.json`:

```json
{
  "mcpServers": {
    "brain": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "<repo-path>", "brain-server"],
      "env": { "BRAIN_NO_AUTOREFLECT": "1" }
    }
  }
}
```

You can also pass that object to `claude mcp add-json brain '<json>' --scope
user`.

To give a client read-only access, register `brain-server-readonly` instead.
It exposes only `recall` and `get_episode`.

## 7. Install the skills

The repo's `skills/` folder holds six skills. Only one of them works against
a local store:

| Skill in `skills/` | Local mode |
| --- | --- |
| `reflect-local` | Use it. This is the local consolidation pass. |
| `checkpoint` | Needs hosted-only tools. Use the local version below. |
| `catchup` | Needs hosted-only tools. Use the local version below. |
| `handoff` | Needs hosted-only tools. No local version; `/checkpoint` covers the same need. |
| `reflect`, `reflect-run` | Hosted-only. Use `/reflect-local` instead. |

1. Copy `reflect-local` into your skills folder.

   ```sh
   mkdir -p ~/.claude/skills
   cp -r <repo-path>/skills/reflect-local ~/.claude/skills/
   ```

   Copy it rather than symlinking. Symlinks on Windows need Developer Mode.

2. Point the skill at your clone. It assumes the store is `~/src/brain`. If
   your clone lives anywhere else, replace that path.

   ```sh
   sed -i.bak 's#~/src/brain#<repo-path>#g' ~/.claude/skills/reflect-local/SKILL.md
   rm ~/.claude/skills/reflect-local/SKILL.md.bak
   ```

   For a clone at `~/src/brain-mcp`, the replacement is `~/src/brain-mcp`.

3. Create `~/.claude/skills/checkpoint/SKILL.md` with this content. Replace
   `<repo-path>` throughout.

   ````markdown
   ---
   name: checkpoint
   description: Fast pre-/clear state save for the local brain. Rewrite the
     current project's state note in <repo-path>/memories/. Use before /clear,
     when asked to "checkpoint", or to save resume state without a full reflect.
   ---

   # checkpoint (local brain)

   Save the thread so a fresh session can pick it up. Run inline in the main
   conversation, never as a background agent: a checkpoint runs moments
   before /clear, and a background agent dies with the session.

   1. **Find the project name.** Use the basename of
      `git rev-parse --show-toplevel` in the session's working directory, or
      the working directory's basename outside git. The SessionStart hook
      looks up exactly `memories/<project>-state.md`, so the name must match.

   2. **Overwrite** `<repo-path>/memories/<project>-state.md` wholesale with
      the Write tool. Never append. Use this frontmatter:

      ```
      ---
      name: <project>-state
      description: <current one-line status, 150 characters or less>
      metadata:
        type: project
      date: <today, YYYY-MM-DD, UTC>
      written: <current UTC time, YYYY-MM-DDTHH:MM:SSZ>
      ---
      ```

      The SessionStart hook trusts `written` for freshness, so never omit or
      backdate it. Body, about 25 lines at most: one plain line saying what
      the project is, then **Task**, **State (with evidence)** where every
      claim carries a commit sha, verdict or `file:line`, **Decisions** (with
      the why), **Next actions** (most important first), and **Pointers**
      (paths and commands verified this session). Absolute dates only. No
      secrets, credentials or long transcript quotes.

   3. **Commit and re-index.**

      ```
      git -C <repo-path> add memories/<project>-state.md
      git -C <repo-path> commit -m "checkpoint: <project>" -- memories/<project>-state.md
      uv run --no-sync --directory <repo-path> brain-ingest
      ```

   4. **Touch nothing else.** No other memories, no digests, and never write
      `cache/last-reflect`. A checkpoint is not a reflection.

   5. **Report in five lines or fewer:** the project, the gist of the note,
      and the next action it records.
   ````

4. Create `~/.claude/skills/catchup/SKILL.md` with this content. Replace
   `<repo-path>` throughout.

   ````markdown
   ---
   name: catchup
   description: Deep-recovery resume from the local brain when the SessionStart
     brief is missing, flagged STALE, or contradicts the repo. One subagent reads
     the state note, recalls digests and episodes, cross-checks the repo, and
     returns a short brief. Use for "catch up", "/catchup", "deep resume".
   ---

   # catchup (local brain)

   Resolve the project from the `/catchup <project>` argument, else from the
   repo basename of the working directory. If it is ambiguous, ask once.

   Spawn ONE subagent with this brief, substituting `<project>`:

   > Reconstruct the working state of `<project>` from the local brain. Load
   > the brain MCP tools with ToolSearch
   > `select:mcp__brain__recall,mcp__brain__get_episode` if they are deferred.
   >
   > 1. Run `uv run --no-sync --directory <repo-path> brain-ingest` once so the
   >    latest sessions are indexed. On failure, note it and continue.
   > 2. Read `<repo-path>/memories/<project>-state.md` if it exists. If not, say
   >    so and rely on digests and episodes.
   > 3. `recall("<project> state decisions next steps")`, then
   >    `recall("<project>", scope="summaries")`, then
   >    `recall("<project>", scope="episodes")`. Fetch the top 1 to 3 hits in
   >    full with `get_episode(id)`.
   > 4. Compare against the project repo's `git log -5 --oneline` and
   >    `git status`. If the repo moved past the brain, trust the repo and flag
   >    the drift.
   > 5. Verify every path and command you cite still exists.
   >
   > Return only a brief of about 400 words: **Task**, **State** (with
   > evidence), **Decisions already made**, **Single next action**,
   > **Verified pointers**, and **Staleness warnings** (omit if clean). No raw
   > recall output or transcripts.

   Relay the brief and act on its single next action. Do not repeat the
   subagent's retrieval in the main window.
   ````

5. Restart Claude Code, or start a new session, so it picks up the new
   skills.

## 8. Add the hooks

Add these two entries under `hooks` in `~/.claude/settings.json`. If the
file already has a `hooks` object, merge them in rather than replacing it.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear",
        "hooks": [
          {
            "type": "command",
            "timeout": 10,
            "command": "uv run --no-sync --directory <repo-path> brain-session-start"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "timeout": 5,
            "command": "uv run --no-sync --directory <repo-path> brain-hook"
          }
        ]
      }
    ]
  }
}
```

What each one does:

- **`brain-session-start`** looks for `memories/<project>-state.md`, where
  `<project>` is the basename of the session's git repo. If the note exists,
  it injects it with its write time, a count of sessions recorded since, and
  a STALE banner when the repo has moved on. If no note exists, it prints
  nothing. It then starts a detached index refresh for the next session.
- **`brain-hook`** runs a fast full-text search of your memories for each
  prompt and injects the strongest matches. It stays quiet when nothing
  matches well.

Both hooks never block a session and always exit 0. Set `BRAIN_HOOK_DEBUG=1`
to see their errors on stderr.

There is no Stop or SessionEnd hook in local mode. Hosted setups use
`brain-push` there, which sends transcripts to a server. Locally the
transcripts are already on disk and `brain-ingest` reads them directly.

If Claude Code cannot find `uv` when it runs hooks (common when it is
launched from a GUI with a short `PATH`), use the full path to the binary:
the output of `command -v uv`, or `(Get-Command uv).Source` in PowerShell.

## 9. Schedule a catch-up ingest (optional)

The automatic refreshes only run when the index is more than an hour old,
and only when a session or the server starts. A scheduled ingest keeps recall
current even on days you leave one session open for hours.

macOS or Linux (cron, hourly). Use the full path to `uv`, because cron's
`PATH` is short:

```sh
crontab -e
# add:
0 * * * * cd <repo-path> && /full/path/to/uv run --no-sync brain-ingest >/dev/null 2>&1
```

Windows (Task Scheduler, hourly), from PowerShell:

```powershell
$uv = (Get-Command uv).Source
$action = New-ScheduledTaskAction -Execute $uv `
  -Argument 'run --no-sync --directory "<repo-path>" brain-ingest'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName "brain-ingest" -Action $action -Trigger $trigger `
  -Description "Index new Claude Code sessions into the local brain"
```

A console window may flash briefly each hour. Remove the task with
`Unregister-ScheduledTask -TaskName brain-ingest`.

Ingest takes a lock, so a scheduled run that overlaps a hook refresh or a
server startup simply skips.

## 10. Add the CLAUDE.md block

Paste this into `~/.claude/CLAUDE.md`, replacing `<repo-path>`. It tells
Claude when to search the brain, when to write to it, and how to resume.

```markdown
## The brain (memory)

The brain is my local memory MCP server (`brain`). It runs from my clone at
`<repo-path>`. Memories are markdown files in `<repo-path>/memories/`,
committed to that clone's git history. `cache/brain.db` is a rebuildable
search index.

MCP tools (if deferred, load with ToolSearch
`select:mcp__brain__recall,mcp__brain__get_episode,mcp__brain__remember`):

- `recall(query, k, scope, project, since, until)`: hybrid full-text and
  vector search. scope is `all` | `episodes` | `memories` | `summaries`.
  Use it before asking me something I may have told you in an earlier
  session, and when picking up unfamiliar work in a known project.
- `get_episode(id)`: full text for an `ep_`/`mem_`/`sum_` hit. Snippets are
  too thin to act on; fetch the top hits.
- `remember(fact, type, name, project, session)`: writes and commits a new
  memory. It refuses near-duplicates.

If the brain tools are missing, tell me. Do not guess at past context.

## Writing memories

When a durable fact surfaces mid-session (a preference of mine, feedback on
how to work, a project decision or constraint, a useful reference), save it
right then with `remember`.

- Set `type` (user | feedback | project | reference) and `project` (the repo
  basename). Pass `session` when `$CLAUDE_CODE_SESSION_ID` is available.
- The bar: would knowing this change what you do in a future session?
- Never store secrets, credentials, anything derivable from the code or git
  history, or long transcript quotes.
- To correct or remove a memory, edit or delete its file in
  `<repo-path>/memories/`, commit, then run
  `uv run --no-sync --directory <repo-path> brain-ingest`.
- Do not write memory files into Claude Code's auto-memory directory
  (`~/.claude/projects/*/memory/`). Every new fact goes through `remember`.

## Resuming work

Routine resume is automatic: the SessionStart hook injects the project's
state note (`memories/<project>-state.md`) stamped with freshness. If that
brief is present and clean, act on its "Next actions" without re-running
retrieval. If there is no brief, it carries a STALE banner, or it
contradicts the repo, run /catchup.

## Ending a session

Before /clear after substantive work, run /checkpoint. It rewrites this
project's state note so the next session opens with it. Every few days, or
when I ask, run /reflect-local to write session digests and distill memories.
```

## 11. Verify it works

1. **The server is registered and connects.**

   ```sh
   claude mcp list
   ```

   `brain` should show as connected. Inside a session, `/mcp` lists it with
   the tools `recall`, `get_episode` and `remember`.

2. **The prompt hook answers.** Pick a word that appears in one of your
   memories, or skip this until you have written one in check 4.

   macOS or Linux:

   ```sh
   echo '{"cwd":"'"$PWD"'","session_id":"test","prompt":"what did we decide about <topic>"}' \
     | uv run --no-sync --directory <repo-path> brain-hook
   ```

   Windows (PowerShell):

   ```powershell
   @{cwd="$PWD"; session_id="test"; prompt="what did we decide about <topic>"} |
     ConvertTo-Json -Compress |
     uv run --no-sync --directory <repo-path> brain-hook
   ```

   A match prints JSON with `hookSpecificOutput.additionalContext`. No match
   prints nothing, which is also correct.

3. **Recall finds your history.** In a Claude Code session, ask: "Use the
   brain to recall what I worked on yesterday." Claude should call `recall`
   and quote real past sessions.

4. **Writes land in git.** Ask Claude to remember a harmless test fact, then
   check:

   ```sh
   git -C <repo-path> log --oneline -1
   uv run --directory <repo-path> brain-recall "<words from the fact>"
   ```

   The last commit should read `remember: <slug>`, and recall should return
   the new `mem_` id near the top. Delete the test file from `memories/`,
   commit, and re-run `brain-ingest` when you are done.

5. **The session-start brief appears.** In a project repo, run `/checkpoint`,
   then `/clear`. The new session should open with "Project state from
   brain (memories/<project>-state.md, written ...)". To test the hook
   directly, pipe `{"cwd":"<project-dir>","source":"startup"}` into
   `brain-session-start` the same way as check 2.

## 12. Troubleshooting

**The MCP server hangs on Windows and tools never respond.** Older versions
of the server could deadlock on Windows stdin. Current `main` fixes this.
Update with `git -C <repo-path> pull upstream main`, then restart Claude Code
or reconnect the server from `/mcp`. Sessions that were already running keep
the old server process until then.

**`BRAIN_CLAUDE_PROJECTS` with a Windows path indexes nothing.** The variable
is a colon-separated list, so `C:\Users\me\x` is split into `C` and
`\Users\me\x`. On Windows, leave it unset; the default
(`~/.claude/projects` plus `~/.claude-work/projects`) is right for normal
use. When you need an override, such as the empty directory in step 4, use a
relative path (`..\empty-projects`) or a path without a drive letter on the
current drive (`\Users\me\empty-projects`). Only one directory can be given
that way.

**The session-start hook prints nothing.** That is by design when no state
note exists for the project. The note name is the basename of the git
repository the session runs in, so a session in `~/src/acme-api` looks for
`memories/acme-api-state.md`. Run `/checkpoint` once to create it. Set
`BRAIN_HOOK_DEBUG=1` to see errors.

**The brief says "local memory commit(s) not yet pushed".** The clone still
tracks a remote branch. Run `git branch --unset-upstream` in `<repo-path>`,
or add a private remote as described in step 3.

**Recall misses recent sessions.** The index refreshes at most hourly on its
own. Run `uv run --directory <repo-path> brain-ingest`, or set up step 9. To
rebuild the whole index, delete `<repo-path>/cache/brain.db` and run
`brain-ingest --full`.

**Hooks fail with "uv: command not found".** Use the full path to `uv` in the
hook commands (step 8).

**The brain uses Postgres when you expected SQLite.** If `DATABASE_URL` is
exported in your environment for another project, the brain picks the
Postgres backend. Set `BRAIN_BACKEND=sqlite` in the MCP server's `-e` flags
and in your shell.

**A Hugging Face warning about symlinks appears on Windows.** It is harmless.
The model cache still works without symlinks, using a little more disk.

## 13. Upgrade later to Supabase or a hosted server

You can move to a shared store without losing what you built. Two paths
exist, both documented in the repo:

- **Supabase store, local server**: one Postgres database, the same stdio
  server on each machine. See
  [Path 2 in the README](../README.md#path-2-supabase-store-local-server).
- **Self-hosted HTTP server**: one server with Google sign-in for MCP and a
  device token for hooks and session recording. See
  [Path 3 in the README](../README.md#path-3-self-hosted-server),
  [docs/supabase.md](supabase.md) (especially section 7, sessions and hooks,
  and section 8, cutover from the local brain), and
  [docs/connecting-agents.md](connecting-agents.md).

`brain-pg-load` imports your local store's committed memories and digests
into Postgres. After a move to the hosted server, swap the pieces of this
guide for their hosted counterparts:

| Local (this guide) | Hosted |
| --- | --- |
| stdio `brain-server` | `claude mcp add --transport http brain <server-url>/mcp` |
| `brain-session-start` hook | `brain-remote-session-start` |
| `brain-hook` | `brain-remote-recall-hook` |
| transcripts read by `brain-ingest` | `brain-push --hook` at Stop and SessionEnd, plus a periodic `brain-push` |
| local `checkpoint` and `catchup` skills | the repo's `skills/checkpoint` and `skills/catchup` |
| `/reflect-local` | the repo's `skills/reflect` and `skills/reflect-run` |
| none | `skills/handoff` |

## 14. What differs from a hosted setup

If a teammate runs the hosted version, a few of their pieces have no local
equivalent. You are not missing anything you need.

- **`brain-push` and device tokens.** These send sessions to the HTTP
  server's device routes. Locally, `brain-ingest` reads the transcripts from
  disk instead, so you have no push hook and no token.
- **Google sign-in through `/mcp`.** A local stdio server needs no sign-in.
- **Hosted-only MCP tools** (`state_get`, `state_write`, `handoff_write`,
  `reflect_claim` and the rest). The local server has only `recall`,
  `get_episode` and `remember`. The local `checkpoint` skill writes the state
  note file directly, and `/reflect-local` writes digests and memories as
  files.
- **`/handoff`.** Left out. `/checkpoint` plus the automatic session-start
  brief covers the same need.
- **"Editing the markdown changes nothing."** That rule applies to a hosted
  brain, where Postgres is the source of truth. Locally the markdown is the
  source of truth: edit a memory file, commit it, and run `brain-ingest`.
