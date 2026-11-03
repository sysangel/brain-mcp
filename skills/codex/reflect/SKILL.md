---
name: reflect
description: Consolidate recent sessions and the current Codex conversation into the hosted Brain, refresh durable memories and project state notes, and advance the reflect watermark. Use when the user invokes "$reflect" or "/reflect", asks to reflect or consolidate memory, wants work reflected back into Brain, or is about to clear a substantive session. Do not use for a single fact that belongs in the lighter remember workflow.
---

# Reflect

Run Brain's canonical consolidation workflow against the hosted brain (MCP
server `brain`; connect with `codex mcp login brain` if it asks for sign-in)
while making the current Codex session explicit. Codex conversations are not
pushed into Brain's episode store, so this session's facts only get in
through you.

## Workflow

1. Read `skills/reflect-run/SKILL.md` from the brain repository (on GitHub
   at `OWNER/brain`, or any checkout) completely before taking any
   reflection action. That is the canonical full pass; `skills/reflect/SKILL.md`
   is a Claude-only fire-and-forget launcher and is not what Codex runs.
2. Treat that file's numbered steps and guardrails as the operational source
   of truth: claim, window, digests, distill, state notes, compact, proposals,
   complete, report. Every step is a brain MCP tool call.
3. Apply the Codex adaptations below. Where they do not apply, follow the
   canonical workflow exactly.

## Codex adaptations

- Ignore the canonical skill's Claude-only `context` and `model` frontmatter.
  Work in the current Codex agent. Do not delegate merely to emulate
  `context: fork`.
- Before distilling, review the current conversation directly for durable
  decisions, corrections, preferences, project state, and next actions that
  would change future behavior.
- Apply the canonical memory bar to current-session material. Prefer
  refreshing the project's state note (`state_write`) with verifiable
  commit, test, or file evidence. Do not create a session digest or invent a
  session id unless that session exists in the brain (`session_episodes`
  returns it).
- Keep facts derivable from source code or git out of ordinary memories; use
  the state note for current evidence and point to a handoff
  (`handoff_latest`) when detailed implementation history already exists.
- Always finish with `reflect_complete` on the claim you took, even when
  nothing was worth keeping.

## Completion

Return the canonical compact report and additionally state:

- whether current Codex-session facts were captured;
- any tool call that failed or was denied.
