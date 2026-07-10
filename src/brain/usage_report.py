"""brain-usage-report: the feedback report reflect reads (see
specs/usage-logging.md).

Prunes rows older than the retention window, opens `local/usage.db` read-write
(for the prune) and ATTACHes the derived index `cache/brain.db` read-only (for
the memory universe), then emits four sections — never-recalled memories, gap
queries, most-opened items, and a calibration block. Compact markdown by
default; `--json` for machine consumption.

Absent telemetry or a missing index is a one-line notice + exit 0: reflect must
never die on missing, lossy usage data.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from .config import DB_PATH, USAGE_DB_PATH, USAGE_RETENTION_DAYS, WEAK_VEC_COS

_NEVER_RECALLED = """
SELECT m.stable_id AS stable_id, m.mtype AS mtype, m.path AS path,
       substr(m.ts, 1, 10) AS updated
FROM brain.memories m
WHERE m.ts < :since
  AND NOT EXISTS (
        SELECT 1 FROM recall_hits h JOIN recalls r ON r.id = h.recall_id
        WHERE h.kind = 'memory' AND h.stable_id = m.stable_id AND r.ts >= :since)
  AND NOT EXISTS (
        SELECT 1 FROM opens o
        WHERE o.kind = 'memory' AND o.stable_id = m.stable_id AND o.ts >= :since)
ORDER BY m.ts
"""

_GAP_QUERIES = """
SELECT count(*) AS times, max(r.ts) AS last, r.source AS source, r.query AS query,
       max(r.top_vec_cos) AS best_cos, max(coalesce(r.top_score, 0)) AS best_rrf
FROM recalls r
WHERE r.ts >= :since
  AND (r.n_hits = 0
       OR (r.top_vec_cos IS NOT NULL AND r.top_vec_cos < :weak))
GROUP BY lower(trim(r.query))
ORDER BY times DESC, last DESC LIMIT 20
"""

_MOST_OPENED = """
SELECT kind, stable_id, count(*) AS opens,
       sum(source = 'mcp') AS explicit_opens, max(ts) AS last
FROM opens WHERE ts >= :since
GROUP BY kind, stable_id ORDER BY explicit_opens DESC, opens DESC LIMIT 10
"""

_VOLUME = """
SELECT source, count(*) AS n, sum(fts_only) AS fts_only
FROM recalls WHERE ts >= :since GROUP BY source ORDER BY n DESC
"""

_WEAK_RATE = """
SELECT count(*) AS total,
       sum(CASE WHEN n_hits = 0
                 OR (top_vec_cos IS NOT NULL AND top_vec_cos < :weak)
                THEN 1 ELSE 0 END) AS weak
FROM recalls WHERE ts >= :since
"""

_COS_VALUES = """
SELECT top_vec_cos AS c FROM recalls
WHERE ts >= :since AND top_vec_cos IS NOT NULL ORDER BY top_vec_cos
"""

_NEVER_RECALLED_SHOWN = 40  # markdown display cap (keeps total output ~150 lines)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _percentiles(values: list[float]) -> dict | None:
    """min/p25/p50/p75/max via nearest-rank; None on an empty sample."""
    if not values:
        return None
    n = len(values)

    def at(q: float) -> float:
        idx = min(n - 1, max(0, round(q * (n - 1))))
        return round(values[idx], 4)

    return {
        "min": round(values[0], 4),
        "p25": at(0.25),
        "p50": at(0.50),
        "p75": at(0.75),
        "max": round(values[-1], 4),
    }


def _build(conn: sqlite3.Connection, since: str) -> dict:
    p = {"since": since, "weak": WEAK_VEC_COS}
    never = [dict(r) for r in conn.execute(_NEVER_RECALLED, p).fetchall()]
    gaps = [dict(r) for r in conn.execute(_GAP_QUERIES, p).fetchall()]
    opened = [dict(r) for r in conn.execute(_MOST_OPENED, p).fetchall()]

    volume = [dict(r) for r in conn.execute(_VOLUME, p).fetchall()]
    wr = conn.execute(_WEAK_RATE, p).fetchone()
    total, weak = (wr["total"] or 0), (wr["weak"] or 0)
    cos_vals = [r["c"] for r in conn.execute(_COS_VALUES, p).fetchall()]
    calibration = {
        "recalls": total,
        "weak": weak,
        "weak_rate": round(weak / total, 3) if total else 0.0,
        "by_source": volume,
        "weak_vec_cos_threshold": WEAK_VEC_COS,
        "top_vec_cos": _percentiles(cos_vals),
    }
    return {
        "since": since,
        "never_recalled": never,
        "gap_queries": gaps,
        "most_opened": opened,
        "calibration": calibration,
    }


def _cell(c) -> str:
    """One markdown table cell: never None, with pipes/newlines that would break
    the row layout neutralized — verbatim query text can contain both."""
    s = "" if c is None else str(c)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _table(headers: list[str], rows: list[list], empty: str = "(none)") -> list[str]:
    if not rows:
        return [empty]
    cells = [[_cell(c) for c in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    line = lambda cols: "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cols)) + " |"
    out = [line(headers), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [line(row) for row in cells]
    return out


def _render_md(data: dict) -> str:
    out: list[str] = [f"# brain usage report (since {data['since']})", ""]

    never = data["never_recalled"]
    out.append(f"## never-recalled memories ({len(never)})")
    shown = never[:_NEVER_RECALLED_SHOWN]
    out += _table(
        ["type", "updated", "id", "path"],
        [[m["mtype"], m["updated"], m["stable_id"], m["path"]] for m in shown],
        empty="(none — every aged memory saw a hit or open)",
    )
    if len(never) > len(shown):
        out.append(f"… and {len(never) - len(shown)} more")
    out.append("")

    out.append(f"## gap queries ({len(data['gap_queries'])})")
    out += _table(
        ["times", "src", "best_cos", "best_rrf", "last", "query"],
        [
            [g["times"], g["source"],
             "" if g["best_cos"] is None else round(g["best_cos"], 3),
             round(g["best_rrf"], 4), (g["last"] or "")[:10], g["query"]]
            for g in data["gap_queries"]
        ],
        empty="(none — no weak or empty recalls in the window)",
    )
    out.append("")

    out.append(f"## most-opened ({len(data['most_opened'])})")
    out += _table(
        ["kind", "opens", "explicit", "last", "id"],
        [
            [o["kind"], o["opens"], o["explicit_opens"], (o["last"] or "")[:10],
             o["stable_id"]]
            for o in data["most_opened"]
        ],
        empty="(none — nothing opened in the window)",
    )
    out.append("")

    c = data["calibration"]
    out.append("## calibration")
    out.append(
        f"- recalls: {c['recalls']} · weak: {c['weak']} "
        f"(rate {c['weak_rate']}, threshold cos < {c['weak_vec_cos_threshold']})"
    )
    out += _table(
        ["source", "recalls", "fts_only"],
        [[v["source"], v["n"], v["fts_only"] or 0] for v in c["by_source"]],
        empty="(no recalls in the window)",
    )
    d = c["top_vec_cos"]
    if d:
        out.append(
            f"- top_vec_cos: min {d['min']} · p25 {d['p25']} · p50 {d['p50']} "
            f"· p75 {d['p75']} · max {d['max']}"
        )
    else:
        out.append("- top_vec_cos: (no vector recalls in the window)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="brain-usage-report",
        description="Usage feedback report for the nightly reflect pass.",
    )
    ap.add_argument("--days", type=int, default=30, help="window size in days (default 30)")
    ap.add_argument("--since", type=str, default=None,
                    help="explicit ISO lower bound (overrides --days), e.g. 2026-06-03T00:00:00Z")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    args = ap.parse_args(argv)

    since = args.since if args.since is not None else _iso(_utcnow() - timedelta(days=args.days))

    if not USAGE_DB_PATH.exists():
        print(f"brain-usage-report: no usage telemetry yet ({USAGE_DB_PATH} absent)")
        return 0
    if not DB_PATH.exists():
        print(f"brain-usage-report: no index yet ({DB_PATH} absent); run brain-ingest --full")
        return 0

    # uri=True so the ATTACH below can pass a `?mode=ro` URI for the index.
    try:
        conn = sqlite3.connect(USAGE_DB_PATH.resolve().as_uri(), uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("PRAGMA foreign_keys=ON")  # so the prune cascades recall_hits
        try:
            cutoff = _iso(_utcnow() - timedelta(days=USAGE_RETENTION_DAYS))
            conn.execute("DELETE FROM recalls WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM opens WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.execute(
                "ATTACH DATABASE ? AS brain", (f"{DB_PATH.resolve().as_uri()}?mode=ro",)
            )
            data = _build(conn, since)
        finally:
            conn.close()
    except sqlite3.Error as e:
        # A corrupt/truncated usage.db or a failed ATTACH is lossy telemetry, not
        # a fatal error: same one-line notice + exit 0 as a missing file, so the
        # nightly reflect pass never dies on unreadable telemetry.
        print(f"brain-usage-report: usage telemetry unreadable ({e}); skipping report")
        return 0

    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(_render_md(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
