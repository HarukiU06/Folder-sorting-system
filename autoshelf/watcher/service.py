"""Runs the watcher: watchdog events -> stability tracker -> pipeline, off the main thread."""

import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from autoshelf.actions.history import reconcile_pending
from autoshelf.config import (
    DEFAULT_AUTO_MARGIN,
    DEFAULT_AUTO_THRESHOLD,
    DEFAULT_IGNORE_PATTERNS,
    DEFAULT_STABLE_SECONDS,
)
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.layout import PROCESSED_ZONES, Zone, inbox_dir, zone_of
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Outcome, OutcomeKind, Pipeline
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.selfmoves import SelfMoveRegistry
from autoshelf.watcher.stability import StabilityTracker

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5
_MAX_NOTED_PATHS = 5000
_PROGRESS_EVERY = 25

LibraryFactory = Callable[[sqlite3.Connection], LibraryIndex]


class _EventHandler(FileSystemEventHandler):
    """Translates watchdog events into ``offer``/``forget`` calls on the service."""

    def __init__(self, service: "WatchService") -> None:
        super().__init__()
        self._service = service

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        src = Path(os.fsdecode(event.src_path))
        if event.event_type == "deleted":
            self._service.forget(src)
        elif event.event_type == "moved":
            self._service.forget(src)
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

    def start(self) -> None:
        """Settle leftover history, start watching, and queue files already waiting."""
        self._settle_history()
        observer = Observer()
        observer.schedule(_EventHandler(self), str(self.root), recursive=True)
        observer.start()
        self._observer = observer
        self._worker = threading.Thread(
            target=self._run_worker, name="autoshelf-worker", daemon=True
        )
        self._worker.start()
        self._queue_existing_files()

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

    def offer(self, path: Path) -> None:
        """Consider a path from a file event; track it if it should be processed."""
        if self.self_moves.is_expected(path):
            return
        zone = zone_of(path, self.root)
        if zone is Zone.CATEGORY:
            self._note_once(path, "inside a category folder: kept there as an example")
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

    def forget(self, path: Path) -> None:
        """Stop tracking a path that was deleted or moved away."""
        self.tracker.discard(path)
        with self._held_lock:
            self._held.discard(path)

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

    def _queue_existing_files(self) -> None:
        """Queue files that arrived while AutoShelf wasn't running."""
        for folder in (self.root, inbox_dir(self.root)):
            if not folder.is_dir():
                continue
            for entry in folder.iterdir():
                if entry.is_file():
                    self.offer(entry)

    def _open_library(self, conn: sqlite3.Connection) -> LibraryIndex | None:
        """Load the classifier and index the files already in category folders."""
        if self._make_library is None:
            return None
        self._status("Loading the language model...")
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
        return library

    def _index_progress(self, done: int, total: int) -> None:
        if done == total or done % _PROGRESS_EVERY == 0:
            log.info("Reading files already in your folders: %d/%d", done, total)
            self._status(f"Reading files already in your folders: {done}/{total}")

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
        )
        try:
            while not self._stop.wait(self.poll_interval):
                for path in self.tracker.poll():
                    if self._stop.is_set():
                        break
                    if self._paused.is_set():
                        self._hold(path)
                        continue
                    try:
                        self._report(pipeline.process(path))
                    except Exception:
                        log.exception("Error while reporting on %s; still watching", path)
        finally:
            conn.close()

    def _hold(self, path: Path) -> None:
        with self._held_lock:
            if path in self._held:
                return
            self._held.add(path)
        log.info("Paused: %s will be sorted when you resume", self._display(path))

    def _report(self, outcome: Outcome) -> None:
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
