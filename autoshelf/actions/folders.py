"""Changing folders from the Library: make one, move files, rename or move a folder, and
move files or folders to ``_Trash`` (the Library's Delete). Nothing is ever deleted.

Every change is recorded in history before it happens and can be undone. A folder change
is one row with the action ``folder`` (``src`` the folder's old path, ``dst`` its new one,
``details.kind`` rename, move or trash). The records follow the folder at once
(``repository.follow_folder``): the paths of the files in it, and, for a rename or move,
everything that names it (description, corrections, rules, flashcards). Otherwise the next
rescan would take every file in it for one the user moved, and learn from each.

Files moved by hand between folders teach AutoShelf, like moves made in Explorer.
"""

import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from autoshelf.actions import learning
from autoshelf.actions.create import trash_dir
from autoshelf.actions.mover import BeforeRename, MoveError, move_file
from autoshelf.db import repository
from autoshelf.features.new_folders import FolderNameError, create_folder, valid_folder_name
from autoshelf.fsutil import is_within
from autoshelf.layout import Zone, zone_of

log = logging.getLogger(__name__)

ACTION_FOLDER = "folder"
REASON_USER = "user"
REASON_TRASH = "trash"
KIND_RENAME = "rename"
KIND_MOVE = "move"
KIND_TRASH = "trash"
MAX_SUFFIX = 9999


class FolderError(Exception):
    """A folder change was refused or failed. Nothing was changed."""


@dataclass(frozen=True)
class Change:
    """One file or folder that was moved, renamed or moved to _Trash."""

    history_id: int
    src: Path
    dst: Path
    kind: str  # "file" or "folder"


@dataclass(frozen=True)
class Changes:
    """What one action did, as one batch in History when it changed several things."""

    batch_id: str | None
    done: list[Change] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def history_ids(self) -> list[int]:
        """The history rows of the changes, in the order they were made."""
        return [change.history_id for change in self.done]


def folder_path(root: Path, key: str) -> Path:
    """The path of a folder key ("" is the root)."""
    return root.joinpath(*key.split("/")) if key else root


def folder_key(root: Path, path: Path) -> str:
    """The key of a folder inside the root ("" for the root)."""
    return "/".join(path.relative_to(root).parts)


def make_folder(root: Path, parent: str, name: str) -> str:
    """Make a folder the user named inside ``parent`` ("" for the root); returns its key.

    Not recorded in History: a folder made by mistake is moved to _Trash like any other.
    """
    if parent and not _is_category(root, folder_path(root, parent)):
        raise FolderError(f"There is no folder {parent!r}")
    try:
        return create_folder(root, name, parent)
    except (FolderNameError, OSError) as exc:
        raise FolderError(str(exc)) from exc


def move_files(
    conn: sqlite3.Connection,
    root: Path,
    paths: list[Path],
    folder: str,
    *,
    before_rename: BeforeRename | None = None,
    batch_id: str | None = None,
) -> Changes:
    """Move files into one of the user's folders (not the root itself, where files get
    sorted). Each move to another folder is learned from, like a move in Explorer."""
    if not folder:
        raise FolderError(
            "Files can't be put in your AutoShelf folder itself: AutoShelf would sort them "
            "again. Choose a folder."
        )
    target = _existing_folder(root, folder)
    batch = batch_id or (uuid.uuid4().hex if len(paths) > 1 else None)
    done, failed = [], []
    for path in paths:
        old_folder = learning.folder_key_of(path, root)
        try:
            result = move_file(
                conn,
                path,
                target,
                root=root,
                reason=REASON_USER,
                sha256=_known_hash(conn, path),
                batch_id=batch,
                before_rename=before_rename,
            )
        except MoveError as exc:
            failed.append((path, str(exc)))
            continue
        done.append(Change(result.history_id, result.src, result.dst, "file"))
        if old_folder is not None and old_folder.casefold() != folder.casefold():
            learning.record_choice(
                conn,
                file_id=result.file_id,
                from_folder=old_folder,
                to_folder=folder_key(root, target),
                source=learning.SOURCE_EXTERNAL,
                history_id=result.history_id,
            )
    if not done and failed:
        raise FolderError("; ".join(reason for _, reason in failed))
    return Changes(batch, done, failed)


def rename_folder(
    conn: sqlite3.Connection,
    root: Path,
    key: str,
    name: str,
    *,
    before_rename: BeforeRename | None = None,
) -> str:
    """Rename a folder; returns its new key. A name already taken there is refused."""
    source = _existing_folder(root, key)
    try:
        cleaned = valid_folder_name(name)
    except FolderNameError as exc:
        raise FolderError(str(exc)) from exc
    target = source.parent / cleaned
    case_only = target.name != source.name and target.name.casefold() == source.name.casefold()
    if target.name == source.name:
        return key
    if os.path.lexists(target) and not case_only:
        raise FolderError(f"There is already a {cleaned!r} in {_place(root, source.parent)}")
    folder_change(conn, root, source, target, KIND_RENAME, before_rename=before_rename)
    return folder_key(root, target)


def move_folder(
    conn: sqlite3.Connection,
    root: Path,
    key: str,
    parent: str,
    *,
    before_rename: BeforeRename | None = None,
    batch_id: str | None = None,
) -> str:
    """Move a folder into ``parent`` ("" for the top level); returns its new key. A name
    already taken there gets " (2)", " (3)"..."""
    change = move_folder_change(
        conn, root, key, parent, before_rename=before_rename, batch_id=batch_id
    )
    return folder_key(root, change.dst)


def move_folder_change(
    conn: sqlite3.Connection,
    root: Path,
    key: str,
    parent: str,
    *,
    before_rename: BeforeRename | None = None,
    batch_id: str | None = None,
) -> Change:
    """``move_folder``, returning the change (with its history row)."""
    source = _existing_folder(root, key)
    destination = folder_path(root, parent)
    if parent:
        destination = _existing_folder(root, parent)
    wanted = parent.casefold()
    if wanted == key.casefold() or wanted.startswith(key.casefold() + "/"):
        raise FolderError(f"{source.name} can't be moved into itself")
    if source.parent == destination:
        raise FolderError(f"{source.name} is already in {_place(root, destination)}")
    target = _free_folder_name(destination, source.name)
    history_id = folder_change(
        conn, root, source, target, KIND_MOVE, before_rename=before_rename, batch_id=batch_id
    )
    return Change(history_id, source, target, "folder")


def trash(
    conn: sqlite3.Connection,
    root: Path,
    files: list[Path],
    folders: list[str],
    *,
    before_rename: BeforeRename | None = None,
) -> Changes:
    """Move files and folders into ``_Trash`` (the user empties it). Never deletes."""
    sources = [(Path(path), "file") for path in files]
    sources += [(_existing_folder(root, key), "folder") for key in folders]
    # Something inside another chosen folder goes with it.
    chosen = [path for path, kind in sources if kind == "folder"]
    sources = [
        (path, kind)
        for path, kind in sources
        if not any(path != folder and is_within(path, folder) for folder in chosen)
    ]
    bin_dir = trash_dir(root)
    if not bin_dir.is_dir():
        bin_dir.mkdir()  # AutoShelf's own reserved folder, like _Inbox
    batch = uuid.uuid4().hex if len(sources) > 1 else None
    done, failed = [], []
    for path, kind in sources:
        try:
            if kind == "folder":
                target = _free_folder_name(bin_dir, path.name)
                history_id = folder_change(
                    conn,
                    root,
                    path,
                    target,
                    KIND_TRASH,
                    before_rename=before_rename,
                    batch_id=batch,
                )
                done.append(Change(history_id, path, target, kind))
            else:
                result = move_file(
                    conn,
                    path,
                    bin_dir,
                    root=root,
                    reason=REASON_TRASH,
                    sha256=_known_hash(conn, path),
                    batch_id=batch,
                    before_rename=before_rename,
                )
                done.append(Change(result.history_id, result.src, result.dst, kind))
        except (MoveError, FolderError) as exc:
            failed.append((path, str(exc)))
    if not done and failed:
        raise FolderError("; ".join(reason for _, reason in failed))
    return Changes(batch, done, failed)


def folder_change(
    conn: sqlite3.Connection,
    root: Path,
    src: Path,
    dst: Path,
    kind: str,
    *,
    undo_of: int | None = None,
    before_rename: BeforeRename | None = None,
    batch_id: str | None = None,
) -> int:
    """Rename the folder ``src`` to ``dst`` with a history row, and make the records follow.

    ``undo_of``: this puts back the folder change of that history row (recorded as an
    ``undo``). Returns the history id. Raises FolderError; then nothing was changed.
    """
    root = Path(root).resolve(strict=True)
    src, dst = Path(src), Path(dst)
    if src.is_symlink() or getattr(src, "is_junction", lambda: False)():
        raise FolderError(f"Refusing to move a link: {src}")
    if not src.is_dir():
        raise FolderError(f"{src.name} is no longer in {src.parent}")
    for end in (src, dst):
        if end == root or not is_within(end, root):
            raise FolderError(f"{end} is outside your AutoShelf folder")
    if not dst.parent.is_dir():
        raise FolderError(f"{dst.parent} no longer exists, and AutoShelf won't recreate it")
    case_only = str(src).casefold() == str(dst).casefold()
    if os.path.lexists(dst) and not case_only:
        raise FolderError(f"{dst} already exists")
    history_id = repository.insert_history(
        conn,
        action="undo" if undo_of is not None else ACTION_FOLDER,
        src=src,
        dst=dst,
        reason="undo"
        if undo_of is not None
        else (REASON_TRASH if kind == KIND_TRASH else REASON_USER),
        root=root,
        batch_id=batch_id,
        undo_of=undo_of,
        details={"kind": kind, "folder": True},
    )
    try:
        if before_rename is not None:
            before_rename(src, dst)
        os.rename(src, dst)
    except OSError as exc:
        repository.set_history_state(conn, history_id, "failed", note=str(exc))
        raise FolderError(f"Couldn't move {src.name}: {exc}") from exc
    follow(conn, root, src, dst)
    repository.set_history_state(conn, history_id, "done")
    log.debug("History #%d: folder %s %s -> %s", history_id, kind, src, dst)
    return history_id


def follow(conn: sqlite3.Connection, root: Path, src: Path, dst: Path) -> int:
    """Make the records follow a folder moved from ``src`` to ``dst`` (both in the root).

    Between two of the user's folders everything that names it follows; into or out of
    _Trash only the paths and statuses change. Safe to repeat."""
    old_key, new_key = _key_or_none(root, src), _key_or_none(root, dst)
    if old_key is not None and new_key is not None:
        return repository.follow_folder(
            conn, src, dst, old_key=old_key, new_key=new_key, status=None
        )
    status = "sorted" if new_key is not None else "ignored"
    return repository.follow_folder(conn, src, dst, old_key=None, new_key=None, status=status)


def _key_or_none(root: Path, folder: Path) -> str | None:
    """The key of a category folder, or None for a reserved one (like _Trash/...)."""
    return folder_key(root, folder) if _is_category(root, folder) else None


def _is_category(root: Path, folder: Path) -> bool:
    return folder != root and zone_of(folder / "x", root) is Zone.CATEGORY


def _existing_folder(root: Path, key: str) -> Path:
    path = folder_path(root, key)
    if not key or not _is_category(root, path) or not path.is_dir():
        raise FolderError(f"There is no folder {key!r}")
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise FolderError(f"{key} is a link, not one of your folders")
    return path


def _free_folder_name(parent: Path, name: str) -> Path:
    if not os.path.lexists(parent / name):
        return parent / name
    for number in range(2, MAX_SUFFIX + 1):
        candidate = parent / f"{name} ({number})"
        if not os.path.lexists(candidate):
            return candidate
    raise FolderError(f"No free name left for {name} in {parent}")


def _known_hash(conn: sqlite3.Connection, path: Path) -> str | None:
    """The stored hash of an unchanged file, so moving it doesn't read it (an online-only
    iCloud file would be downloaded)."""
    record = repository.get_file_by_path(conn, path)
    try:
        st = path.stat()
    except OSError:
        return None
    if (
        record is None
        or record.sha256 is None
        or record.size != st.st_size
        or record.mtime is None
        or abs(record.mtime - st.st_mtime) > 1e-3
    ):
        return None
    return record.sha256


def _place(root: Path, folder: Path) -> str:
    return folder_key(root, folder).replace("/", " / ") or "your AutoShelf folder"
