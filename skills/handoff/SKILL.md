---
name: handoff
description: Relay session state across /clear. At the end of a session, /handoff
  writes a briefing note to the brain; at the start of a fresh session, /handoff
  reads the latest note and picks the work back up. Use when asked to "hand off",
  "write a handoff", or "handoff resume".
---

# handoff

Session-state relay across /clear, for keeping sessions fresh without losing
the thread. Notes live in `~/src/brain/handoffs/` (git-tracked — text-in-git
rule; they will sync to a remote once git-sync ships, so write nothing you
wouldn't commit).

## Mode selection

- Fresh session — this invocation is the first substantive message (right
  after /clear) → **resume mode**.
- Real work exists above in the conversation → **write mode**.
- Explicit args always win: `/handoff write`, `/handoff resume`, or
  `/handoff resume <topic or date>`.

## Write mode

1. Distill THIS session into a briefing a context-free session can act on —
   a briefing, not a transcript:
   - **Task** — what we were working on and why.
   - **State** — done / in-progress / blocked, with evidence (verdicts, test
     results, commits).
   - **Decisions** — calls the user made this session. The next session must
     not re-litigate these.
   - **Next steps** — the single concrete first action, then the ordered queue.
   - **Pointers** — exact file paths, commands, branches, spec/doc locations.
   - **Open questions** — only the genuinely unresolved.
2. Get the timestamp (`date '+%Y-%m-%d-%H%M'`) and write to
   `~/src/brain/handoffs/<timestamp>-<task-slug>.md` with frontmatter:
   `date` (ISO), `project` (repo/dir the work lives in), `description`
   (one line). Names sort chronologically — that is the "latest" mechanism.
3. Commit: `git -C ~/src/brain add handoffs/ && git -C ~/src/brain commit -m "handoff: <task-slug>"`.
4. Tell the user the note is written and they can /clear. Then END the reply
   with the exact kickoff for the next session in a fenced code block, so the
   user can copy-paste it verbatim without thinking:
   - default: `/handoff`
   - if this note might not be the newest by then: `/handoff resume <task-slug>`
   - if the next session should skip straight to a specific action, put that
     full prompt in the block instead (e.g. `/handoff` on one line, then the
     one-line instruction).
   The block is the deliverable of write mode — a handoff that doesn't tell
   the user what to paste next is incomplete.

## Resume mode

1. Newest file in `~/src/brain/handoffs/` (by name) is the latest handoff;
   read it. If args name a topic or date, find that one instead.
2. Brief the user: task, state, decisions already made, and lead with the
   first concrete next action. Flag the note's age if it is more than ~2 days
   old — the world may have moved.
3. If the note references specs, plans, or files, verify the key ones still
   exist before acting on them; `mcp__brain__recall` can fill gaps the note
   doesn't cover.
4. If the first next step is unambiguous, propose it and get to work;
   otherwise present the queue and let the user pick.
