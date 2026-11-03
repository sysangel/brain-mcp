"""Cloud Run Job entry: backup, then git export.

A failure in one step does not skip the other. The process exits non-zero
when either step failed, so Cloud Run marks the execution failed and the
scheduler's next day still runs.
"""

from __future__ import annotations

import sys

from . import backup, git_export


def main(argv: list[str] | None = None) -> int:
    del argv
    errors: list[str] = []
    try:
        code = backup.main()
        if code:
            errors.append(f"backup exited {code}")
    except Exception as exc:
        errors.append(f"backup: {exc}")
        print(f"brain-nightly: backup: {exc}", file=sys.stderr)
    try:
        code = git_export.main()
        if code:
            errors.append(f"git export exited {code}")
    except Exception as exc:
        errors.append(f"git export: {exc}")
        print(f"brain-nightly: git export: {exc}", file=sys.stderr)
    if errors:
        print("brain-nightly: failed: " + "; ".join(errors), file=sys.stderr)
        return 1
    print("brain-nightly: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
