"""Runs the watcher: watchdog events -> stability tracker -> pipeline, off the main thread."""

import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from autoshelf.actions.history import reconcile_pending
from autoshelf.config import (
    DEFAULT_AUTO_MARGIN,
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_STABLE_SECONDS,
    DEFAULT_SWEEP_MINUTES,
)
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.layout import PROCESSED_ZONES, RESERVED_PREFIX, Zone, inbox_dir, zone_of
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Outcome, OutcomeKind, Pipeline
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.selfmoves import SelfMoveRegistry
from autoshelf.watcher.stability import StabilityTracker

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5
# After the user changes something in their folders, the library is rescanned once things
# have been quiet this long (to learn from files they moved, see actions.learning).
LEARN_DELAY_SECONDS = 2.0
DEFAULT_SWEEP_SECONDS = DEFAULT_SWEEP_MINUTES * 60
_MAX_NOTED_PATHS = 5000
_PROGRESS_EVERY = 25
_MTIME_TOLERANCE = 1e-3

LibraryFactory = Callable[[sqlite3.Connection], LibraryIndex]

# What the watcher is busy with, for the app's progress bar (Progress.stage).
LOADING = "loading"  # starting (or downloading) the language model
READING = "reading"  # reading the files already in your folders, once
SORTING = "sorting"  # sorting the new files that were waiting
PAUSED = "paused"  # new files are waiting, but sorting is paused
READY = "ready"  # nothing left to do: watching for new files


@dataclass(frozen=True)
class Progress:
    """How far the watcher has got: ``done`` of ``total`` steps of ``stage``."""

    stage: str
    done: int = 0
    total: int = 0


class _EventHandler(FileSystemEventHandler):
    """Translates watchdog events into ``offer``/``forget`` calls on the service."""

    def __init__(self, service: "WatchService") -> None:
        super().__init__()
        self._service = service

    def on_any_event(self, event: FileSystemEvent) -> None:
        src = Path(os.fsdecode(event.src_path))
        if event.is_directory:
            # A folder appeared, vanished or was renamed: the index must rescan (a new
            # empty folder makes no file event). "modified" only means its content changed.
            if event.event_type in ("created", "deleted", "moved"):
                self._service.folder_changed(src)
            if event.event_type == "moved":
                self._service.folder_changed(Path(os.fsdecode(event.dest_path)))
            return
        if event.event_type == "deleted":
            # Windows can't say whether a deleted path was a file or a folder, and reports
            # a move between folders as a deletion plus a creation.
            self._service.forget(src, removed=True)
        elif event.event_type == "moved":
            self._service.forget(src, removed=True)
            self._service.offer(Path(os.fsdecode(event.dest_path)))
        elif event.event_type in ("created", "modified", "closed"):
            self._service.offer(src)


class WatchService:
    """Watches the root and processes files in the root and ``_Inbox`` once they are stable.

    Call ``start()`` then ``stop()``. ``root`` must already be validated and resolved.
    ``make_library(conn)`` builds the classifier's library index on the worker thread
    (loading the language model there, not on the caller's thread). Without it, or if it
    fails, every new file waits in ``_Inbox``.

    While paused, files that become ready are held, not processed; ``resume`` sorts them.
    ``on_status(message)`` reports progress such as loading the model, from any thread.
    Changes inside category folders make the worker rescan the library shortly after,
    so files the user moved between folders (or out of ``_Inbox``, e.g. on the iPhone)
    are learned from; ``on_learned(relocations)`` reports them. The library is rescanned
    before new files are sorted only if its folders changed (``LibraryIndex.ensure_current``).

    Every ``sweep_interval`` seconds (None: never) loose files in the root and ``_Inbox``
    are offered again and the library is rescanned (``sweep``): Windows can drop events on
    busy iCloud folders, and the tracker gives up on files that take too long to arrive.
    """

    def __init__(
        self,
        root: Path,
        db_path: Path,
        *,
        dry_run: bool = False,
        stable_seconds: float = DEFAULT_STABLE_SECONDS,
        ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_outcome: Callable[[Outcome], None] | None = None,
        make_library: LibraryFactory | None = None,
        threshold: float = DEFAULT_AUTO_THRESHOLD,
        margin: float = DEFAULT_AUTO_MARGIN,
        paused: bool = False,
        on_status: Callable[[str], None] | None = None,
        on_learned: Callable[[Sequence], None] | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        learn_delay: float = LEARN_DELAY_SECONDS,
        watched: Sequence[Path] = (),
        watched_extensions: Iterable[str] = (),
        sweep_interval: float | None = DEFAULT_SWEEP_SECONDS,
    ) -> None:
        self.root = root
        self.db_path = db_path
        self.dry_run = dry_run
        self.ignore_patterns = tuple(ignore_patterns)
        self.poll_interval = poll_interval
        self.tracker = StabilityTracker(stable_seconds)
        self.self_moves = SelfMoveRegistry()
        self._on_outcome = on_outcome
        self._make_library = make_library
        self.threshold = threshold
        self.margin = margin
        self._on_status = on_status
        self._on_learned = on_learned
        self._on_progress = on_progress
        self._sorting: list[int] | None = None  # [done, total] while new files are sorted
        self._learn_delay = learn_delay
        self.watched = tuple(watched)
        self.watched_extensions = {ext.casefold() for ext in watched_extensions}
        self.sweep_interval = sweep_interval if sweep_interval and sweep_interval > 0 else None
        self._changed_at: float | None = None
        self._changed_lock = threading.Lock()
        self._library_dirty = threading.Event()  # set from the observer's thread
        self._stop = threading.Event()
        self._paused = threading.Event()
        if paused:
            self._paused.set()
        self._held: set[Path] = set()
        self._held_lock = threading.Lock()
        self._observer: Observer | None = None
        self._worker: threading.Thread | None = None
        self._noted: set[Path] = set()
        self._noted_lock = threading.Lock()
        # Dry runs record nothing, so files they reported are remembered here instead.
        self._reported: set[tuple[Path, int, int]] = set()

    def start(self) -> None:
        """Settle leftover history and start watching. The worker then queues files that
        arrived while AutoShelf wasn't running."""
        self._settle_history()
        observer = Observer()
        observer.schedule(_EventHandler(self), str(self.root), recursive=True)
        for folder in self.watched:
            # Only files that arrive from now on; what is already there stays put.
            observer.schedule(_EventHandler(self), str(folder), recursive=False)
            log.info(
                "Also watching %s for new %s files",
                folder,
                ", ".join(sorted(self.watched_extensions)),
            )
        observer.start()
        self._observer = observer
        self._worker = threading.Thread(
            target=self._run_worker, name="autoshelf-worker", daemon=True
        )
        self._worker.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Stop watching and wait for the worker to finish the file it is on.

        With a ``timeout``, waits at most that long (the worker is a daemon thread, so
        an exiting app isn't held up by, say, a model download). Returns True if stopped.
        """
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout)
        if self._worker is not None:
            self._worker.join(timeout)
            return not self._worker.is_alive()
        return True

    @property
    def paused(self) -> bool:
        """True while automatic sorting is paused."""
        return self._paused.is_set()

    def pause(self) -> None:
        """Stop sorting automatically; files that arrive meanwhile wait until ``resume``."""
        self._paused.set()
        log.info("Sorting paused; new files will wait")

    def resume(self) -> None:
        """Sort again, starting with the files that arrived while paused."""
        self._paused.clear()
        with self._held_lock:
            held, self._held = self._held, set()
        log.info("Sorting resumed%s", f"; {len(held)} waiting file(s) next" if held else "")
        for path in held:
            self.offer(path)
        if held:
            self._begin_sorting(len(held))

    def offer(self, path: Path) -> None:
        """Consider a path from a file event; track it if it should be processed."""
        if self.self_moves.is_expected(path):
            return
        zone = zone_of(path, self.root)
        if zone is Zone.OUTSIDE and self._in_watched_folder(path):
            zone = Zone.DROP
        if zone is Zone.CATEGORY:
            self._note_once(path, "inside a category folder: kept there as an example")
            self._library_changed()
            return
        if zone not in PROCESSED_ZONES:
            return
        try:
            reason = ignore_reason(path, self.ignore_patterns)
        except FileNotFoundError:
            return
        if reason is not None:
            self._note_once(path, f"ignored: {reason}")
            return
        self.tracker.touch(path)

    def _in_watched_folder(self, path: Path) -> bool:
        return path.suffix.casefold() in self.watched_extensions and any(
            path.parent == folder for folder in self.watched
        )

    def forget(self, path: Path, *, removed: bool = False) -> None:
        """Stop tracking a path that was deleted or moved away.

        ``removed``: it was deleted or moved away. Unless AutoShelf moved it itself, and if
        it may have been a file or folder of the library, the library is rescanned before
        the next decision.
        """
        self.tracker.discard(path)
        with self._held_lock:
            self._held.discard(path)
        if removed and not self.self_moves.is_expected_removal(path) and self._in_library(path):
            self._library_changed()

    def folder_changed(self, folder: Path) -> None:
        """A folder was created, deleted or renamed: rescan if it is (or was) a category."""
        if folder != self.root and zone_of(folder / "x", self.root) is Zone.CATEGORY:
            self._library_changed()

    def _in_library(self, path: Path) -> bool:
        """True if a removed ``path`` may have been a library file or a category folder."""
        zone = zone_of(path, self.root)
        if zone is Zone.CATEGORY:
            return True
        # Directly in the root: a loose file, or a whole category folder.
        return zone is Zone.DROP and not path.name.startswith(RESERVED_PREFIX)

    def _library_changed(self) -> None:
        self._library_dirty.set()
        with self._changed_lock:
            self._changed_at = time.monotonic()

    def _library_refresh_due(self) -> bool:
        """True once (per burst of changes) when the folders have been quiet for a moment."""
        with self._changed_lock:
            if self._changed_at is None or time.monotonic() - self._changed_at < self._learn_delay:
                return False
            self._changed_at = None
            return True

    def _settle_history(self) -> None:
        conn = open_database(self.db_path)
        try:
            if self.dry_run:
                pending = repository.list_history_in_state(conn, "pending")
                if pending:
                    log.info(
                        "%d unfinished history entries will be settled on a real run", len(pending)
                    )
            else:
                reconcile_pending(conn)
        finally:
            conn.close()

    def sweep(self, conn: sqlite3.Connection, library: LibraryIndex | None) -> int:
        """Offer loose files again and rescan the library; returns how many were offered.

        Catches files whose events Windows dropped and files the tracker gave up on.
        """
        offered = self.offer_loose_files(conn)
        self._relearn(library)  # also catches folder changes whose events were lost
        if offered:
            log.info("Looked for missed files: %d to check", offered)
        return offered

    def offer_loose_files(self, conn: sqlite3.Connection) -> int:
        """Offer every file in the root and ``_Inbox`` that may need sorting.

        Files AutoShelf knows and that are unchanged (waiting in ``_Inbox``, or left in
        place after an undo) are skipped by their size and time alone, so nothing is
        read, hashed or downloaded from iCloud for them.
        """
        offered = 0
        for folder in (self.root, inbox_dir(self.root)):
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue  # no _Inbox yet
            for entry in entries:
                path = Path(entry.path)
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue  # vanished meanwhile
                if not self._known_unchanged(conn, path, st):
                    self.offer(path)
                    offered += 1
        return offered

    def _known_unchanged(self, conn: sqlite3.Connection, path: Path, st: os.stat_result) -> bool:
        if (path, st.st_size, st.st_mtime_ns) in self._reported:
            return True
        record = repository.get_file_by_path(conn, path)
        return (
            record is not None
            and record.status in ("inbox", "ignored")
            and record.size == st.st_size
            and record.mtime is not None
            and abs(record.mtime - st.st_mtime) <= _MTIME_TOLERANCE
        )

    def _open_library(self, conn: sqlite3.Connection) -> LibraryIndex | None:
        """Load the classifier and index the files already in category folders."""
        if self._make_library is None:
            return None
        self._status("Loading the language model...")
        self._progress(Progress(LOADING))
        try:
            library = self._make_library(conn)
            stats = library.refresh(progress=self._index_progress)
        except Exception as exc:
            message = f"Sorting by content is unavailable ({exc}); new files will wait in _Inbox"
            log.error("%s", message)
            log.debug("Library failure", exc_info=True)
            self._status(message)
            return None
        log.info(
            "Ready: %d folders, %d files in them indexed (%d read just now%s)",
            stats.folders,
            stats.files,
            stats.embedded,
            f", {stats.unreadable} unreadable" if stats.unreadable else "",
        )
        if stats.folders == 0:
            log.info("No folders yet: create folders in the root and new files will be sorted")
        self._status(f"Ready: {stats.folders} folders, {stats.files} files in them")
        self._report_learned(stats.relocations)
        return library

    def _relearn(self, library: LibraryIndex | None) -> None:
        """Rescan the folders after the user changed them, and learn from their moves."""
        if library is None:
            return
        self._library_dirty.clear()  # this rescan covers every change noted so far
        try:
            stats = library.refresh()
        except Exception:
            log.exception("Couldn't rescan your folders")
            return
        self._report_learned(stats.relocations)

    def _sync_library(self, library: LibraryIndex | None) -> None:
        """Before sorting new files, rescan the folders if they changed since last time."""
        if library is None:
            return
        if self._library_dirty.is_set():
            self._library_dirty.clear()
            library.mark_dirty()
        try:
            stats = library.ensure_current()
        except Exception:
            log.exception("Couldn't rescan your folders")
            return
        if stats is not None:
            self._report_learned(stats.relocations)

    def _report_learned(self, relocations: Sequence) -> None:
        learned = [item for item in relocations if item.correction_id is not None]
        if learned:
            names = ", ".join(f"{item.new_path.name} -> {item.to_folder}" for item in learned[:3])
            more = f" and {len(learned) - 3} more" if len(learned) > 3 else ""
            self._status(f"Learned from your moves: {names}{more}")
        if relocations and self._on_learned is not None:
            try:
                self._on_learned(list(relocations))
            except Exception:
                log.exception("Learned callback failed")

    def _index_progress(self, done: int, total: int) -> None:
        self._progress(Progress(READING, done, total))
        if done == total or done % _PROGRESS_EVERY == 0:
            log.info("Reading files already in your folders: %d/%d", done, total)
            self._status(f"Reading files already in your folders: {done}/{total}")

    def _progress(self, progress: Progress) -> None:
        if self._on_progress is not None:
            try:
                self._on_progress(progress)
            except Exception:
                log.exception("Progress callback failed")

    def _begin_sorting(self, count: int) -> None:
        """``count`` new files are about to be sorted (or held, while paused)."""
        if count == 0:
            self._sorting = None
            self._progress(Progress(READY))
        elif self._paused.is_set():
            self._sorting = None
            self._progress(Progress(PAUSED, 0, count))
        else:
            self._sorting = [0, count]
            self._progress(Progress(SORTING, 0, count))

    def _sorted_one(self) -> None:
        sorting = self._sorting
        if sorting is None:
            return
        sorting[0] += 1
        if sorting[0] >= sorting[1]:
            self._sorting = None
            self._progress(Progress(READY))
        else:
            self._progress(Progress(SORTING, sorting[0], sorting[1]))

    def _check_sorting_done(self) -> None:
        """Files that vanished or turned out to be ignored never arrive: stop waiting."""
        if self._sorting is not None and len(self.tracker) == 0:
            self._sorting = None
            self._progress(Progress(READY))

    def _status(self, message: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(message)
            except Exception:
                log.exception("Status callback failed")

    def _run_worker(self) -> None:
        conn = open_database(self.db_path)
        pipeline = Pipeline(
            conn,
            self.root,
            dry_run=self.dry_run,
            ignore_patterns=self.ignore_patterns,
            self_moves=self.self_moves,
            library=self._open_library(conn),
            threshold=self.threshold,
            margin=self.margin,
            watched=self.watched,
            watched_extensions=self.watched_extensions,
        )
        try:
            # files that arrived while AutoShelf wasn't running
            self._begin_sorting(self.offer_loose_files(conn))
            next_sweep = self._next_sweep()
            while not self._stop.wait(self.poll_interval):
                ready = self.tracker.poll()
                if ready and not self._paused.is_set():
                    self._sync_library(pipeline.library)  # once per batch, if needed
                for path in ready:
                    if self._stop.is_set():
                        break
                    if self._paused.is_set():
                        self._hold(path)
                        continue
                    try:
                        self._report(pipeline.process(path))
                    except Exception:
                        log.exception("Error while reporting on %s; still watching", path)
                    self._sorted_one()
                if not ready:
                    self._check_sorting_done()
                if self._library_refresh_due():
                    self._relearn(pipeline.library)
                if next_sweep is not None and time.monotonic() >= next_sweep:
                    try:
                        self.sweep(conn, pipeline.library)
                    except Exception:
                        log.exception("Couldn't look for missed files; still watching")
                    next_sweep = self._next_sweep()
        finally:
            conn.close()

    def _next_sweep(self) -> float | None:
        return None if self.sweep_interval is None else time.monotonic() + self.sweep_interval

    def _hold(self, path: Path) -> None:
        with self._held_lock:
            if path in self._held:
                return
            self._held.add(path)
        log.info("Paused: %s will be sorted when you resume", self._display(path))

    def _report(self, outcome: Outcome) -> None:
        if self.dry_run:
            self._remember_reported(outcome.path)
        name = self._display(outcome.path)
        if outcome.kind is OutcomeKind.MOVED:
            log.info(
                "Moved %s -> %s (#%d: %s)",
                name,
                self._display(outcome.dst),
                outcome.history_id,
                outcome.message,
            )
        elif outcome.kind is OutcomeKind.WOULD_MOVE:
            log.info(
                "[dry run] Would move %s -> %s (%s)",
                name,
                self._display(outcome.dst),
                outcome.message,
            )
        elif outcome.kind is OutcomeKind.WAITING:
            log.info("%s %s", name, outcome.message)
        elif outcome.kind is OutcomeKind.SKIPPED:
            log.info("Left %s where it is: %s", name, outcome.message)
        else:
            log.warning("Could not process %s: %s", name, outcome.message)
        if outcome.extraction is not None:
            log.info("  %s", outcome.extraction.summary())
        for duplicate in outcome.duplicates:
            log.info(
                "  %s is an exact duplicate of %s (flagged only; nothing is deleted)",
                name,
                self._display(duplicate),
            )
        if self._on_outcome is not None:
            self._on_outcome(outcome)

    def _remember_reported(self, path: Path) -> None:
        """A dry run reports a file once, not again at every sweep (until it changes)."""
        try:
            st = path.stat()
        except OSError:
            return
        if len(self._reported) >= _MAX_NOTED_PATHS:
            self._reported.clear()
        self._reported.add((path, st.st_size, st.st_mtime_ns))

    def _note_once(self, path: Path, message: str) -> None:
        with self._noted_lock:
            if path in self._noted:
                return
            if len(self._noted) >= _MAX_NOTED_PATHS:
                self._noted.clear()
            self._noted.add(path)
        log.info("Left %s where it is: %s", self._display(path), message)

    def _display(self, path: Path | None) -> str:
        if path is None:
            return "?"
        try:
            return f"'{path.relative_to(self.root)}'"
        except ValueError:
            return f"'{path}'"
