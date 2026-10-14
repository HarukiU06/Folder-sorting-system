"""Files that crashed AutoShelf while it read them are not read again (no crash loop).

A parser written in C (PDF, XML) can take the whole process down on a broken or
malicious file, and no ``except`` catches that. The file is still in the root afterwards,
so the next start would read it again and crash again. To prevent that, the process that
sorts the library (the one holding the watch lock) lists each file it is reading in
``reading.json`` in the data folder. A file still listed when that process starts again
was being read when it stopped: from then on it is sorted by its name only, until its
content changes. Every process skips those files; only the sorting process records.
"""

import itertools
import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILENAME = "reading.json"
CRASHED = "it stopped AutoShelf the last time it was read, so only its name is used"
MAX_SKIPPED = 200


class ReadGuard:
    """Files being read now (when ``record``), and files whose reading crashed before."""

    def __init__(self, state_path: Path, *, record: bool) -> None:
        self.state_path = state_path
        self.record = record
        self._lock = threading.Lock()
        self._tokens = itertools.count()
        self._reading: dict[int, dict] = {}
        self._closed = False
        state = self._load()
        self._skipped = state["skipped"]
        if record:
            for entry in state["reading"]:
                log.warning(
                    "%s stopped AutoShelf while it was being read; from now on only its "
                    "name is used to sort it",
                    entry["path"],
                )
            self._skipped = (self._skipped + state["reading"])[-MAX_SKIPPED:]
            self._save()

    def skips(self, path: Path) -> bool:
        """True if reading this file (as it is now) crashed AutoShelf before."""
        try:
            st = path.stat()
        except OSError:
            return False
        return any(
            entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns
            for entry in self._skipped
        )

    @contextmanager
    def reading(self, path: Path) -> Iterator[None]:
        """List ``path`` as being read for the duration of the ``with`` block."""
        if not self.record or self._closed:
            yield
            return
        st = path.stat()
        entry = {"path": str(path), "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        with self._lock:
            token = next(self._tokens)
            self._reading[token] = entry
            self._save()
        try:
            yield
        finally:
            with self._lock:
                self._reading.pop(token, None)
                if not self._closed:
                    self._save()

    def close(self) -> None:
        """A clean exit: reads still running at this point didn't crash anything."""
        with self._lock:
            if self.record and not self._closed:
                self._reading.clear()
                self._save()
            self._closed = True

    def _load(self) -> dict[str, list[dict]]:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError) as exc:
            log.warning("Couldn't read %s: %s", self.state_path, exc)
            state = {}
        if not isinstance(state, dict):
            state = {}
        return {key: _valid(state.get(key)) for key in ("reading", "skipped")}

    def _save(self) -> None:
        state = {"reading": list(self._reading.values()), "skipped": self._skipped}
        temp = self.state_path.with_name(self.state_path.name + ".tmp")
        try:
            temp.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temp, self.state_path)
        except OSError as exc:
            log.debug("Couldn't save %s: %s", self.state_path, exc)


def _valid(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("size"), int)
        and isinstance(entry.get("mtime_ns"), int)
    ]


_active: ReadGuard | None = None


def start(home: Path, *, record: bool) -> ReadGuard:
    """Use the guard of the data folder ``home``. ``record`` only in the process that
    holds the watch lock (it alone may treat leftover entries as crashes)."""
    global _active
    stop()
    _active = ReadGuard(home / STATE_FILENAME, record=record)
    return _active


def stop() -> None:
    """Stop using the guard (a clean exit)."""
    global _active
    guard, _active = _active, None
    if guard is not None:
        guard.close()


def crashed_before(path: Path) -> bool:
    """True if reading ``path`` crashed AutoShelf before (False when no guard is used)."""
    return _active is not None and _active.skips(path)


@contextmanager
def reading(path: Path) -> Iterator[None]:
    """List ``path`` as being read, if this process records."""
    guard = _active
    if guard is None:
        yield
        return
    with guard.reading(path):
        yield
