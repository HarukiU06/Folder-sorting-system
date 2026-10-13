"""The desktop app's logic, without Qt: settings, the watcher, and every user action.

The UI calls into this and only draws. Each method opens its own database connection,
so any method may be called from a background thread. Methods that read files or load
the language model are slow; the UI must not call them on its own thread.
"""

import contextlib
import logging
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from autoshelf.actions import learning
from autoshelf.actions import rules as rule_engine
from autoshelf.actions import undo as undo_actions
from autoshelf.actions.inbox import InboxItem, assign, inbox_items, resolve_folder
from autoshelf.actions.mover import BeforeRename, MoveResult
from autoshelf.config import (
    AppPaths,
    ConfigError,
    Settings,
    load_settings,
    save_settings,
    validate_root,
)
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.instance import LOCK_FILENAME, WatchLock
from autoshelf.layout import PROCESSED_ZONES, inbox_dir, zone_of
from autoshelf.ml.embedder import Embedder, is_downloaded
from autoshelf.ml.library import LibraryIndex, scan_library
from autoshelf.pipeline import Outcome
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.service import WatchService

log = logging.getLogger(__name__)

EmbedderLoader = Callable[[str, Path], Embedder]
STOP_TIMEOUT_SECONDS = 5.0


def default_loader(model: str, cache_dir: Path) -> Embedder:
    """Load the real model (looked up at call time, so tests can replace it)."""
    from autoshelf.ml import embedder

    return embedder.load_embedder(model, cache_dir)


DOWNLOAD_MESSAGE = (
    "Downloading the language model (about 470 MB, first run only); new files will be "
    "sorted when it is ready..."
)


def library_factory(
    paths: AppPaths,
    settings: Settings,
    root: Path,
    loader: EmbedderLoader = default_loader,
    on_status: Callable[[str], None] | None = None,
) -> Callable[[sqlite3.Connection], LibraryIndex]:
    """Build ``make_library(conn)`` for a watcher: loads the model where it is called."""

    def make_library(conn: sqlite3.Connection) -> LibraryIndex:
        if on_status is not None and not is_downloaded(settings.embedding_model, paths.model_dir):
            on_status(DOWNLOAD_MESSAGE)
        embedder = loader(settings.embedding_model, paths.model_dir)
        return LibraryIndex(conn, root, embedder, ignore_patterns=settings.ignore_patterns)

    return make_library


def watch_lock(paths: AppPaths) -> WatchLock:
    """The lock that lets only one process sort the library in ``paths``."""
    return WatchLock(paths.home / LOCK_FILENAME)


@dataclass(frozen=True)
class FolderInfo:
    """A category folder as shown in the app."""

    key: str
    files: int  # files directly in it
    description: str


@dataclass(frozen=True)
class RuleTest:
    """What the rules (or, failing them, the classifier) would do with a file."""

    path: Path
    rule: repository.RuleRecord | None
    target: str | None  # the folder it would go to; None: it would wait in _Inbox
    message: str


class AppService:
    """Owns the watcher for the app's lifetime and performs the user's actions."""

    def __init__(self, paths: AppPaths, *, loader: EmbedderLoader = default_loader) -> None:
        self.paths = paths
        self._loader = loader
        self._lock = watch_lock(paths)
        self._service: WatchService | None = None
        self._service_lock = threading.RLock()
        self.on_outcome: Callable[[Outcome], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        self.on_learned: Callable[[list[learning.Relocation]], None] | None = None

    # --- settings -----------------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = open_database(self.paths.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def settings(self) -> Settings:
        """The current settings, freshly loaded."""
        with self._connect() as conn:
            return load_settings(conn)

    def save_settings(self, settings: Settings) -> None:
        """Store settings. Call ``restart`` to apply ones the watcher uses."""
        with self._connect() as conn:
            save_settings(conn, settings)

    def root(self) -> Path | None:
        """The configured root folder, if it is still a valid one."""
        configured = self.settings().root
        if configured is None:
            return None
        try:
            return validate_root(Path(configured), self.paths)
        except ConfigError as exc:
            log.warning("The configured root can't be used: %s", exc)
            return None

    def check_root(self, folder: Path) -> Path:
        """Validate a folder the user picked as the root (raises ConfigError)."""
        return validate_root(folder, self.paths)

    def set_root(self, folder: Path) -> Path:
        """Validate and store a new root. Call ``restart`` afterwards."""
        root = self.check_root(folder)
        self.save_settings(replace(self.settings(), root=str(root)))
        return root

    def loose_files(self, root: Path) -> int:
        """Files that sorting would act on right away: in the root, or new in _Inbox."""
        settings = self.settings()
        count = 0
        with self._connect() as conn:
            for folder in (root, inbox_dir(root)):
                if not folder.is_dir():
                    continue
                for path in folder.iterdir():
                    with contextlib.suppress(OSError):
                        if (
                            path.is_file()
                            and zone_of(path, root) in PROCESSED_ZONES
                            and ignore_reason(path, settings.ignore_patterns) is None
                            and not _is_waiting(conn, path)
                        ):
                            count += 1
        return count

    # --- the watcher ----------------------------------------------------------------

    def acquire(self) -> bool:
        """Take the single-sorter lock for this app session. False if another has it."""
        return self._lock.acquire()

    def lock_holder(self) -> int | None:
        """Process id of whoever holds the lock, if known."""
        return self._lock.holder_pid()

    def release(self) -> None:
        """Stop the watcher and give the lock back (on quit)."""
        self.stop()
        self._lock.release()

    @property
    def watching(self) -> bool:
        """True while the watcher runs."""
        return self._service is not None

    @property
    def paused(self) -> bool:
        """True if automatic sorting is paused (a stored setting)."""
        return self.settings().paused

    def start(self) -> bool:
        """Start watching the configured root. Returns False if there is no usable root."""
        if not self._lock.held:
            raise RuntimeError("AppService.acquire() must succeed before start()")
        with self._service_lock:
            if self._service is not None:
                return True
            root = self.root()
            if root is None:
                return False
            settings = self.settings()
            service = WatchService(
                root,
                self.paths.db_path,
                stable_seconds=settings.stable_seconds,
                ignore_patterns=settings.ignore_patterns,
                make_library=library_factory(
                    self.paths, settings, root, self._loader, on_status=self._status
                ),
                threshold=settings.auto_threshold,
                margin=settings.auto_margin,
                paused=settings.paused,
                on_outcome=self._outcome,
                on_status=self._status,
                on_learned=self._learned,
            )
            service.start()
            self._service = service
            log.info("Watching %s%s", root, " (paused)" if settings.paused else "")
            return True

    def stop(self) -> None:
        """Stop watching (waits a few seconds at most for the file in progress)."""
        with self._service_lock:
            service, self._service = self._service, None
        if service is not None and not service.stop(timeout=STOP_TIMEOUT_SECONDS):
            log.warning("The watcher is still busy; it will stop when the app exits")

    def restart(self) -> bool:
        """Apply changed settings by restarting the watcher."""
        self.stop()
        return self.start()

    def set_paused(self, paused: bool) -> None:
        """Pause or resume automatic sorting, and remember the choice."""
        self.save_settings(replace(self.settings(), paused=paused))
        with self._service_lock:
            service = self._service
        if service is None:
            return
        if paused:
            service.pause()
        else:
            service.resume()

    def _outcome(self, outcome: Outcome) -> None:
        if self.on_outcome is not None:
            self.on_outcome(outcome)

    def _status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)

    def _learned(self, relocations: list[learning.Relocation]) -> None:
        if self.on_learned is not None:
            self.on_learned(relocations)

    def _before_rename(self) -> BeforeRename | None:
        """Tell the running watcher about the app's own moves, so it ignores them."""
        with self._service_lock:
            service = self._service
        if service is None:
            return None
        return lambda src, dst: service.self_moves.expect(dst)

    # --- actions (slow: call these off the UI thread) -----------------------------------

    def _require_root(self) -> Path:
        root = self.root()
        if root is None:
            raise ConfigError("Choose the folder to sort first (Settings)")
        return root

    def library(self, conn: sqlite3.Connection, root: Path) -> LibraryIndex:
        """A refreshed library index for ``root`` (loads the model on first use)."""
        settings = self.settings()
        library = library_factory(self.paths, settings, root, self._loader)(conn)
        library.refresh()
        return library

    def inbox(self) -> list[InboxItem]:
        """Files waiting in _Inbox, with previews and fresh suggestions."""
        root = self._require_root()
        settings = self.settings()
        with self._connect() as conn:
            try:
                library = self.library(conn, root)
            except Exception as exc:
                log.warning("No suggestions available: %s", exc)
                library = None
            return inbox_items(
                conn,
                root,
                library,
                threshold=settings.auto_threshold,
                margin=settings.auto_margin,
                ignore_patterns=settings.ignore_patterns,
            )

    def assign(self, path: Path, folder: str) -> MoveResult:
        """Move a waiting file into the folder the user chose."""
        root = self._require_root()
        settings = self.settings()
        with self._connect() as conn:
            return assign(
                conn,
                path,
                folder,
                root=root,
                ignore_patterns=settings.ignore_patterns,
                before_rename=self._before_rename(),
            )

    def undo(self, history_id: int, *, force: bool = False) -> MoveResult:
        """Undo one history entry."""
        with self._connect() as conn:
            return undo_actions.undo(
                conn, history_id, force=force, before_rename=self._before_rename()
            )

    def undo_last(self, count: int, *, force: bool = False) -> list[MoveResult]:
        """Undo the ``count`` most recent moves, newest first."""
        with self._connect() as conn:
            return undo_actions.undo_last(
                conn, count, force=force, before_rename=self._before_rename()
            )

    def history(self, limit: int = 200) -> list[repository.HistoryEntry]:
        """The most recent history entries, newest first."""
        with self._connect() as conn:
            return repository.list_history(conn, limit)

    def folders(self) -> list[FolderInfo]:
        """The category folders with file counts and descriptions."""
        root = self._require_root()
        settings = self.settings()
        scan = scan_library(root, settings.ignore_patterns)
        counts = dict.fromkeys(scan.folders, 0)
        for item in scan.files:
            counts[item.folder] += 1
        with self._connect() as conn:
            descriptions = repository.folder_descriptions(conn)
        return [
            FolderInfo(key, counts[key], descriptions.get(key.casefold(), ""))
            for key in scan.folders
        ]

    def describe(self, folder: str, description: str) -> str:
        """Set a folder's description and return the folder's key."""
        root = self._require_root()
        key = resolve_folder(root, folder, self.settings().ignore_patterns)
        with self._connect() as conn:
            repository.set_folder_description(conn, key, description.strip())
        return key

    # --- learning -----------------------------------------------------------------

    def correct(self, history_id: int, folder: str) -> MoveResult:
        """Move a file that AutoShelf sorted into the right folder, and learn from it."""
        root = self._require_root()
        key = resolve_folder(root, folder, self.settings().ignore_patterns)
        with self._connect() as conn:
            return learning.correct(
                conn, history_id, key, root=root, before_rename=self._before_rename()
            )

    def corrections(self) -> list:
        """Every recorded correction, oldest first."""
        with self._connect() as conn:
            return repository.list_corrections(conn)

    # --- rules --------------------------------------------------------------------

    def rules(self) -> list[repository.RuleRecord]:
        """The rules in the order they are checked."""
        with self._connect() as conn:
            return repository.list_rules(conn)

    def _checked_rule(self, name: str, kind: str, pattern: str, target: str) -> str:
        """Validate a rule and return the key of its (existing) folder."""
        rule_engine.check_rule(name, kind, pattern)
        root = self._require_root()
        return resolve_folder(root, target, self.settings().ignore_patterns)

    def add_rule(
        self, name: str, kind: str, pattern: str, target: str, *, enabled: bool = True
    ) -> repository.RuleRecord:
        """Add a rule after the existing ones. Raises RuleError or AssignError if invalid."""
        key = self._checked_rule(name, kind, pattern, target)
        with self._connect() as conn:
            rule_id = repository.insert_rule(
                conn,
                name=name.strip(),
                kind=kind,
                pattern=pattern.strip(),
                target_folder=key,
                enabled=enabled,
            )
            return repository.get_rule(conn, rule_id)

    def update_rule(self, rule: repository.RuleRecord) -> repository.RuleRecord:
        """Save changes to a rule (validated like a new one)."""
        key = self._checked_rule(rule.name, rule.kind, rule.pattern, rule.target_folder)
        updated = replace(
            rule, name=rule.name.strip(), pattern=rule.pattern.strip(), target_folder=key
        )
        with self._connect() as conn:
            repository.update_rule(conn, updated)
        return updated

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        """Switch a rule on or off."""
        with self._connect() as conn:
            rule = repository.get_rule(conn, rule_id)
            if rule is not None:
                repository.update_rule(conn, replace(rule, enabled=enabled))

    def delete_rule(self, rule_id: int) -> None:
        """Remove a rule."""
        with self._connect() as conn:
            repository.delete_rule(conn, rule_id)

    def move_rule(self, rule_id: int, offset: int) -> None:
        """Move a rule earlier (negative ``offset``) or later in the checking order."""
        with self._connect() as conn:
            ids = [rule.id for rule in repository.list_rules(conn)]
            if rule_id not in ids:
                return
            index = ids.index(rule_id)
            new_index = max(0, min(len(ids) - 1, index + offset))
            ids.insert(new_index, ids.pop(index))
            repository.reorder_rules(conn, ids)

    def test_rules(self, path: Path) -> RuleTest:
        """Which rule would handle ``path`` (the file is only read, never moved)."""
        from autoshelf.extract import extract

        extraction = extract(path)
        with self._connect() as conn:
            rules = repository.list_rules(conn, enabled_only=True)
        match = rule_engine.first_match(rules, path, extraction)
        if match is not None:
            rule = match.rule
            message = (
                f"Rule {rule.name!r} matches ({match.match}): it would go to {rule.target_folder}"
            )
            return RuleTest(path, rule, rule.target_folder, message)
        message = "No rule matches this file; AutoShelf would decide by its content."
        return RuleTest(path, None, None, message)


def _is_waiting(conn: sqlite3.Connection, path: Path) -> bool:
    record = repository.get_file_by_path(conn, path)
    return record is not None and record.status in ("inbox", "ignored")
