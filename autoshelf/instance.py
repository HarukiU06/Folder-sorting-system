"""Only one AutoShelf process may sort a library at a time.

Two watchers on the same data folder (say the desktop app and ``autoshelf watch``) would
race to move the same files. ``WatchLock`` is an exclusive OS-level lock on a file in the
data folder; the OS releases it if the process dies, so it never goes stale.
"""

import os
import sys
from pathlib import Path

LOCK_FILENAME = "watcher.lock"
_LOCK_OFFSET = 64  # the PID is written before the locked byte, so others can read it


class WatchLock:
    """An exclusive, non-blocking lock. Use ``acquire``/``release`` or ``with``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._file is not None

    def acquire(self) -> bool:
        """Take the lock. Returns False (without waiting) if another holder has it."""
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # noqa: SIM115 - kept open while the lock is held
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}".ljust(_LOCK_OFFSET - 1).encode() + b"\n")
        handle.flush()
        self._file = handle
        return True

    def release(self) -> None:
        """Give the lock back (does nothing if it isn't held)."""
        if self._file is None:
            return
        try:
            _unlock(self._file)
        finally:
            self._file.close()
            self._file = None

    def holder_pid(self) -> int | None:
        """The process id written by the current holder, if readable."""
        try:
            # Unbuffered, to read only the PID: the locked byte after it can't be read
            # by other processes, and a buffered read would reach it.
            with open(self.path, "rb", buffering=0) as handle:
                first = handle.read(_LOCK_OFFSET - 1).strip()
            return int(first) if first else None
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "WatchLock":
        if not self.acquire():
            raise RuntimeError(f"{self.path} is locked by another process")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
