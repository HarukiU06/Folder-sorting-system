"""Files that crashed AutoShelf while it read them are not read again (no crash loop).

A parser written in C (PDF, XML) can take the whole process down on a broken or
malicious file, and no ``except`` catches that. The file is still in the root afterwards,
so the next start would read it again and crash again. To prevent that, the process that
sorts the library (the one holding the watch lock) lists each file it is reading in
``reading.json`` in the data folder. A file still listed when that process starts again
was being read when it stopped: from then on it is sorted by its name only, until its
content changes. Every process skips those files; only the sorting process records.

A power cut or a forced restart looks the same as a crash, so the skipped files can be
listed and cleared (``skipped_files``, ``clear``: Settings and ``autoshelf skipped``);
a cleared file is read again. A running guard notices when another process cleared
files, so clearing works while the app runs.
"""

import itertools
import json
import logging
import os
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

STATE_FILENAME = "reading.json"
CRASHED = "it stopped AutoShelf the last time it was read, so only its name is used"
MAX_SKIPPED = 200


@dataclass(frozen=True)
class SkippedFile:
    """A file AutoShelf no longer reads (only its name is used), as it was then."""

    path: Path
    size: int
    mtime_ns: int


class ReadGuard:
    """Files being read now (when ``record``), and files whose reading crashed before."""

    def __init__(self, state_path: Path, *, record: bool) -> None:
        self.state_path = state_path
        self.record = record
        self._lock = threading.Lock()
        self._tokens = itertools.count()
        self._reading: dict[int, dict] = {}
        self._closed = False
        self._seen_ns: int | None = None  # the state file's mtime when last read or written
        state = self._load()
        self._skipped = state["skipped"]
        self.new_skips: list[dict] = []  # skipped from this start on (they crashed the last run)
        if record:
            for entry in state["reading"]:
                log.warning(
                    "%s stopped AutoShelf while it was being read; from now on only its "
                    "name is used to sort it",
                    entry["path"],
                )
            self.new_skips = state["reading"]
            self._skipped = (self._skipped + state["reading"])[-MAX_SKIPPED:]
            self._save()

    def skips(self, path: Path) -> bool:
        """True if reading this file (as it is now) crashed AutoShelf before."""
        try:
            st = path.stat()
        except OSError:
            return False
        with self._lock:
            self._reload_if_changed()
            skipped = list(self._skipped)
        return any(
            entry["size"] == st.st_size and entry["mtime_ns"] == st.st_mtime_ns for entry in skipped
        )

    def skipped(self) -> list[SkippedFile]:
        """The files that are no longer read, oldest first."""
        with self._lock:
            self._reload_if_changed()
            return _as_files(self._skipped)

    def clear(self, paths: Iterable[Path] | None = None) -> int:
        """Read these files (or all, for None) again; returns how many were cleared."""
        with self._lock:
            self._reload_if_changed()
            kept = _without(self._skipped, paths)
            cleared = len(self._skipped) - len(kept)
            self._skipped = kept
            self._save()
        return cleared

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
        self._seen_ns = _mtime_ns(self.state_path)
        return _read_state(self.state_path)

    def _reload_if_changed(self) -> None:
        """Pick up skipped files another process cleared (call with the lock held)."""
        if _mtime_ns(self.state_path) != self._seen_ns:
            self._skipped = self._load()["skipped"]

    def _save(self) -> None:
        # The skipped list on disk may have been cleared by another process meanwhile.
        self._reload_if_changed()
        state = {"reading": list(self._reading.values()), "skipped": self._skipped}
        _write_state(self.state_path, state)
        self._seen_ns = _mtime_ns(self.state_path)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_state(state_path: Path) -> dict[str, list[dict]]:
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        log.warning("Couldn't read %s: %s", state_path, exc)
        state = {}
    if not isinstance(state, dict):
        state = {}
    return {key: _valid(state.get(key)) for key in ("reading", "skipped")}


def _write_state(state_path: Path, state: dict) -> None:
    # One temporary file per process and thread, so two writers never share one.
    temp = state_path.with_name(f"{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(temp, state_path)
    except OSError as exc:
        log.debug("Couldn't save %s: %s", state_path, exc)


def _as_files(entries: list[dict]) -> list[SkippedFile]:
    return [SkippedFile(Path(entry["path"]), entry["size"], entry["mtime_ns"]) for entry in entries]


def _without(entries: list[dict], paths: Iterable[Path] | None) -> list[dict]:
    """``entries`` minus those for ``paths``; all of them for None.

    An entry belongs to a path by its name (any case), or by the size and time of the file
    there now: a skipped file is usually moved (sorted by its name) after it was skipped.
    """
    if paths is None:
        return []
    names: set[str] = set()
    versions: set[tuple[int, int]] = set()
    for path in paths:
        names.add(str(path).casefold())
        try:
            st = Path(path).stat()
        except OSError:
            continue
        versions.add((st.st_size, st.st_mtime_ns))
    return [
        entry
        for entry in entries
        if entry["path"].casefold() not in names
        and (entry["size"], entry["mtime_ns"]) not in versions
    ]


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


def _active_for(home: Path) -> ReadGuard | None:
    guard = _active
    if guard is not None and guard.state_path == home / STATE_FILENAME:
        return guard
    return None


def skipped_files(home: Path) -> list[SkippedFile]:
    """The files AutoShelf no longer reads (only their names are used), for data folder
    ``home``."""
    guard = _active_for(home)
    if guard is not None:
        return guard.skipped()
    return _as_files(_read_state(home / STATE_FILENAME)["skipped"])


def clear(home: Path, paths: Iterable[Path] | None = None) -> int:
    """Have AutoShelf read these skipped files (or all of them, for None) again.

    Returns how many were cleared. Works whether or not this process records, and while
    another process (the app) runs: its guard notices the change.
    """
    guard = _active_for(home)
    if guard is not None:
        return guard.clear(paths)
    state_path = home / STATE_FILENAME
    state = _read_state(state_path)
    kept = _without(state["skipped"], paths)
    cleared = len(state["skipped"]) - len(kept)
    if cleared:
        _write_state(state_path, {"reading": state["reading"], "skipped": kept})
    return cleared


def newly_skipped() -> list[Path]:
    """Files that stopped the previous run while being read, so they are skipped from now
    on (empty unless this process records)."""
    guard = _active
    return [] if guard is None else [Path(entry["path"]) for entry in guard.new_skips]


@contextmanager
def reading(path: Path) -> Iterator[None]:
    """List ``path`` as being read, if this process records."""
    guard = _active
    if guard is None:
        yield
        return
    with guard.reading(path):
        yield
