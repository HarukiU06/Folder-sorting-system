"""Files AutoShelf makes (a compressed or converted copy): always a new file, never in place.

The new file is first written to AutoShelf's own working folder, outside the library. It
is then copied next to its source under a free name (``name (2).ext`` and so on), opened
with exclusive creation, so nothing is ever overwritten, not even a file that appears at
the last moment. The history row (action ``create``: src the first source, dst the new
file, sha256 the new file's content) is written before the copy and marked done after, so
a crash in between is settled at the next start (``actions.history``). Undoing it moves
the new file into ``_Trash/`` for the user to delete: AutoShelf never deletes files.
"""

import logging
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from autoshelf.actions.mover import candidate_names
from autoshelf.db import repository
from autoshelf.fsutil import is_within, sha256_file
from autoshelf.layout import PROCESSED_ZONES, RESERVED_PREFIX, Zone, zone_of
from autoshelf.reasons import CREATED, reason

log = logging.getLogger(__name__)

TRASH_DIRNAME = RESERVED_PREFIX + "Trash"
ACTION_CREATE = "create"
_COPY_CHUNK = 1024 * 1024

BeforeCreate = Callable[[Path], None]


class CreateError(Exception):
    """A new file couldn't be put in place. Nothing that existed was changed."""


@dataclass(frozen=True)
class Created:
    """A file AutoShelf made and put next to its source."""

    history_id: int
    file_id: int
    dst: Path


def trash_dir(root: Path) -> Path:
    """Where undone new files go for the user to delete (it may not exist yet)."""
    return root / TRASH_DIRNAME


def created_status(path: Path, root: Path, *, sort: bool = False) -> str:
    """The ``files.status`` of a new file: in a category folder it is part of the library;
    in the root or ``_Inbox`` it is sorted like any new file only if ``sort``."""
    zone = zone_of(path, root)
    if zone is Zone.CATEGORY:
        return "sorted"
    if zone in PROCESSED_ZONES and sort:
        return "new"
    return "ignored"


def place_new_file(
    conn: sqlite3.Connection,
    made: Path,
    folder: Path,
    name: str,
    *,
    root: Path,
    operation: str,
    sources: Sequence[Path],
    extra: Mapping | None = None,
    batch_id: str | None = None,
    sort: bool = False,
    before_create: BeforeCreate | None = None,
) -> Created:
    """Copy the finished file ``made`` into ``folder`` as ``name`` (or ``name (2)``...).

    ``folder`` must be inside the library (not a reserved ``_`` folder). The history row
    records ``operation`` (e.g. "zip"), the ``sources`` and ``extra`` (e.g. ``{"to": "pdf"}``)
    as the reason. ``before_create(dst)`` is called before each attempt (the watcher uses it
    to recognise its own new files). ``sort``: whether the watcher may sort a new file
    that lands in the root or ``_Inbox``. Raises CreateError; existing files are untouched.
    """
    root = Path(root).resolve(strict=True)
    target = _checked_folder(Path(folder), root)
    if not sources:
        raise CreateError("A new file needs the file(s) it was made from")
    if name in {"", ".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise CreateError(f"Invalid file name: {name!r}")
    digest = sha256_file(made)
    candidates = (target / candidate for candidate in candidate_names(name))
    dst = _next_free(candidates)
    why = reason(
        CREATED, operation=operation, sources=[str(source) for source in sources], **(extra or {})
    )
    history_id = repository.insert_history(
        conn,
        action=ACTION_CREATE,
        src=sources[0],
        dst=dst,
        reason=operation,
        root=root,
        sha256=digest,
        batch_id=batch_id,
        details={"reason": why},
    )
    try:
        dst = _copy_exclusive(conn, history_id, made, dst, candidates, before_create)
    except (OSError, CreateError) as exc:
        repository.set_history_state(conn, history_id, "failed", note=str(exc))
        raise CreateError(f"Couldn't make {name}: {exc}") from exc

    st = dst.stat()
    file_id = repository.upsert_file(conn, dst, sha256=digest, size=st.st_size, mtime=st.st_mtime)
    repository.set_file_status(conn, file_id, created_status(dst, root, sort=sort))
    repository.set_history_file(conn, history_id, file_id)
    repository.set_history_state(conn, history_id, "done")
    if zone_of(dst, root) is Zone.CATEGORY:
        repository.bump_library_version(conn)  # a new example in a folder
    log.debug("History #%d: created %s", history_id, dst)
    return Created(history_id, file_id, dst)


def record_settled(conn: sqlite3.Connection, entry: repository.HistoryEntry) -> None:
    """Give a new file that was found complete after a restart its file record."""
    dst, root = Path(entry.dst), Path(entry.root)
    st = dst.stat()
    file_id = repository.upsert_file(
        conn, dst, sha256=entry.sha256, size=st.st_size, mtime=st.st_mtime
    )
    repository.set_file_status(conn, file_id, created_status(dst, root))
    repository.set_history_file(conn, entry.id, file_id)


def _copy_exclusive(
    conn: sqlite3.Connection,
    history_id: int,
    made: Path,
    dst: Path,
    candidates: Iterator[Path],
    before_create: BeforeCreate | None,
) -> Path:
    """Copy ``made`` to ``dst``, moving on to the next free name if ``dst`` got taken."""
    while True:
        if before_create is not None:
            before_create(dst)
        try:
            out = open(dst, "xb")  # noqa: SIM115 - fails rather than replacing a file
        except FileExistsError:
            dst = _next_free(candidates)
            repository.update_history_dst(conn, history_id, dst)
            continue
        try:
            with out, open(made, "rb") as source:
                shutil.copyfileobj(source, out, _COPY_CHUNK)
        except OSError as exc:
            # Our own unfinished copy is left for the user to see; nothing is deleted.
            raise CreateError(f"{exc}; an incomplete copy may be at {dst}") from exc
        return dst


def _next_free(candidates: Iterator[Path]) -> Path:
    for candidate in candidates:
        if not os.path.lexists(candidate):
            return candidate
    raise CreateError("No free name left")


def _checked_folder(folder: Path, root: Path) -> Path:
    try:
        resolved = folder.resolve(strict=True)
    except FileNotFoundError:
        raise CreateError(f"The folder {folder} no longer exists") from None
    if not resolved.is_dir():
        raise CreateError(f"Not a folder: {resolved}")
    if not is_within(resolved, root):
        raise CreateError(
            f"{resolved} is outside your library ({root}); AutoShelf only makes files inside it"
        )
    if resolved != root and zone_of(resolved / "x", root) is Zone.RESERVED:
        raise CreateError(f"{resolved.name} is AutoShelf's own folder")
    return resolved
