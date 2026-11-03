"""Split a SQL script into statements without breaking dollar quotes.

Migrations use ``DO $$ ... $$`` blocks and function bodies. A naive split on
``;`` would cut those in half. ``psycopg`` executes one statement at a time.
"""

from __future__ import annotations

import re

_DOLLAR = re.compile(r"\$[A-Za-z0-9_]*\$")


def split_sql(script: str) -> list[str]:
    """Return executable statements, comments-only chunks dropped."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(script)
    dollar: str | None = None
    while i < n:
        if dollar is not None:
            end = script.find(dollar, i)
            if end < 0:
                buf.append(script[i:])
                i = n
                break
            end += len(dollar)
            buf.append(script[i:end])
            i = end
            dollar = None
            continue
        if script.startswith("--", i):
            nl = script.find("\n", i)
            if nl < 0:
                i = n
                break
            i = nl + 1
            continue
        if script[i] == "'":
            j = i + 1
            while j < n:
                if script[j] == "'":
                    if j + 1 < n and script[j + 1] == "'":
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            buf.append(script[i:j])
            i = j
            continue
        match = _DOLLAR.match(script, i)
        if match:
            dollar = match.group(0)
            buf.append(dollar)
            i = match.end()
            continue
        if script[i] == ";":
            stmt = "".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(script[i])
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts
