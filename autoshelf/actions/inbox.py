"""The Inbox: files waiting for the user's decision, and recording the answer (SPEC 4.3)."""

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.actions import learning
from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.extract.filename import filename_tokens
from autoshelf.layout import PROCESSED_ZONES, inbox_dir, zone_of
from autoshelf.ml.classifier import Classification
from autoshelf.ml.library import TEXT_EXCERPT_CHARS, LibraryIndex, scan_library
from autoshelf.watcher.ignore import ignore_reason

log = logging.getLogger(__name__)

REASON_USER = "user"  # the user chose the folder


class AssignError(Exception):
    """A file couldn't be put into the chosen folder. Nothing was moved."""


@dataclass(frozen=True)
class InboxItem:
    """A file waiting in _Inbox, with what is needed to decide where it goes."""

    path: Path
    title: str | None
    excerpt: str | None
    classification: Classification | None  # None without a classifier
    details: dict | None = None  # why: scores, similar files, shared words (ml.explain)

    @property
    def explanation(self) -> str:
        """Why it is waiting."""
        if self.classification is None:
            return "it can't be sorted by content right now"
        return self.classification.explanation


def waiting_files(
    root: Path, ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS
) -> list[Path]:
    """Files in ``_Inbox`` waiting for a decision, sorted by name."""
    inbox = inbox_dir(root)
    if not inbox.is_dir():
        return []
    patterns = tuple(ignore_patterns)
    files = []
    for path in sorted(inbox.iterdir(), key=lambda item: item.name.casefold()):
        try:
            if path.is_file() and not path.is_symlink() and ignore_reason(path, patterns) is None:
                files.append(path)
        except OSError:
            continue  # vanished meanwhile
    return files


def resolve_folder(
    root: Path, folder: str, ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS
) -> str:
    """Return the key of an existing category folder named ``folder`` (any case, / or \\).

    Raises AssignError if there is no such folder: AutoShelf never creates folders.
    """
    wanted = folder.replace("\\", "/").strip("/").casefold()
    for key in scan_library(root, ignore_patterns).folders:
        if key.casefold() == wanted:
            return key
    raise AssignError(
        f"There is no folder {folder!r} in {root}. AutoShelf never creates folders, "
        "so create it first (in Explorer or on your iPhone)."
    )


def assign(
    conn: sqlite3.Connection,
    path: Path,
    folder: str,
    *,
    root: Path,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    before_rename: BeforeRename | None = None,
) -> MoveResult:
    """Move a waiting file into the folder the user chose, and learn from it.

    The move is recorded in history (reason "user") and undoable. A correction row
    notes the classifier's earlier suggestion and the user's choice, with the file's
    vector. The vector also moves with the file, so it immediately becomes an example
    for its new folder.
    """
    if zone_of(path, root) not in PROCESSED_ZONES:
        raise AssignError(
            f"{path.name} isn't waiting for a decision; only files in the root or "
            "_Inbox can be assigned"
        )
    key = resolve_folder(root, folder, ignore_patterns)
    known = repository.get_file_by_path(conn, path)
    suggested = known.suggestion if known else None
    try:
        result = move_file(
            conn,
            path,
            root.joinpath(*key.split("/")),
            root=root,
            reason=REASON_USER,
            before_rename=before_rename,
        )
    except MoveError as exc:
        raise AssignError(str(exc)) from exc
    source, decision_id = learning.inbox_source(conn, result.file_id)
    learning.record_choice(
        conn,
        file_id=result.file_id,
        from_folder=suggested,
        to_folder=key,
        source=source,
        history_id=decision_id,
    )
    repository.set_suggestion(conn, result.file_id, key)
    return result


def inbox_items(
    conn: sqlite3.Connection,
    root: Path,
    library: LibraryIndex | None,
    *,
    threshold: float = DEFAULT_AUTO_THRESHOLD,
    margin: float = DEFAULT_AUTO_MARGIN,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    extractor: Callable[[Path], Extraction] = extract,
) -> list[InboxItem]:
    """Every waiting file with a text preview and fresh folder suggestions.

    Suggestions are recomputed, so they reflect folders, descriptions and examples added
    since the file arrived. Stored vectors are reused; other files are read. Call
    ``library.refresh()`` first. Slow (it may read files): keep it off the UI thread.
    """
    items = []
    for path in waiting_files(root, ignore_patterns):
        record = repository.get_file_by_path(conn, path)
        title, excerpt = repository.get_file_content(conn, record.id) if record else (None, None)
        vector = library.stored_vector(path) if library is not None else None
        extraction = None
        if vector is None and (library is not None or not (title or excerpt)):
            try:
                extraction = extractor(path)
            except OSError as exc:
                log.debug("Couldn't read %s: %s", path, exc)
                continue  # gone or locked; it will show up again on the next refresh
            title = title or extraction.title
            excerpt = excerpt or extraction.text[:TEXT_EXCERPT_CHARS] or None
            if library is not None:
                vector = library.vectorize(extraction, path.name)
        classification = details = None
        if library is not None and vector is not None:
            classification = library.classify(vector, threshold=threshold, margin=margin)
            extraction = extraction or Extraction(
                "stored", tuple(filename_tokens(path.name)), text=excerpt or "", title=title
            )
            details = library.explain(classification, extraction, path.name)
        items.append(InboxItem(path, title, excerpt, classification, details))
    return items
