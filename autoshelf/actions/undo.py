"""Undo moves and renames recorded in history."""

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.db import repository
from autoshelf.fsutil import sha256_file


class UndoError(Exception):
    """An undo was refused or failed. ``completed`` lists undos done before the failure."""

    def __init__(self, message: str, completed: Iterable[MoveResult] = ()) -> None:
        super().__init__(message)
        self.completed = list(completed)


def undo(
    conn: sqlite3.Connection,
    history_id: int,
    *,
    force: bool = False,
    before_rename: BeforeRename | None = None,
) -> MoveResult:
    """Move a file back to where history entry ``history_id`` found it.

    Refuses if the file is gone, if its content changed (unless ``force``), or if its
    original folder no longer exists (folders are never recreated). The undo is itself
    a history row; a name now taken at the original location gets a `` (2)`` suffix.
    """
    entry = repository.get_history(conn, history_id)
    if entry is None:
        raise UndoError(f"There is no history entry #{history_id}")
    if entry.action not in repository.UNDOABLE_ACTIONS:
        raise UndoError(
            f"#{history_id} is a {entry.action!r}; only moves and renames can be undone"
        )
    if entry.state != "done":
        raise UndoError(f"#{history_id} is {entry.state}, so there is nothing to undo")

    current = Path(entry.dst)
    original = Path(entry.src)
    if not current.is_file():
        raise UndoError(
            f"{current.name} is no longer in {current.parent}; "
            "it was moved or deleted outside AutoShelf"
        )
    if not force and entry.sha256 and sha256_file(current) != entry.sha256:
        raise UndoError(
            f"{current.name} has changed since it was moved; use --force to undo anyway"
        )
    if not original.parent.is_dir():
        raise UndoError(
            f"The original folder {original.parent} no longer exists, "
            "and AutoShelf won't recreate it"
        )

    try:
        result = move_file(
            conn,
            current,
            original.parent,
            root=Path(entry.root),
            reason=f"undo #{history_id}",
            action="undo",
            new_name=original.name,
            undo_of=history_id,
            before_rename=before_rename,
        )
    except MoveError as exc:
        raise UndoError(str(exc)) from exc
    repository.set_history_state(conn, history_id, "undone")
    return result


def undo_last(
    conn: sqlite3.Connection,
    count: int,
    *,
    force: bool = False,
    before_rename: BeforeRename | None = None,
) -> list[MoveResult]:
    """Undo the ``count`` most recent moves, newest first, stopping at the first failure."""
    completed: list[MoveResult] = []
    for entry in repository.last_undoable(conn, count):
        try:
            completed.append(undo(conn, entry.id, force=force, before_rename=before_rename))
        except UndoError as exc:
            raise UndoError(f"Stopped at #{entry.id}: {exc}", completed) from exc
    return completed
