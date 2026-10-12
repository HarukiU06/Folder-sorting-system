"""Remembers where AutoShelf just moved files, so the watcher doesn't react to its own moves."""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_TTL_SECONDS = 30.0


class SelfMoveRegistry:
    """Short-lived, thread-safe set of destinations of the app's own moves."""

    def __init__(
        self, ttl: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._expiry: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(os.fspath(path))

    def expect(self, *paths: Path) -> None:
        """Record that the app is about to create files at ``paths``."""
        expires = self._clock() + self._ttl
        with self._lock:
            for path in paths:
                self._expiry[self._key(path)] = expires

    def is_expected(self, path: Path) -> bool:
        """Return True if events for ``path`` were caused by the app recently."""
        now = self._clock()
        with self._lock:
            self._expiry = {key: exp for key, exp in self._expiry.items() if exp > now}
            return self._key(path) in self._expiry
