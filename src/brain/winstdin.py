"""Windows: make the MCP stdin read poll instead of parking a blocking read.

On Windows, a pending SYNCHRONOUS ReadFile on a pipe locks that pipe's
kernel file object. Other operations that touch the same file object then
block until the read completes — and the standard handles get probed a lot:
CreateProcess inspects them while wiring child std handles, and a freshly
loaded DLL that carries its own C runtime (numpy's OpenBLAS, onnxruntime)
probes them during CRT init under the OS loader lock.

The MCP stdio transport parks exactly such a read between client messages,
and the client stays silent for the whole duration of any tool call (it is
waiting for the response). Net effect, observed 2026-08-21: the first
recall/remember wedges inside `import sqlite_vec` -> numpy -> OpenBLAS
DllMain, and every remember/sync git subprocess wedges inside
CreateProcess/communicate — timeouts included. The wedge releases the
instant the client cancels (teardown closes stdin, the parked read returns
EOF), which is why responses always surfaced at the exact second of a
user-cancel and were then dropped as unknown message ids. POSIX has no
equivalent: read(2) on a pipe serializes nothing anyone else touches.
See proposals/2026-08-21-bug-mcp-tool-hangs.md.

Fix: replace sys.stdin with a stream whose reads never leave I/O pending —
PeekNamedPipe until bytes are available (15ms tick), then a ReadFile that
is guaranteed to return immediately. Idle cost is a few dozen microsecond
syscalls per second; added message latency is at most one tick.
"""

from __future__ import annotations

import io
import sys
import time

_POLL_SECONDS = 0.015


def install() -> None:
    """Swap sys.stdin for the poll-based reader (win32 only).

    Must run before the stdio transport starts: mcp.server.stdio captures
    `sys.stdin.buffer` when stdio_server() is entered. No-op off Windows,
    when stdin is closed/detached, or when stdin is not a pipe (console
    runs keep the normal blocking read, which is harmless there)."""
    if sys.platform != "win32":
        return
    import msvcrt
    import _winapi

    try:
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        _winapi.PeekNamedPipe(handle, 0)
    except (AttributeError, OSError, ValueError):
        return  # detached stdin, or not a pipe: nothing to shield

    class _PollingStdin(io.RawIOBase):
        """Reads the stdin pipe without ever leaving a read pending."""

        def readable(self) -> bool:
            return True

        def readinto(self, b) -> int:
            while True:
                try:
                    avail = _winapi.PeekNamedPipe(handle, 0)[0]
                except OSError:
                    return 0  # client closed its end -> clean EOF
                if avail:
                    break
                time.sleep(_POLL_SECONDS)
            try:
                data, _ = _winapi.ReadFile(handle, min(len(b), avail))
            except OSError:
                return 0
            n = len(data)
            b[:n] = data
            return n

    sys.stdin = io.TextIOWrapper(
        io.BufferedReader(_PollingStdin()), encoding="utf-8", errors="replace"
    )
