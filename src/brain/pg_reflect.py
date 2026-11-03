"""Reflect lease against ``brain.reflect_runs``.

Replaces the git file claim for the Supabase store. The lock that serializes
two Cloud Run instances is ``pg_advisory_xact_lock`` inside the claim
transaction, which Supavisor transaction mode keeps until commit. The lease
that outlives the request is the row, not a session advisory lock.
"""

from __future__ import annotations

import argparse
import socket
import sys

from .pgstore import claim_reflect, complete_reflect, reflect_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Supabase reflect lease")
    parser.add_argument("command", choices=("claim", "complete", "status"))
    parser.add_argument("--by", default=socket.gethostname())
    parser.add_argument("--id", type=int, default=None)
    args = parser.parse_args(argv)
    if args.command == "claim":
        result = claim_reflect(args.by)
        if not result["ok"]:
            print(
                f"reflect claim held by {result['claimed_by']} since {result['claimed_at']}",
                file=sys.stderr,
            )
            return 1
        print(result["claimed_at"])
        return 0
    if args.command == "complete":
        result = complete_reflect(args.id)
        if not result["ok"]:
            print(result["reason"], file=sys.stderr)
            return 1
        print(result["watermark"])
        return 0
    status = reflect_status()
    claim = status["claim"]
    print(f"watermark={status['watermark'] or '-'}")
    if claim is None:
        print("claim=-")
    else:
        print(f"claim={claim['claimed_by']} {claim['claimed_at']} id={claim['id']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"brain-pg-reflect: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
