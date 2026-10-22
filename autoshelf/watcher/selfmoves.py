"""Remembers where AutoShelf just moved files, so the watcher doesn't react to its own moves."""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

DEFAULT_TTL_SECONDS = 30.0


class SelfMoveRegistry:
    """Short-lived, thread-safe sets of the paths the app's own moves create and vacate.

    Destinations (``expect``) are kept apart from sources (``expect_removal``): a new file
    may legitimately appear at a source right away and must still be processed, while
    the removal of the source (Windows reports a move between folders as a deletion plus
    a creation) must not count as the user deleting a file.
    """

    def __init__(
        self, ttl: float = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._expiry: dict[str, float] = {}
        self._removals: dict[str, float] = {}
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

    def expect_removal(self, *paths: Path) -> None:
        """Record that the app is about to move files away from ``paths``."""
        expires = self._clock() + self._ttl
        with self._lock:
            for path in paths:
                self._removals[self._key(path)] = expires

    def is_expected(self, path: Path) -> bool:
        """Return True if events for ``path`` were caused by the app recently."""
        now = self._clock()
        with self._lock:
            self._expiry = {key: exp for key, exp in self._expiry.items() if exp > now}
            return self._key(path) in self._expiry

    def is_expected_removal(self, path: Path) -> bool:
        """Return True if ``path`` disappeared because the app moved the file away."""
        now = self._clock()
        with self._lock:
            self._removals = {key: exp for key, exp in self._removals.items() if exp > now}
            return self._key(path) in self._removals

    def expect_move(self, src: Path, dst: Path) -> None:
        """Record both ends of a move the app is about to make (a ``BeforeRename``)."""
        self.expect_removal(src)
        self.expect(dst)
