"""Safe moves inside the library root.

Guarantees (CLAUDE.md safety rules):

- never overwrites: a taken name becomes ``name (2).ext``, ``name (3).ext``, ...;
- never deletes and never creates folders;
- refuses anything that resolves outside the root (including via links or junctions),
  except a folder the user explicitly opted in for this move (a watched folder, or a
  folder they asked to organize), which must be on the same drive: a move is a rename,
  never a copy followed by a delete;
- writes the history row as ``pending`` *before* touching the file and marks it ``done`` after.
"""

import logging
import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

from autoshelf.db import repository
from autoshelf.fsutil import is_within, sha256_file
from autoshelf.layout import Zone, zone_of

log = logging.getLogger(__name__)

MAX_COLLISION_SUFFIX = 9999

BeforeRename = Callable[[Path, Path], None]


class MoveError(Exception):
    """A move was refused or failed. The file was left where it was."""


@dataclass(frozen=True)
class MoveResult:
    """A completed move."""

    history_id: int
    file_id: int
    src: Path
    dst: Path


def candidate_names(name: str) -> Iterator[str]:
    """Yield ``name``, then ``stem (2).ext``, ``stem (3).ext``, ... up to the suffix limit."""
    pure = PurePath(name)
    yield name
    for number in range(2, MAX_COLLISION_SUFFIX + 1):
        yield f"{pure.stem} ({number}){pure.suffix}"


def free_destination(dst_dir: Path, name: str) -> Path:
    """Return the first path in ``dst_dir`` for ``name`` that nothing occupies yet."""
    return _next_free(dst_dir / candidate for candidate in candidate_names(name))


def status_after_move(dst: Path, root: Path, action: str) -> str:
    """Return the ``files.status`` a file should have at ``dst``.

    A file that an undo put back in the root is ``ignored`` so the watcher leaves it alone.
    """
    zone = zone_of(dst, root)
    if zone is Zone.INBOX:
        return "inbox"
    if zone is Zone.CATEGORY:
        return "sorted"
    if zone is Zone.DROP and action != "undo":
        return "new"
    return "ignored"


def move_file(
    conn: sqlite3.Connection,
    src: Path,
    dst_dir: Path,
    *,
    root: Path,
    reason: str,
    action: str = "move",
    new_name: str | None = None,
    sha256: str | None = None,
    confidence: float | None = None,
    batch_id: str | None = None,
    undo_of: int | None = None,
    details: dict[str, Any] | None = None,
    before_rename: BeforeRename | None = None,
    allowed_source: Path | None = None,
    allowed_target: Path | None = None,
) -> MoveResult:
    """Move ``src`` into the existing folder ``dst_dir``, safely and with a history record.

    ``before_rename(src, dst)`` is called right before each rename attempt; the watcher
    uses it to recognise its own file events. ``details`` (e.g. the classifier's top
    candidates) is stored with the history row. Raises MoveError if the move is refused or
    fails, in which case the file stays where it was and the history row is ``failed``.

    ``allowed_source`` is a folder outside the root that ``src`` may come from; the
    history then records it, so the move can be undone. ``allowed_target`` is a folder
    outside the root that ``dst_dir`` may be (undo uses it to put such a file back).
    """
    root = Path(root).resolve(strict=True)
    source = _checked_source(Path(src), root, allowed_source)
    target_dir = _checked_target_dir(Path(dst_dir), root, allowed_target)
    if not is_within(source, root):
        details = {**(details or {}), "source_folder": str(source.parent)}
    name = new_name or source.name
    _check_name(name)
    if target_dir / name == source:
        raise MoveError(f"{source.name} is already in {target_dir}")

    st = source.stat()
    digest = sha256 or sha256_file(source)
    file_id = repository.upsert_file(
        conn, source, sha256=digest, size=st.st_size, mtime=st.st_mtime
    )
    candidates = (target_dir / candidate for candidate in candidate_names(name))
    dst = _next_free(candidates)
    history_id = repository.insert_history(
        conn,
        action=action,
        src=source,
        dst=dst,
        reason=reason,
        root=root,
        file_id=file_id,
        sha256=digest,
        confidence=confidence,
        batch_id=batch_id,
        undo_of=undo_of,
        details=details,
    )
    try:
        dst = _rename_with_retries(conn, history_id, source, dst, candidates, before_rename)
    except (OSError, MoveError) as exc:
        repository.set_history_state(conn, history_id, "failed", note=str(exc))
        raise MoveError(f"Could not move {source.name}: {exc}") from exc

    # The record follows the file before the row is marked done: while the row is pending,
    # the library knows not to mistake this move for one the user made (actions.learning).
    repository.set_file_location(conn, file_id, dst, status_after_move(dst, root, action))
    repository.set_history_state(conn, history_id, "done")
    log.debug("History #%d: %s %s -> %s", history_id, action, source, dst)
    return MoveResult(history_id=history_id, file_id=file_id, src=source, dst=dst)


def _rename_with_retries(
    conn: sqlite3.Connection,
    history_id: int,
    source: Path,
    dst: Path,
    candidates: Iterator[Path],
    before_rename: BeforeRename | None,
) -> Path:
    """Rename, moving on to the next free name if another file grabbed ``dst`` meanwhile."""
    while True:
        if before_rename is not None:
            before_rename(source, dst)
        try:
            _rename_no_replace(source, dst)
            return dst
        except FileExistsError:
            dst = _next_free(candidates)
            repository.update_history_dst(conn, history_id, dst)


def _rename_no_replace(src: Path, dst: Path) -> None:
    """Rename atomically, raising FileExistsError rather than replacing an existing file."""
    if os.name == "nt":
        os.rename(src, dst)  # Windows refuses to replace an existing destination.
    else:
        os.link(src, dst)  # Fails with FileExistsError if dst exists.
        os.unlink(src)


def _next_free(candidates: Iterator[Path]) -> Path:
    for candidate in candidates:
        if not os.path.lexists(candidate):
            return candidate
    raise MoveError(f"No free name left after {MAX_COLLISION_SUFFIX} tries")


def same_drive(a: Path, b: Path) -> bool:
    """True if two existing paths are on the same volume (so a move is a rename)."""
    return os.stat(a).st_dev == os.stat(b).st_dev


def _checked_source(src: Path, root: Path, allowed: Path | None) -> Path:
    if src.is_symlink():
        raise MoveError(f"Refusing to move a symbolic link: {src}")
    try:
        resolved = src.resolve(strict=True)
    except FileNotFoundError:
        raise MoveError(f"{src} no longer exists") from None
    if not resolved.is_file():
        raise MoveError(f"Only files are moved, not {resolved}")
    if resolved != root and is_within(resolved, root):
        return resolved
    if allowed is not None and is_within(resolved, _resolved_dir(allowed)):
        if not same_drive(resolved, root):
            raise MoveError(
                f"{resolved} is on another drive than {root}; AutoShelf only moves files "
                "and never copies and deletes them"
            )
        return resolved
    raise MoveError(f"{resolved} is outside the root {root}")


def _checked_target_dir(dst_dir: Path, root: Path, allowed: Path | None) -> Path:
    try:
        resolved = dst_dir.resolve(strict=True)
    except FileNotFoundError:
        raise MoveError(
            f"Destination folder {dst_dir} does not exist (AutoShelf never creates folders itself)"
        ) from None
    if not resolved.is_dir():
        raise MoveError(f"Destination is not a folder: {resolved}")
    if is_within(resolved, root):
        return resolved
    if allowed is not None and resolved == _resolved_dir(allowed):
        if not same_drive(resolved, root):
            raise MoveError(f"{resolved} is on another drive than {root}")
        return resolved
    raise MoveError(f"Destination {resolved} is outside the root {root}")


def _resolved_dir(folder: Path) -> Path:
    try:
        return Path(folder).resolve(strict=True)
    except FileNotFoundError:
        raise MoveError(f"The folder {folder} no longer exists") from None


def _check_name(name: str) -> None:
    if name in {"", ".", ".."} or PurePath(name).name != name or "/" in name or "\\" in name:
        raise MoveError(f"Invalid file name: {name!r}")
