"""Learning examples from the user's choices and corrections (SPEC section 4.6).

A correction (file -> folder) is recorded when the user:

- picks a folder for a waiting file (``actions.inbox.assign``),
- moves a file AutoShelf sorted into another folder (``correct``, from History),
- moves a file between folders, or out of ``_Inbox``, in Explorer or on the iPhone.

The last case is noticed when the library rescans its folders (``adopt_moved_file``): a
file new to AutoShelf whose content matches a known file that vanished from its old
place (for an iCloud online-only file, whose name and size match, since reading it would
download it). iCloud may report such a move as a delete plus a create, and the app may
have been closed at the time; matching by content handles both. AutoShelf's own moves
never count: the mover updates the file's record before it finishes, and a move still
in progress has a pending history row.
"""

import logging
import os
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.db import repository
from autoshelf.db.connection import transaction
from autoshelf.layout import Zone, zone_of

log = logging.getLogger(__name__)

SOURCE_INBOX = "inbox"  # chosen in the Inbox
SOURCE_UNDO = "undo"  # an AutoShelf decision the user reversed
SOURCE_EXTERNAL = "external"  # moved in Explorer or on the iPhone


class CorrectionError(Exception):
    """A file can't be moved to another folder as asked. Nothing was moved."""


@dataclass(frozen=True)
class Relocation:
    """A file the user moved outside AutoShelf, and what was learned from it."""

    file_id: int
    old_path: Path
    new_path: Path
    from_folder: str | None
    to_folder: str
    correction_id: int | None  # None if nothing was learned (e.g. a rename in place)


def folder_key_of(path: Path, root: Path) -> str | None:
    """Key ("A/B") of the category folder holding ``path``, or None outside categories."""
    if zone_of(path, root) is not Zone.CATEGORY:
        return None
    return "/".join(path.relative_to(root).parts[:-1])


def record_choice(
    conn: sqlite3.Connection,
    *,
    file_id: int,
    from_folder: str | None,
    to_folder: str,
    source: str,
    history_id: int | None = None,
) -> int:
    """Record a learning example, keeping the file's vector so it outlives the file."""
    stored = repository.get_vector_blob(conn, file_id)
    model, vector = stored if stored else (None, None)
    return repository.record_correction(
        conn,
        file_id=file_id,
        from_folder=from_folder,
        to_folder=to_folder,
        source=source,
        vector=vector,
        model=model,
        history_id=history_id,
    )


def inbox_source(conn: sqlite3.Connection, file_id: int) -> tuple[str, int | None]:
    """How to label the user's choice for a waiting file: (source, decision it corrects).

    A file whose automatic move the user undid is an "undo" correction; otherwise it
    answers the Inbox's question.
    """
    decision = repository.last_decision(conn, file_id)
    if decision is None:
        return SOURCE_INBOX, None
    if decision.state == "undone" and decision.reason != "ask":
        return SOURCE_UNDO, decision.id
    return SOURCE_INBOX, decision.id


def correct(
    conn: sqlite3.Connection,
    history_id: int,
    folder_key: str,
    *,
    root: Path,
    before_rename: BeforeRename | None = None,
) -> MoveResult:
    """Move a file that a history entry put in a folder into another (existing) folder.

    This is "undo and choose another folder" in one step: the move is recorded (and
    can be undone), and the correction teaches AutoShelf. ``folder_key`` must be the key
    of an existing category folder (see ``actions.inbox.resolve_folder``).
    """
    entry = repository.get_history(conn, history_id)
    if entry is None:
        raise CorrectionError(f"There is no history entry #{history_id}")
    if entry.action != "move" or entry.state != "done":
        raise CorrectionError(f"#{history_id} isn't a completed move")
    current = Path(entry.dst)
    from_folder = folder_key_of(current, root)
    if from_folder is None:
        raise CorrectionError(
            f"{current.name} wasn't put in one of your folders by #{history_id}; "
            "answer files waiting in _Inbox from the Inbox instead"
        )
    if not current.is_file():
        raise CorrectionError(f"{current.name} is no longer in {from_folder}")
    if from_folder.casefold() == folder_key.casefold():
        raise CorrectionError(f"{current.name} is already in {folder_key}")
    try:
        result = move_file(
            conn,
            current,
            root.joinpath(*folder_key.split("/")),
            root=root,
            reason="user",
            details={
                "corrects": history_id,
                "explanation": f"you moved it from {from_folder!r} to {folder_key!r}",
            },
            before_rename=before_rename,
        )
    except MoveError as exc:
        raise CorrectionError(str(exc)) from exc
    record_choice(
        conn,
        file_id=result.file_id,
        from_folder=from_folder,
        to_folder=folder_key,
        source=SOURCE_UNDO,
        history_id=history_id,
    )
    repository.set_suggestion(conn, result.file_id, folder_key)
    return result


def adopt_moved_file(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    *,
    size: int,
    online_only: bool,
    digest: str | None,
) -> Relocation | None:
    """Recognise a new file in a category folder as a known file the user moved there.

    ``digest`` is the file's SHA-256 (None for an online-only file). If exactly one
    known file has vanished and matches, its record (vector, tags, history) moves to
    ``path`` and, if the folder changed, a correction is recorded. Safe to call from
    several processes: a move is only ever adopted once.
    """
    to_folder = folder_key_of(path, root)
    if to_folder is None:
        return None
    matches = _vanished_matches(conn, root, path, size, online_only, digest)
    if len(matches) != 1:
        if len(matches) > 1:
            log.debug("%s matches %d vanished files; not guessing", path, len(matches))
        return None
    record = matches[0]
    old_path = Path(record.path)
    old_zone = zone_of(old_path, root)
    old_folder = folder_key_of(old_path, root)
    from_folder = old_folder if old_zone is Zone.CATEGORY else record.suggestion
    learn = (old_folder or "").casefold() != to_folder.casefold() and not (
        old_zone is Zone.DROP and from_folder is None
    )
    with transaction(conn):
        if repository.has_pending_history(conn, record.id):
            return None  # AutoShelf itself is moving it right now
        if not repository.relocate_file(conn, record.id, old_path, path, "sorted"):
            return None  # adopted meanwhile by another process
        correction_id = None
        if learn:
            decision = repository.last_decision(conn, record.id)
            correction_id = record_choice(
                conn,
                file_id=record.id,
                from_folder=from_folder,
                to_folder=to_folder,
                source=SOURCE_EXTERNAL,
                history_id=decision.id if decision else None,
            )
    if learn:
        log.info(
            "Learned: you moved %s from %s to %s",
            path.name,
            from_folder or old_zone.value,
            to_folder,
        )
    return Relocation(record.id, old_path, path, from_folder, to_folder, correction_id)


def _vanished_matches(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    size: int,
    online_only: bool,
    digest: str | None,
) -> list[repository.FileRecord]:
    """Known files that are gone from their recorded place and look like ``path``."""
    by_content = repository.find_files_by_sha256(conn, digest) if digest else []
    candidates = _vanished(by_content, root, path)
    if candidates:
        return candidates
    # Online-only files can't be hashed without downloading them, and files indexed while
    # online-only have no hash: compare names and sizes instead.
    same_name = [
        record
        for record in repository.find_files_by_size(conn, size)
        if Path(record.path).name.casefold() == path.name.casefold()
        and (online_only or record.sha256 is None)
    ]
    return _vanished(same_name, root, path)


def _vanished(
    records: Iterable[repository.FileRecord], root: Path, path: Path
) -> list[repository.FileRecord]:
    found = []
    for record in records:
        old = Path(record.path)
        if old == path or os.path.lexists(old):
            continue
        if zone_of(old, root) in (Zone.CATEGORY, Zone.INBOX, Zone.DROP):
            found.append(record)
    return found
