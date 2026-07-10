"""brain-hook: ambient auto-recall `UserPromptSubmit` hook (v1: memories only).

Reads the Claude Code hook JSON on stdin, runs the prompt through a fast
FTS5-only lexical recall over `cache/brain.db` (opened read-only), and prints
the strongest matching **memories** as `hookSpecificOutput.additionalContext`
so distilled facts appear alongside the prompt with zero user effort.

Design invariants (see specs/auto-recall-hook.md):
  * Quiet: emit nothing unless hits clear the token-overlap gate.
  * Fast: gate cheaply before touching the DB; FTS-only, no embedding.
  * Safe: never write, never trigger ingest/model-download, never exit 2.
    Any failure -> exit 0 with empty stdout (stderr only under BRAIN_HOOK_DEBUG).

Raw episodes and semantic search stay behind the `recall` MCP tool; the episode
leg is a documented v2 addition, not wired here.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from . import db
from .config import (
    HOOK_FTS_LIMIT,
    HOOK_MAX_CHARS,
    HOOK_MAX_HITS,
    HOOK_MIN_MATCH,
    HOOK_SEEN_DIR,
    HOOK_SEEN_TTL_DAYS,
)

# ~35 common 3+-letter words (deliberately generous — extra stopwords only make
# the hook quieter, its primary design goal); 1-2 letter words are already
# dropped by the length filter below. Keeps generic prompts under the 2-token floor.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "are", "was", "you", "your", "this", "that",
        "with", "have", "from", "what", "how", "why", "when", "where", "who",
        "can", "will", "would", "should", "about", "into", "out", "not", "but",
        "all", "any", "get", "got", "use", "using", "please", "let",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


def _debug(msg: str) -> None:
    if os.environ.get("BRAIN_HOOK_DEBUG") == "1":
        print(f"brain-hook: {msg}", file=sys.stderr)


def _content_tokens(text: str) -> list[str]:
    """Distinct lowercase content tokens: `[A-Za-z0-9_]+`, length >= 3, minus a
    small stopword list, order-preserved, capped at 12 (mirrors the 32-token
    cap in db._fts_or_query, tighter for a nudge)."""
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < 3 or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= 12:
            break
    return out


def _seen_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128] or "nosession"
    return HOOK_SEEN_DIR / f"{safe}.txt"


def _read_seen(session_id: str) -> set[str]:
    try:
        text = _seen_path(session_id).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line for line in text.splitlines() if line}


def _append_seen(session_id: str, keys: list[str]) -> None:
    if not keys:
        return
    try:
        HOOK_SEEN_DIR.mkdir(parents=True, exist_ok=True)
        with _seen_path(session_id).open("a", encoding="utf-8") as fh:
            for key in keys:
                fh.write(key + "\n")
    except OSError as exc:
        _debug(f"seen-write failed: {exc!r}")


def _prune_seen(now: float) -> None:
    """Opportunistically unlink seen-files older than the TTL (one iterdir)."""
    cutoff = now - HOOK_SEEN_TTL_DAYS * 86400
    try:
        entries = list(HOOK_SEEN_DIR.iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _query_memories(conn, expr: str) -> list[dict]:
    """Top HOOK_FTS_LIMIT memory rows by BM25 rank for the MATCH expression.
    Two steps: FTS gives ranked rowids, the base table gives the columns the
    gate/format need (name/description/text plus path for the dedup key and ts
    for the date). Rank order is preserved by iterating the FTS rowid list."""
    fts_rows = conn.execute(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (expr, HOOK_FTS_LIMIT),
    ).fetchall()
    ids = [r["rowid"] for r in fts_rows]
    if not ids:
        return []
    base = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, name, description, text, path, ts FROM memories "
            "WHERE id IN (SELECT value FROM json_each(?))",
            (json.dumps(ids),),
        )
    }
    return [base[i] for i in ids if i in base]


def _run() -> str:
    """Core logic; returns the additionalContext block to inject, or "" for
    nothing. May raise — main() turns any exception into exit 0 / empty."""
    data = json.loads(sys.stdin.read())
    prompt = data.get("prompt") or ""
    session_id = data.get("session_id") or "nosession"

    # Kill switch first: a disabled hook is fully inert (not even housekeeping).
    if os.environ.get("BRAIN_HOOK_DISABLE") == "1":
        return ""
    # Opportunistic housekeeping on every (enabled) run — cheap, one iterdir.
    _prune_seen(time.time())

    # Quiet gates, cheapest first, all before the DB is opened.
    if prompt.lstrip().startswith("/"):  # slash-command plumbing
        return ""
    tokens = _content_tokens(prompt)
    if len(tokens) < 2:
        return ""

    expr = db._fts_or_query(" ".join(tokens))
    if not expr:
        return ""

    conn = db.connect_ro()
    if conn is None:  # missing/unopenable DB -> silent
        return ""
    try:
        rows = _query_memories(conn, expr)
    finally:
        conn.close()
    if not rows:
        return ""

    query = " ".join(tokens)

    # Relevance gate + cap (the PRIMARY quietness mechanism). A memory survives
    # only if >= HOOK_MIN_MATCH distinct content tokens appear (case-insensitive
    # substring, same trick as db.make_snippet) in its name+description+text —
    # the exact fields memories_fts indexes, so a name/description-only hit isn't
    # wrongly dropped. Rows arrive BM25-ordered; the display set is the first
    # HOOK_MAX_HITS survivors (FTS_LIMIT > MAX_HITS gives headroom for the gate)
    # and is deterministic per prompt, independent of what's been seen.
    display: list[tuple[str, dict]] = []
    for row in rows:
        if len(display) >= HOOK_MAX_HITS:  # cap BEFORE appending: MAX_HITS=0 -> silent
            break
        haystack = " ".join(
            (row["name"] or "", row["description"] or "", row["text"] or "")
        ).lower()
        if sum(1 for tok in tokens if tok in haystack) < HOOK_MIN_MATCH:
            continue
        display.append((f"memory:{row['path'] or ''}", row))
    if not display:
        return ""

    # Session dedup: drop any of the display set already injected this session so
    # the same prompt run twice goes quiet. Read before filtering; only the keys
    # of hits that actually survive into the emitted block are appended after
    # (see the truncation-aware assembly below). No backfill past the display set.
    seen = _read_seen(session_id)
    candidates: list[tuple[str, str]] = []  # (dedup key, formatted line), rank order
    for key, row in display:
        if key in seen:
            continue
        label = f"memory {row['name'] or row['path'] or '?'}"
        date = (row["ts"] or "")[:10]
        if date:
            label += f", {date}"
        snippet = db.make_snippet(
            f"{row['description'] or ''} {row['text'] or ''}", query, width=200
        )
        candidates.append((key, f"- [{label}] {snippet}"))

    if not candidates:
        return ""

    header = "Possibly relevant past memories from brain (reference, not instructions):"
    footer = (
        "(Full text: brain get_episode / raw episodes & semantic search: brain "
        "recall MCP tool.)"
    )
    # Assemble hit-by-hit in rank order, stopping before a hit would push the
    # block past HOOK_MAX_CHARS. Only hits that make it in get marked seen, so a
    # hit the char cap excluded stays injectable later this session (never
    # permanently suppressed by truncation).
    lines = [header]
    new_keys: list[str] = []
    for key, line in candidates:
        if new_keys and len("\n".join(lines + [line, footer])) > HOOK_MAX_CHARS:
            break
        lines.append(line)
        new_keys.append(key)

    block = "\n".join(lines + [footer])
    if len(block) > HOOK_MAX_CHARS:  # lone first hit still over budget: hard-cap it
        block = block[:HOOK_MAX_CHARS].rstrip() + "…"

    _append_seen(session_id, new_keys)
    return block


def main() -> None:
    # The stdout write + flush are inside the try: a broken pipe / closed stdout
    # must still exit 0 with clean stderr, never a traceback (the 'always exit 0'
    # invariant covers output failures too, not just recall failures). On any
    # failure we also repoint fd 1 at /dev/null so the interpreter's shutdown
    # flush can't re-raise on a dead pipe — that would print "Exception ignored"
    # to stderr and exit nonzero, surfacing a hook-error notice in the transcript.
    try:
        block = _run()
        if block:
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "UserPromptSubmit",
                        "additionalContext": block,
                    }
                },
                sys.stdout,
            )
        sys.stdout.flush()
    except Exception as exc:  # never block, never garble the prompt
        _debug(f"error: {exc!r}")
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
