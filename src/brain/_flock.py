"""Cross-platform flock shim: `from . import _flock as fcntl` keeps call
sites written against the POSIX API working on Windows.

POSIX: re-exports the real fcntl. Windows: emulates via msvcrt.locking on
byte 0 of the lockfile, which covers both patterns this codebase uses —
LOCK_EX|LOCK_NB raising OSError when the lock is held, and a blocking
LOCK_EX. Like flock, the lock dies with the file handle / process, so the
no-TTL, no-stale-reclaim doctrine holds on both platforms.
"""

from __future__ import annotations

import sys

if sys.platform != "win32":
    from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock  # noqa: F401
else:
    import msvcrt
    import os
    import time

    LOCK_SH = 0x1
    LOCK_EX = 0x2
    LOCK_NB = 0x4
    LOCK_UN = 0x8

    def flock(fh, flags: int) -> None:
        fd = fh.fileno() if hasattr(fh, "fileno") else fh
        # msvcrt.locking locks bytes from the CURRENT position; pin to 0 so
        # lock and unlock always target the same byte regardless of writes.
        os.lseek(fd, 0, os.SEEK_SET)
        if flags & LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        if flags & LOCK_NB:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # raises OSError if held
            return
        while True:  # blocking acquire; LK_LOCK gives up after ~10s, so loop
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.1)
