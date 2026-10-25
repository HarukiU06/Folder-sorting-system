"""The desktop app's logic, without Qt: settings, the watcher, and every user action.

The UI calls into this and only draws. Each method opens its own database connection,
so any method may be called from a background thread. Methods that read files or load
the language model are slow; the UI must not call them on its own thread.
"""

import contextlib
import logging
import sqlite3
import tempfile
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from autoshelf import links, llm
from autoshelf.actions import learning
from autoshelf.actions import rules as rule_engine
from autoshelf.actions import undo as undo_actions
from autoshelf.actions.create import Created, place_new_file
from autoshelf.actions.inbox import InboxItem, assign, inbox_items, resolve_folder
from autoshelf.actions.mover import BeforeRename, MoveResult
from autoshelf.config import (
    CONFIDENCE_LEVELS,
    AppPaths,
    ConfigError,
    Settings,
    load_settings,
    save_settings,
    validate_outside_folder,
    validate_root,
)
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.extract import guard as read_guard
from autoshelf.features import ask as ask_features
from autoshelf.features import convert as convert_features
from autoshelf.features import digest as digest_features
from autoshelf.features import duplicates as duplicate_features
from autoshelf.features import new_folders as folder_features
from autoshelf.features import organize as organize_features
from autoshelf.features import rename as rename_features
from autoshelf.features import study as study_features
from autoshelf.features import summaries as summary_features
from autoshelf.features import tags as tag_features
from autoshelf.features.related import Related, related_files
from autoshelf.features.rule_suggestions import RuleSuggestion, suggest_rules
from autoshelf.features.search import SearchHit, SearchIndex, folder_label
from autoshelf.features.stats import LibraryStats, library_stats
from autoshelf.fsutil import is_within
from autoshelf.instance import LOCK_FILENAME, WatchLock
from autoshelf.layout import PROCESSED_ZONES, Zone, inbox_dir, zone_of
from autoshelf.ml import calibrate as calibration
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


def valid_watched(paths: AppPaths, settings: Settings, root: Path) -> list[Path]:
    """The opted-in watched folders that are still safe to take files from."""
    valid = []
    for folder in settings.watched_folders:
        try:
            valid.append(validate_outside_folder(Path(folder), root, paths))
        except ConfigError as exc:
            log.warning("Not watching %s: %s", folder, exc)
    return valid


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
class FileInfo:
    """One file as shown in the Library's detail panel."""

    path: Path
    folder: str
    title: str | None
    excerpt: str | None
    summary: str | None
    tags: list[str]
    size: int
    modified: float
    tag_suggestions: list[str]  # from the last decision: close runner-up folders


@dataclass(frozen=True)
class SortPlan:
    """Where files chosen in Explorer would go: proposals to apply, files already where
    they belong, and files that can't be sorted (with the reason)."""

    proposals: list[organize_features.Proposal]
    in_place: list[Path]
    refused: list[tuple[Path, str]]


@dataclass(frozen=True)
class WhyHere:
    """Why a file is where it is: AutoShelf's last move of it (None: AutoShelf has no
    record of moving it), and whether that move put it where it is now."""

    path: Path
    entry: repository.HistoryEntry | None
    here: bool


@dataclass(frozen=True)
class RuleTest:
    """Which rule would handle a file (``wording.rule_test`` puts it in words)."""

    path: Path
    rule: repository.RuleRecord | None  # None: no rule matches; the content decides
    target: str | None  # the folder it would go to (None: AutoShelf decides)
    matched: tuple[dict, ...] = ()  # what the rule's conditions found
    new_name: str | None = None  # the name the rule would give it


def _same_path(stored: str, path: Path) -> bool:
    return str(Path(stored)).casefold() == str(path).casefold()


class AppService:
    """Owns the watcher for the app's lifetime and performs the user's actions."""

    def __init__(
        self,
        paths: AppPaths,
        *,
        loader: EmbedderLoader = default_loader,
        root: Path | None = None,
    ) -> None:
        self.paths = paths
        self._loader = loader
        self._root_override = root  # the CLI's --root
        self._lock = watch_lock(paths)
        self._service: WatchService | None = None
        self._service_lock = threading.RLock()
        self.on_outcome: Callable[[Outcome], None] | None = None
        self.on_status: Callable[[str], None] | None = None
        self.on_learned: Callable[[list[learning.Relocation]], None] | None = None
        self._search_index = SearchIndex()

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
        configured = self._root_override or self.settings().root
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
        if self._lock.held:
            return True
        if not self._lock.acquire():
            return False
        # Files that crashed the last sorting process while being read aren't read again.
        read_guard.start(self.paths.home, record=True)
        return True

    def lock_holder(self) -> int | None:
        """Process id of whoever holds the lock, if known."""
        return self._lock.holder_pid()

    def release(self) -> None:
        """Stop the watcher and give the lock back (on quit)."""
        self.stop()
        if self._lock.held:
            read_guard.stop()
        self._lock.release()

    # --- files AutoShelf no longer reads (they stopped it while being read) ------------

    def skipped_files(self) -> list[read_guard.SkippedFile]:
        """Files only sorted by name because AutoShelf stopped while reading them (a crash,
        or a power cut or forced restart, which look the same).

        Each is shown where it is now, if AutoShelf knows (it was sorted by its name since).
        """
        shown = []
        with self._connect() as conn:
            for item in read_guard.skipped_files(self.paths.home):
                found = repository.path_of_version(conn, item.size, item.mtime_ns)
                shown.append(replace(item, path=Path(found)) if found else item)
        return shown

    def newly_skipped(self) -> list[Path]:
        """Files that stopped AutoShelf's previous run and are skipped from this start on."""
        return read_guard.newly_skipped()

    def clear_skipped(self, paths: list[Path] | None = None) -> int:
        """Read these skipped files (or all of them) again; returns how many were cleared.

        Files in your folders that were indexed by name only are read on the next rescan.
        """
        cleared = read_guard.clear(self.paths.home, paths)
        if cleared:
            with self._connect() as conn:
                repository.bump_library_version(conn)
        return cleared

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
            watched = valid_watched(self.paths, settings, root)
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
                watched=watched,
                watched_extensions=settings.watched_extensions,
                sweep_interval=settings.sweep_minutes * 60,
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
        return service.self_moves.expect_move

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

    def assign(self, path: Path, folder: str, tags: list[str] | None = None) -> MoveResult:
        """Move a waiting file into the folder the user chose (and tag it, if asked)."""
        root = self._require_root()
        settings = self.settings()
        with self._connect() as conn:
            result = assign(
                conn,
                path,
                folder,
                root=root,
                ignore_patterns=settings.ignore_patterns,
                before_rename=self._before_rename(),
            )
            if tags:
                tag_features.tag_file(conn, result.dst, tags, root)
            return result

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

    def folder_files(self, folder: str) -> list[Path]:
        """The files directly in one of your folders, by name (reads no file)."""
        root = self._require_root()
        scan = scan_library(root, self.settings().ignore_patterns)
        return [item.path for item in scan.files if item.folder == folder]

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

    # --- library: search, related files, tags --------------------------------------

    def _embedder(self) -> Embedder:
        settings = self.settings()
        return self._loader(settings.embedding_model, self.paths.model_dir)

    def search(self, query: str, limit: int = 30) -> list[SearchHit]:
        """Files matching ``query`` by meaning or by exact words (loads the model)."""
        root = self._require_root()
        embedder = self._embedder()
        with self._connect() as conn:
            return self._search_index.search(conn, root, embedder, query, limit=limit)

    def related(self, path: Path) -> list[Related]:
        """The files most similar to ``path``."""
        root = self._require_root()
        with self._connect() as conn:
            return related_files(conn, root, path)

    def file_info(self, path: Path) -> FileInfo:
        """What the Library shows about one file."""
        root = self._require_root()
        st = path.stat()
        with self._connect() as conn:
            record = repository.get_file_by_path(conn, path)
            title, excerpt = (
                repository.get_file_content(conn, record.id) if record else (None, None)
            )
            summary = repository.get_summary(conn, record.id) if record else None
            tags = repository.tags_of(conn, record.id) if record else []
            decision = repository.last_decision(conn, record.id) if record else None
        suggestions = decision.details.get("tag_suggestions", []) if decision else []
        return FileInfo(
            path,
            folder_label(path, root),
            title,
            excerpt,
            summary,
            tags,
            st.st_size,
            st.st_mtime,
            [name for name in suggestions if name not in tags],
        )

    def mark_opened(self, path: Path) -> None:
        """Note that the user opened a file from AutoShelf (for the dashboard)."""
        with self._connect() as conn:
            record = repository.get_file_by_path(conn, path)
            if record is not None:
                repository.mark_opened(conn, record.id)

    def tags(self) -> list[tuple[str, int]]:
        """Every tag in use, with how many files have it."""
        with self._connect() as conn:
            return repository.tag_counts(conn)

    def tagged(self, name: str) -> list[Path]:
        """Files with a tag (a virtual folder)."""
        with self._connect() as conn:
            return tag_features.tagged_files(conn, name)

    def tag(self, path: Path, names: list[str]) -> list[str]:
        """Tag a file; returns the tags that were new to it."""
        root = self._require_root()
        with self._connect() as conn:
            return tag_features.tag_file(conn, path, names, root)

    def untag(self, path: Path, name: str) -> bool:
        """Remove a tag from a file."""
        root = self._require_root()
        with self._connect() as conn:
            return tag_features.untag_file(conn, path, name, root)

    # --- tidying up: duplicates, versions, organize ------------------------------------

    def duplicates(self) -> list[duplicate_features.DuplicateGroup]:
        """Groups of identical files (refreshes the index first, so new files count)."""
        root = self._require_root()
        with self._connect() as conn:
            try:
                self.library(conn, root)
            except Exception as exc:
                log.warning("Couldn't refresh the index: %s", exc)
            return duplicate_features.exact_duplicates(conn, root)

    def keep_one(self, keep: Path, others: list[Path]) -> list[MoveResult]:
        """Keep one copy and move the other identical copies into _Duplicates."""
        root = self._require_root()
        with self._connect() as conn:
            return duplicate_features.keep_one(
                conn, root, keep, others, before_rename=self._before_rename()
            )

    def versions(self) -> list[duplicate_features.VersionGroup]:
        """Groups of files that look like versions of one document."""
        root = self._require_root()
        with self._connect() as conn:
            model = self.settings().embedding_model
            try:
                model = self.library(conn, root).model
            except Exception as exc:
                log.warning("Couldn't refresh the index: %s", exc)
            return duplicate_features.possible_versions(conn, root, model)

    def check_organize_folder(self, folder: Path) -> Path:
        """Validate a folder to organize: inside the root (not a reserved folder), or a
        safe folder outside it on the same drive."""
        root = self._require_root()
        resolved = Path(folder).resolve()
        if resolved == root or resolved.is_relative_to(root):
            if resolved != root and zone_of(resolved / "x", root) is Zone.RESERVED:
                raise ConfigError(f"{resolved.name} is AutoShelf's own folder")
            return resolved
        return validate_outside_folder(resolved, root, self.paths)

    def plan_organize(
        self, folder: Path, *, recursive: bool = False, progress=None
    ) -> list[organize_features.Proposal]:
        """Proposed moves for every file in ``folder`` (reads files, moves nothing)."""
        root = self._require_root()
        folder = self.check_organize_folder(folder)
        settings = self.settings()
        with self._connect() as conn:
            try:
                library = self.library(conn, root)
            except Exception as exc:
                log.warning("Only rules can propose folders: %s", exc)
                library = None
            return organize_features.plan(
                conn,
                root,
                library,
                folder,
                recursive=recursive,
                threshold=settings.auto_threshold,
                margin=settings.auto_margin,
                ignore_patterns=settings.ignore_patterns,
                progress=progress,
            )

    def apply_organize(
        self, folder: Path, proposals: list[organize_features.Proposal]
    ) -> organize_features.OrganizeResult:
        """Move the ticked proposals as one batch (undo it with ``undo_batch``)."""
        root = self._require_root()
        folder = self.check_organize_folder(folder)
        with self._connect() as conn:
            return organize_features.apply(
                conn, root, proposals, folder=folder, before_rename=self._before_rename()
            )

    def undo_batch(self, batch_id: str, *, force: bool = False) -> list[MoveResult]:
        """Undo every move of one batch, newest first."""
        with self._connect() as conn:
            return undo_actions.undo_batch(
                conn, batch_id, force=force, before_rename=self._before_rename()
            )

    # --- files chosen in Explorer (Sort with AutoShelf, Why is this here?) ----------------------

    def plan_sort(self, paths: list[Path]) -> SortPlan:
        """Where each file would go (reads them, moves nothing). Files outside the root must
        be in a folder on the same drive that may be organized; others are refused with
        the reason. Raises ConfigError if none of them can be sorted."""
        root = self._require_root()
        settings = self.settings()
        files, refused = [], []
        for path in dict.fromkeys(Path(item) for item in paths):
            try:
                files.append(self._sortable(path, root))
            except ConfigError as exc:
                refused.append((path, str(exc)))
        if not files:
            raise ConfigError("\n".join(reason for _, reason in refused) or "No files chosen")
        with self._connect() as conn:
            try:
                library = self.library(conn, root)
            except Exception as exc:
                log.warning("Only rules can propose folders: %s", exc)
                library = None
            proposals, in_place = organize_features.plan_files(
                conn,
                root,
                library,
                files,
                threshold=settings.auto_threshold,
                margin=settings.auto_margin,
            )
        return SortPlan(proposals, in_place, refused)

    def apply_sort(
        self, proposals: list[organize_features.Proposal]
    ) -> organize_features.OrganizeResult:
        """Move the ticked proposals of ``plan_sort`` as one batch (undo with ``undo_batch``)."""
        root = self._require_root()
        outside = {
            validate_outside_folder(proposal.path.parent, root, self.paths)
            for proposal in proposals
            if proposal.selected and proposal.target and not is_within(proposal.path, root)
        }
        with self._connect() as conn:
            return organize_features.apply(
                conn, root, proposals, folders=sorted(outside), before_rename=self._before_rename()
            )

    def _sortable(self, path: Path, root: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_file():
            raise ConfigError(f"{path.name} is not a file AutoShelf can sort")
        if is_within(resolved, root):
            if zone_of(resolved, root) is Zone.RESERVED:
                raise ConfigError(f"{resolved.name} is in one of AutoShelf's own folders")
            return resolved
        try:
            validate_outside_folder(resolved.parent, root, self.paths)
        except ConfigError as exc:
            raise ConfigError(f"{resolved.name} can't be sorted: {exc}") from None
        return resolved

    def why_here(self, path: Path) -> WhyHere:
        """AutoShelf's last move of a file in the library, if it made one."""
        root = self._require_root()
        resolved = Path(path).resolve()
        if not is_within(resolved, root):
            raise ConfigError(
                f"{resolved.name} is outside the folder AutoShelf sorts ({root}), so AutoShelf "
                "didn't put it there."
            )
        with self._connect() as conn:
            record = repository.get_file_by_path(conn, resolved)
            entry = repository.latest_history_of(conn, record.id) if record else None
        here = entry is not None and entry.dst is not None and _same_path(entry.dst, resolved)
        return WhyHere(resolved, entry, here)

    # --- compress and convert (always new files next to the originals) ------------------------

    def conversions(self, path: Path) -> list[str]:
        """The formats ``path`` can be converted to here (e.g. ["docx"] for a PDF)."""
        return convert_features.targets(path)

    def compress(self, paths: list[Path], *, zip_always: bool = False) -> Created:
        """Make a smaller copy (one PDF or image) or a ZIP (otherwise) next to the first
        file. The originals stay as they are; undo sets the copy aside in _Trash."""
        job = convert_features.plan_compress(self._library_files(paths), zip_always=zip_always)
        (created,) = self._make([job])
        return created

    def convert(self, paths: list[Path], to: str) -> list[Created]:
        """Make a copy of each file in format ``to`` next to it (several images to PDF make
        one PDF). Several new files form one batch in History."""
        return self._make(convert_features.plan_convert(self._library_files(paths), to))

    def _library_files(self, paths: list[Path]) -> list[Path]:
        root = self._require_root()
        files = []
        for path in paths:
            resolved = Path(path).resolve()
            if not resolved.is_file():
                raise convert_features.ConvertError(f"{Path(path).name} is no longer there")
            if not is_within(resolved, root):
                raise convert_features.ConvertError(
                    f"{resolved.name} is outside your library ({root}); AutoShelf only "
                    "compresses and converts files inside it"
                )
            files.append(resolved)
        return files

    def _make(self, jobs: list[convert_features.Job]) -> list[Created]:
        """Make each job's file in AutoShelf's working folder, then put it in place."""
        root = self._require_root()
        settings = self.settings()
        batch = uuid.uuid4().hex if len(jobs) > 1 else None
        work = self.paths.cache_dir / "work"
        work.mkdir(parents=True, exist_ok=True)
        made = []
        with self._connect() as conn:
            for job in jobs:
                with tempfile.TemporaryDirectory(dir=work) as folder:
                    output = convert_features.run(job, Path(folder))
                    made.append(
                        place_new_file(
                            conn,
                            output,
                            job.sources[0].parent,
                            job.name,
                            root=root,
                            operation=job.operation,
                            sources=list(job.sources),
                            extra={"to": job.to} if job.to else None,
                            batch_id=batch,
                            sort=settings.sort_created_files,
                            before_create=self._before_create(settings.sort_created_files),
                        )
                    )
        return made

    def _before_create(self, sort: bool):
        """Unless new files are to be sorted, the watcher ignores the ones AutoShelf makes."""
        with self._service_lock:
            service = self._service
        if service is None or sort:
            return None
        return service.self_moves.expect

    # --- how well would AutoShelf sort this library? ----------------------------------------

    def library_file_count(self) -> int:
        """How many files your folders hold (reads no file)."""
        root = self._require_root()
        return len(scan_library(root, self.settings().ignore_patterns).files)

    def calibrate(self, progress: calibration.Progress | None = None) -> calibration.Calibration:
        """Sort every file in your folders as if it had just arrived, at each confidence
        level, and compare with where it is (loads the model; moves nothing)."""
        root = self._require_root()
        with self._connect() as conn:
            library = self.library(conn, root)
            return calibration.calibrate(library, progress=progress)

    def set_level(self, index: int) -> None:
        """Sort at one of the confidence presets. Call ``restart`` afterwards."""
        level = CONFIDENCE_LEVELS[index]
        self.save_settings(
            replace(self.settings(), auto_threshold=level.threshold, auto_margin=level.margin)
        )

    def stats(self) -> LibraryStats:
        """The dashboard's numbers."""
        root = self._require_root()
        with self._connect() as conn:
            return library_stats(conn, root, self.settings().ignore_patterns)

    def digest(self, now: datetime | None = None) -> digest_features.Digest:
        """What AutoShelf did in the last seven days."""
        root = self._require_root()
        with self._connect() as conn:
            return digest_features.weekly_digest(
                conn, root, self.settings().ignore_patterns, now=now
            )

    def digest_due(self, now: datetime | None = None) -> digest_features.Digest | None:
        """The weekly digest, if it is on and seven days have passed since it was last
        shown (it then counts as shown). The first time, only the date is remembered, and
        a week in which nothing happened is skipped."""
        now = now or datetime.now(UTC)
        settings = self.settings()
        if not settings.weekly_digest or settings.root is None:
            return None
        if settings.digest_shown and not digest_features.due(settings.digest_shown, now):
            return None
        first = not settings.digest_shown
        self.save_settings(replace(settings, digest_shown=now.isoformat(timespec="seconds")))
        if first:
            return None
        week = self.digest(now)
        return None if week.empty else week

    # --- watched folders ------------------------------------------------------------------

    def add_watched(self, folder: Path) -> Path:
        """Opt a folder in: new matching files there are moved in and sorted. Restart after."""
        root = self._require_root()
        resolved = validate_outside_folder(folder, root, self.paths)
        settings = self.settings()
        known = {Path(item) for item in settings.watched_folders}
        if resolved not in known:
            settings.watched_folders = [*settings.watched_folders, str(resolved)]
            self.save_settings(settings)
        return resolved

    def remove_watched(self, folder: Path) -> None:
        """Stop watching a folder. Restart after."""
        settings = self.settings()
        settings.watched_folders = [
            item for item in settings.watched_folders if Path(item) != Path(folder)
        ]
        self.save_settings(settings)

    # --- start with Windows -------------------------------------------------------------

    def starts_with_windows(self) -> bool:
        """True if AutoShelf starts (in the tray) when the user signs in."""
        from autoshelf import startup

        return startup.is_enabled()

    def set_start_with_windows(self, enabled: bool) -> None:
        """Add or remove AutoShelf's entry in the user's startup programs."""
        from autoshelf import startup

        startup.set_enabled(enabled)

    # --- Explorer's right-click menu --------------------------------------------------------

    def explorer_menu(self) -> bool:
        """True if AutoShelf is in Explorer's right-click menu."""
        from autoshelf import explorer

        return explorer.is_added()

    def set_explorer_menu(self, enabled: bool) -> None:
        """Add AutoShelf to Explorer's right-click menu, or take it out."""
        from autoshelf import explorer

        if enabled:
            explorer.add()
        else:
            explorer.remove()

    def notification_buttons(self) -> bool:
        """True if notifications can have buttons (``autoshelf:`` links reach AutoShelf)."""
        from autoshelf import explorer

        return explorer.link_ready()

    def set_notification_buttons(self, enabled: bool) -> None:
        """Register the ``autoshelf:`` link for this copy of AutoShelf (the installer does this
        too), or remove it."""
        from autoshelf import explorer

        if enabled:
            explorer.add_link()
        else:
            explorer.remove_link()

    def repair_integration(self) -> bool:
        """Point the menu and the ``autoshelf:`` link at this copy of AutoShelf if the program
        they start is gone (the app was moved). Returns True if anything changed."""
        from autoshelf import explorer

        changed = False
        if explorer.is_broken():
            explorer.add()
            changed = True
        program = explorer.link_program()
        if program is not None and not program.exists():
            explorer.add_link()
            changed = True
        return changed

    # --- notification buttons -----------------------------------------------------------

    def offer(self, path: Path, folders: list[str]) -> str:
        """Remember what a notification about a waiting file offers; returns the token its
        links carry."""
        with self._connect() as conn:
            return links.make_offer(conn, path, folders)

    def offered_file(self, token: str) -> Path | None:
        """The file a notification was about, if the notification is still current."""
        with self._connect() as conn:
            try:
                return links.get_offer(conn, token).path
            except links.LinkError:
                return None

    def answer_offer(self, token: str, choice: int) -> tuple[MoveResult, str]:
        """Put the file a notification was about into the folder of the button clicked
        (AutoShelf learns from it, as from the Inbox). Each notification can be answered
        once: raises LinkError for an answered or old one, and AssignError if the file no
        longer waits in the Inbox."""
        with self._connect() as conn:
            offer = links.get_offer(conn, token)
            if choice >= len(offer.folders):
                raise links.LinkError("That button offered no folder.")
            links.use_offer(conn, token)
        folder = offer.folders[choice]
        return self.assign(offer.path, folder), folder

    # --- optional AI (off by default) ---------------------------------------------------

    def provider(self, *, bulk: bool = False) -> llm.Provider | None:
        """The configured LLM provider, or None while it is off. Raises LLMError.

        ``bulk``: for summaries, names and flashcards, which use their own model (by
        default the cheapest Claude model).
        """
        settings = self.settings()
        model = settings.llm_model
        if bulk:
            model = llm.bulk_model(settings.llm_provider, model, settings.llm_bulk_model)
        return llm.make_provider(settings.llm_provider, model, ollama_url=settings.ollama_url)

    def _require_provider(self, *, bulk: bool = False) -> llm.Provider:
        provider = self.provider(bulk=bulk)
        if provider is None:
            raise llm.LLMError("Turn on an AI provider in Settings first")
        return provider

    @property
    def ai_enabled(self) -> bool:
        """True if an LLM provider is chosen (it may still lack a key)."""
        return self.settings().llm_provider != llm.OFF

    def save_api_key(self, key: str) -> None:
        """Keep the Claude API key in the Windows Credential Manager."""
        from autoshelf.llm import secrets

        secrets.set_api_key(key)

    def forget_api_key(self) -> None:
        """Remove the saved Claude API key."""
        from autoshelf.llm import secrets

        secrets.delete_api_key()

    def api_key_status(self) -> str:
        """ "saved (...abcd)" or "not saved"."""
        from autoshelf.llm import secrets

        return secrets.masked(secrets.get_api_key())

    def test_ai(self) -> str:
        """Ask for a one-word reply, to check the AI works (with each model in use)."""
        main = self._require_provider()
        bulk = self._require_provider(bulk=True)
        answers = []
        for provider in (main,) if bulk.model == main.model else (main, bulk):
            reply = provider.complete(
                "Reply with the single word OK.",
                [llm.Message("user", "Are you there?")],
                max_tokens=16,
            )
            answers.append(f"{provider.model} answered: {reply[:30]}")
        return f"{main.name}: " + "; ".join(answers)

    def summarize(self, path: Path, *, refresh: bool = False) -> str:
        """A 2-3 sentence summary of a file (cached; made by the AI when missing)."""
        provider = self._require_provider(bulk=True)
        with self._connect() as conn:
            return summary_features.file_summary(conn, provider, path, refresh=refresh)

    def rename_suggestions(self) -> list[rename_features.RenameProposal]:
        """Files with junk names and a better name for each (no AI needed)."""
        from autoshelf.actions.inbox import waiting_files
        from autoshelf.extract import extract

        root = self._require_root()
        with self._connect() as conn:
            proposals = rename_features.rename_suggestions(conn, root)
        known = {proposal.path for proposal in proposals}
        for path in waiting_files(root, self.settings().ignore_patterns):
            if path in known or not rename_features.is_junk_name(path.name):
                continue
            extraction = extract(path)  # waiting files AutoShelf hasn't read yet
            proposal = rename_features.propose_name(path, extraction.title, extraction.text)
            if proposal is not None:
                proposals.append(proposal)
        return proposals

    def propose_name(
        self, path: Path, *, use_ai: bool = False
    ) -> rename_features.RenameProposal | None:
        """A better name for one file, from its title or heading, or from the AI."""
        from autoshelf.extract import extract

        extraction = extract(path)
        provider = self._require_provider(bulk=True) if use_ai else None
        if use_ai:
            stem = rename_features.clean_stem(
                rename_features.ask_for_name(provider, path.name, extraction.title, extraction.text)
            )
            return rename_features.RenameProposal(path, stem + path.suffix, "AI") if stem else None
        return rename_features.propose_name(path, extraction.title, extraction.text)

    def rename(self, path: Path, new_name: str) -> MoveResult:
        """Rename a file the user approved a new name for (undoable)."""
        root = self._require_root()
        with self._connect() as conn:
            return rename_features.rename(
                conn, path, new_name, root=root, before_rename=self._before_rename()
            )

    def suggest_folders(self) -> list[folder_features.FolderSuggestion]:
        """Groups of waiting files that no folder covers yet, with a proposed name."""
        root = self._require_root()
        settings = self.settings()
        try:
            provider = self.provider(bulk=True)  # names the folders
        except llm.LLMError:
            provider = None
        with self._connect() as conn:
            library = self.library(conn, root)
            return folder_features.suggest_folders(
                conn,
                root,
                library,
                provider=provider,
                threshold=settings.auto_threshold,
                ignore_patterns=settings.ignore_patterns,
            )

    def create_folder(self, name: str, files: list[Path] = ()) -> tuple[str, list[MoveResult]]:
        """Create the folder the user asked for and move ``files`` into it (each undoable)."""
        root = self._require_root()
        key = folder_features.create_folder(root, name)
        log.info("Created the folder %s as you asked", key)
        moved = [self.assign(path, key) for path in files if path.is_file()]
        return key, moved

    def ask(
        self, question: str, history: list[ask_features.Answer] | None = None
    ) -> ask_features.Answer:
        """Answer a question from your files, with citations (uses the AI)."""
        root = self._require_root()
        provider = self._require_provider()
        embedder = self._embedder()
        with self._connect() as conn:
            return ask_features.ask(
                conn, root, provider, embedder, self._search_index, question, history
            )

    def _folder_files(self, folder: str) -> list[tuple[Path, str | None, str | None]]:
        root = self._require_root()
        key = resolve_folder(root, folder, self.settings().ignore_patterns)
        scan = scan_library(root, self.settings().ignore_patterns)
        files = [
            item.path
            for item in scan.files
            if item.folder == key or item.folder.startswith(key + "/")
        ]
        with self._connect() as conn:
            found = []
            for path in files:
                record = repository.get_file_by_path(conn, path)
                title, excerpt = (
                    repository.get_file_content(conn, record.id) if record else (None, None)
                )
                found.append((path, title, excerpt))
        return found

    def reading_list(self, folder: str) -> list[study_features.ReadingItem]:
        """The files of a folder from introductory to advanced (uses the AI)."""
        provider = self._require_provider()
        return study_features.reading_list(provider, self._folder_files(folder))

    def make_flashcards(
        self, *, path: Path | None = None, folder: str | None = None, count: int = 10
    ) -> list[repository.Flashcard]:
        """Make cards from one file, or from a whole folder (uses the AI) and keep them."""
        provider = self._require_provider(bulk=True)
        root = self._require_root()
        if path is not None:
            key = folder_label(path, root) or None
            with self._connect() as conn:
                return study_features.cards_for_file(conn, provider, path, key, count)
        if folder is None:
            raise ValueError("make_flashcards needs a file or a folder")
        key = resolve_folder(root, folder, self.settings().ignore_patterns)
        texts = [
            "\n".join(part for part in (f"## {path.stem}", title, excerpt) if part)
            for path, title, excerpt in self._folder_files(key)
        ]
        cards = study_features.make_cards(provider, key, "\n\n".join(texts), count)
        with self._connect() as conn:
            ids = repository.add_flashcards(
                conn, [(card.question, card.answer) for card in cards], file_id=None, folder=key
            )
            stored = {card.id: card for card in repository.list_flashcards(conn)}
        return [stored[card_id] for card_id in ids]

    def flashcards(self, folder: str | None = None) -> list[repository.Flashcard]:
        """Saved cards of a folder (and its subfolders), or all of them."""
        with self._connect() as conn:
            return repository.list_flashcards(conn, folder)

    def delete_flashcards(self, card_ids: list[int]) -> None:
        """Remove cards."""
        with self._connect() as conn:
            repository.delete_flashcards(conn, card_ids)

    def export_flashcards(self, cards: list[repository.Flashcard], target: Path) -> int:
        """Write cards to a new CSV file for Anki (never overwrites a file)."""
        return study_features.export_csv(cards, target)

    # --- rules --------------------------------------------------------------------

    def rules(self) -> list[repository.RuleRecord]:
        """The rules in the order they are checked."""
        with self._connect() as conn:
            return repository.list_rules(conn)

    def _checked_rule(
        self,
        name: str,
        conditions: list[dict],
        target: str,
        tags: list[str] | tuple[str, ...],
        rename_template: str,
    ) -> tuple[list[dict], str, list[str]]:
        """Validate a rule: (its conditions, its existing folder's key or "", its tags).

        Raises RuleError, AssignError (no such folder) or TagError.
        """
        cleaned = rule_engine.check_rule(
            name, conditions, target=target, tags=tags, rename_template=rename_template
        )
        key = ""
        if target.strip():
            key = resolve_folder(self._require_root(), target, self.settings().ignore_patterns)
        names = list(dict.fromkeys(tag_features.clean_tag(tag) for tag in tags if tag.strip()))
        return cleaned, key, names

    def add_rule(
        self,
        name: str,
        kind: str = "",
        pattern: str = "",
        target: str = "",
        *,
        conditions: list[dict] | None = None,
        match_any: bool = False,
        tags: list[str] | tuple[str, ...] = (),
        rename_template: str = "",
        enabled: bool = True,
    ) -> repository.RuleRecord:
        """Add a rule after the existing ones: ``conditions`` (or, for one condition,
        ``kind`` and ``pattern``) and what to do: move to ``target``, add ``tags``, rename
        from ``rename_template``. Raises RuleError, AssignError or TagError if invalid."""
        if conditions is None:
            conditions = [{"kind": kind, "pattern": pattern}]
        cleaned, key, names = self._checked_rule(name, conditions, target, tags, rename_template)
        with self._connect() as conn:
            rule_id = repository.insert_rule(
                conn,
                name=name.strip(),
                conditions=cleaned,
                target_folder=key,
                match_any=match_any,
                tags=names,
                rename_template=rename_template.strip(),
                enabled=enabled,
            )
            return repository.get_rule(conn, rule_id)

    def update_rule(self, rule: repository.RuleRecord) -> repository.RuleRecord:
        """Save changes to a rule (validated like a new one)."""
        cleaned, key, names = self._checked_rule(
            rule.name, list(rule.conditions), rule.target_folder, rule.tags, rule.rename_template
        )
        updated = replace(
            rule,
            name=rule.name.strip(),
            conditions=tuple(cleaned),
            target_folder=key,
            tags=tuple(names),
            rename_template=rule.rename_template.strip(),
        )
        with self._connect() as conn:
            repository.update_rule(conn, updated)
        return updated

    def rule_suggestions(self) -> list[RuleSuggestion]:
        """Rules your choices point to (e.g. files named "invoice" always go to Finance)."""
        root = self._require_root()
        settings = self.settings()
        with self._connect() as conn:
            return suggest_rules(
                conn, root, settings.ignore_patterns, settings.dismissed_rule_suggestions
            )

    def rule_suggestion_for(self, folder: str) -> RuleSuggestion | None:
        """A suggested rule that sends files to ``folder``, if any (after an Inbox answer)."""
        wanted = folder.casefold()
        return next(
            (item for item in self.rule_suggestions() if item.target.casefold() == wanted), None
        )

    def add_suggested_rule(self, suggestion: RuleSuggestion) -> repository.RuleRecord:
        """Turn a suggestion into a rule (checked after the existing ones)."""
        return self.add_rule(
            suggestion.name, conditions=[suggestion.condition], target=suggestion.target
        )

    def dismiss_rule_suggestion(self, key: str) -> None:
        """Don't suggest this rule again."""
        settings = self.settings()
        if key not in settings.dismissed_rule_suggestions:
            settings.dismissed_rule_suggestions = [*settings.dismissed_rule_suggestions, key]
            self.save_settings(settings)

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
        if match is None:
            return RuleTest(path, None, None)
        target = match.rule.target_folder or None
        new_name = rule_engine.new_name(match.rule, path, extraction, target)
        return RuleTest(path, match.rule, target, match.matched, new_name)


def _is_waiting(conn: sqlite3.Connection, path: Path) -> bool:
    record = repository.get_file_by_path(conn, path)
    return record is not None and record.status in ("inbox", "ignored")
