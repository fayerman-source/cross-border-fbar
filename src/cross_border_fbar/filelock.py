"""An exclusive lock between processes, on POSIX and Windows."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _acquire(handle) -> None:
        while True:  # LK_LOCK gives up after about 10 seconds; waiting matches flock on POSIX
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                continue

    def _release(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _acquire(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_EX)

    def _release(handle) -> None:
        fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive(path: Path):
    """Hold the lock file at path for the duration of the block; blocks until it is free."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        _acquire(handle)
        try:
            yield
        finally:
            _release(handle)
