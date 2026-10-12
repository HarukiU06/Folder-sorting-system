"""Stable-file detection: wait until a file has stopped changing before touching it.

iCloud and browsers write files gradually. A file is ready once its size and
modification time haven't changed for ``stable_seconds`` and no other process
holds it open for writing.
"""

import logging
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.config import DEFAULT_STABLE_SECONDS
from autoshelf.fsutil import is_locked as default_is_locked

log = logging.getLogger(__name__)

DEFAULT_MAX_WAIT_SECONDS = 600.0


@dataclass
class _Candidate:
    first_seen: float
    signature: tuple[int, int] | None = None
    unchanged_since: float = 0.0


class StabilityTracker:
    """Collects paths from file events and reports them once they are stable.

    Thread-safe: ``touch`` is called from the watcher thread and ``poll`` from the worker.
    Repeated events for one path are merged, which debounces bursts of events.
    """

    def __init__(
        self,
        stable_seconds: float = DEFAULT_STABLE_SECONDS,
        *,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        is_locked: Callable[[Path], bool] = default_is_locked,
    ) -> None:
        self.stable_seconds = stable_seconds
        self.max_wait = max_wait
        self._clock = clock
        self._is_locked = is_locked
        self._candidates: dict[Path, _Candidate] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._candidates)

    def touch(self, path: Path) -> None:
        """Start (or keep) tracking ``path`` after a create/modify/move-in event."""
        with self._lock:
            if path not in self._candidates:
                self._candidates[path] = _Candidate(first_seen=self._clock())

    def discard(self, path: Path) -> None:
        """Stop tracking ``path`` (e.g. it was deleted or moved away)."""
        with self._lock:
            self._candidates.pop(path, None)

    def poll(self) -> list[Path]:
        """Check every tracked path and return (and stop tracking) the ones now stable."""
        with self._lock:
            tracked = list(self._candidates.items())
        ready: list[Path] = []
        for path, candidate in tracked:
            verdict = self._check(path, candidate)
            if verdict == "wait":
                continue
            self.discard(path)
            if verdict == "ready":
                ready.append(path)
        return ready

    def _check(self, path: Path, candidate: _Candidate) -> str:
        """Return 'ready', 'wait' or 'drop' for one candidate."""
        now = self._clock()
        try:
            st = path.stat()
        except FileNotFoundError:
            log.debug("Stopped tracking %s: it disappeared", path)
            return "drop"
        if not stat.S_ISREG(st.st_mode):
            return "drop"
        if now - candidate.first_seen > self.max_wait:
            log.warning(
                "Gave up waiting for %s to finish arriving; will retry on next change", path
            )
            return "drop"

        signature = (st.st_size, st.st_mtime_ns)
        if signature != candidate.signature:
            candidate.signature = signature
            candidate.unchanged_since = now
            return "wait"
        if now - candidate.unchanged_since < self.stable_seconds:
            return "wait"
        try:
            if self._is_locked(path):
                return "wait"
        except FileNotFoundError:
            return "drop"
        except OSError as exc:
            log.debug("Couldn't probe %s for locks yet: %s", path, exc)
            return "wait"
        return "ready"
