"""Runs the watcher: watchdog events -> stability tracker -> pipeline, off the main thread."""

import logging
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from autoshelf.actions.history import reconcile_pending
from autoshelf.config import DEFAULT_IGNORE_PATTERNS, DEFAULT_STABLE_SECONDS
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.layout import PROCESSED_ZONES, Zone, inbox_dir, zone_of
from autoshelf.pipeline import Outcome, OutcomeKind, Pipeline
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.selfmoves import SelfMoveRegistry
from autoshelf.watcher.stability import StabilityTracker

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 0.5
_MAX_NOTED_PATHS = 5000


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
    ) -> None:
        self.root = root
        self.db_path = db_path
        self.dry_run = dry_run
        self.ignore_patterns = tuple(ignore_patterns)
        self.poll_interval = poll_interval
        self.tracker = StabilityTracker(stable_seconds)
        self.self_moves = SelfMoveRegistry()
        self._on_outcome = on_outcome
        self._stop = threading.Event()
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

    def stop(self) -> None:
        """Stop watching and wait for the worker to finish the file it is on."""
        self._stop.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
        if self._worker is not None:
            self._worker.join()

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

    def _run_worker(self) -> None:
        conn = open_database(self.db_path)
        pipeline = Pipeline(
            conn,
            self.root,
            dry_run=self.dry_run,
            ignore_patterns=self.ignore_patterns,
            self_moves=self.self_moves,
        )
        try:
            while not self._stop.wait(self.poll_interval):
                for path in self.tracker.poll():
                    if self._stop.is_set():
                        break
                    try:
                        self._report(pipeline.process(path))
                    except Exception:
                        log.exception("Error while reporting on %s; still watching", path)
        finally:
            conn.close()

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
        elif outcome.kind is OutcomeKind.SKIPPED:
            log.info("Left %s where it is: %s", name, outcome.message)
        else:
            log.warning("Could not process %s: %s", name, outcome.message)
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
