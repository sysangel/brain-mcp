"""`brain-recall` — search + full-text fetch from Bash, without MCP.

A thin transport shim over the exact two functions the MCP `recall`/`get_episode`
tools delegate to (`db.recall` / `db.get_item`), so CLI and MCP output cannot
diverge on ranking, filtering, FTS-only degradation, or 8k truncation. `uv run`
against source means every invocation runs current code — unlike a `brain-server`
process, which is pinned at spawn time (see specs/recall-cli.md, README).

Two modes:
    brain-recall "query" [--k N] [--scope …] [--project P] [--since D] [--until D] [--json]
    brain-recall --get <ep_…|mem_…|sum_…> [--json]

Search logs telemetry as source="cli" (then flushes, since a short-lived process
would exit before the daemon writer lands the row). --get logs nothing, by design
(an automated fetch is not the human/agent "open" usefulness signal reflect ranks).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys

from . import config, db, usagelog


def _open_conn() -> sqlite3.Connection | None:
    """Connect to the live index, or print a loud notice and return None.

    Guards on DB_PATH existence FIRST: db.connect() would otherwise materialize
    an empty DB (mkdir + schema DDL) and silently answer "0 hits", hiding an
    operator error from a script. Never create a DB as a side effect of a read.
    """
    if not config.DB_PATH.exists():
        print(
            f"brain-recall: no index yet ({config.DB_PATH}); run brain-ingest --full",
            file=sys.stderr,
        )
        return None
    return db.connect()


def _render_hits(result: dict, scope: str) -> None:
    """Compact, greppable, one-line-per-hit output (score-descending).

    A header line reports scope, fts-only (when degraded), and the resolved
    filters/candidate count (when filtering is active) so a thin result reads as
    filtered/degraded rather than as a thin corpus.
    """
    header = [f"scope={scope}"]
    if result.get("fts_only"):
        header.append("fts-only")
    if "filters" in result:
        header.append(f"filters={result['filters']}")
    if "filter_candidates" in result:
        header.append(f"filter_candidates={result['filter_candidates']}")
    print("  ".join(header))

    hits = result["hits"]
    if not hits:
        print("(no hits)")
        return
    for h in hits:
        print(
            f"{h['id']}  {h['kind']}  {h['date']}  {h.get('project') or '?'}  "
            f"score={h['score']:.4f}  {h['snippet']}"
        )


def _render_item(out: dict) -> None:
    """Metadata block (every key except `text`), a blank line, then full text."""
    for key, val in out.items():
        if key == "text":
            continue
        print(f"{key}: {val}")
    print()
    print(out.get("text", ""))


def _search(args: argparse.Namespace) -> int:
    query: str = args.query
    # Reject empty/punctuation-only queries before connecting (mirrors db.py:554)
    # so no empty-query telemetry is written and a script sees the error loudly.
    if not re.search(r"\w", query):  # Unicode-aware: CJK etc. still searchable
        print("brain-recall: empty query", file=sys.stderr)
        return 1

    conn = _open_conn()
    if conn is None:
        return 1
    try:
        result = db.recall(
            conn, query, k=args.k, scope=args.scope,
            project=args.project, since=args.since, until=args.until,
        )
    except ValueError as e:  # bad scope / empty project / bad date bound
        print(f"brain-recall: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    # Pop diag so JSON is byte-parity with the MCP tool, then log as source="cli".
    diag = result.pop("diag", None)
    if diag is not None:
        usagelog.log_recall(
            query=query, scope=args.scope, k=args.k, source="cli",
            hits=result["hits"], diag=diag,
        )

    if args.json:
        print(json.dumps(result))
    else:
        _render_hits(result, args.scope)

    # Flush AFTER rendering: the short-lived process would otherwise exit before
    # the daemon writer lands the row; the ~ms DB write overlaps stdout.
    usagelog.flush(timeout=1.0)
    return 0


def _get(args: argparse.Namespace) -> int:
    conn = _open_conn()
    if conn is None:
        return 1
    try:
        out = db.get_item(conn, args.get)
    finally:
        conn.close()

    if "error" in out:  # unknown prefix / no such row / invalid sum_ id
        print(f"brain-recall: {out['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(out))
    else:
        _render_item(out)
    return 0  # deliberately no telemetry: --get is not an "open" usefulness signal


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="brain-recall",
        description="Search the brain or fetch full item text from Bash, without MCP. "
        "A thin shim over db.recall / db.get_item — same ranking, filters, and truncation "
        "as the MCP tools.",
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("query", nargs="?", help="search query (hybrid FTS + vector recall)")
    mode.add_argument(
        "--get", metavar="ID",
        help="fetch full text + metadata for an ep_/mem_/sum_ id (no telemetry)",
    )
    ap.add_argument("--k", type=int, default=6, help="max hits (default 6, matching the MCP tool)")
    ap.add_argument(
        "--scope", default="all",
        help="all|episodes|memories|summaries (default all)",
    )
    ap.add_argument("--project", default=None, help="filter to a project (session-dir path segment)")
    ap.add_argument("--since", default=None, help="lower date bound: YYYY-MM-DD or Nd (N days ago)")
    ap.add_argument("--until", default=None, help="upper date bound: YYYY-MM-DD or Nd")
    ap.add_argument(
        "--json", action="store_true",
        help="emit JSON (byte-parity with the MCP tool; diag omitted)",
    )
    args = ap.parse_args(argv)

    if args.get is not None:
        return _get(args)
    return _search(args)


if __name__ == "__main__":
    sys.exit(main())
