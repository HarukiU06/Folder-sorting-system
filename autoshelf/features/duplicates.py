"""Exact duplicates and possible versions (SPEC section 5.6).

Nothing is ever deleted. Exact duplicates (same SHA-256) are only listed; "keep one"
moves the other copies into ``_Duplicates/`` (one undoable batch), where the user can
delete them by hand. Possible versions (very similar content and similar names, such as
``report_v2`` and ``report final``, or iCloud conflict copies like ``file 2.pdf``) are
only shown.
"""

import difflib
import os
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath

from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.db import repository
from autoshelf.fsutil import sha256_file
from autoshelf.layout import RESERVED_PREFIX, Zone, zone_of
from autoshelf.ml.vectors import from_blob

DUPLICATES_DIRNAME = RESERVED_PREFIX + "Duplicates"
# Similarity of file vectors (default model): near-identical documents ~0.87 and up,
# while different files in one folder stay below ~0.75.
VERSION_SIMILARITY = 0.85
SAME_NAME_SIMILARITY = 0.75  # enough when the names only differ by a version marker
NAME_SIMILARITY = 0.5

_SEARCHED_ZONES = (Zone.CATEGORY, Zone.INBOX, Zone.DROP)
_VERSION_MARKER = re.compile(
    r"(?:[\s_.\-]*(?:v\d+(?:\.\d+)*|ver\d*|version\s*\d*|final|draft|rev\d*|revised|"
    r"copy|コピー|old|new|latest|updated|edited|\(\d+\)|\s\d{1,2}))+$",
    re.IGNORECASE,
)


class DuplicateError(Exception):
    """Duplicates can't be tidied up as asked. Nothing was moved."""


@dataclass(frozen=True)
class DuplicateGroup:
    """Files with identical content."""

    paths: tuple[Path, ...]
    size: int


@dataclass(frozen=True)
class VersionGroup:
    """Files that look like versions of one document."""

    paths: tuple[Path, ...]
    similarity: float  # the lowest similarity that links two of them


def duplicates_dir(root: Path) -> Path:
    """The folder extra copies are moved to (it may not exist yet)."""
    return root / DUPLICATES_DIRNAME


def exact_duplicates(conn: sqlite3.Connection, root: Path) -> list[DuplicateGroup]:
    """Groups of files under ``root`` (not in ``_Duplicates``) with identical content."""
    groups = []
    for records in repository.duplicate_hashes(conn):
        paths: dict[str, Path] = {}
        for record in records:
            path = Path(record.path)
            if zone_of(path, root) in _SEARCHED_ZONES and path.is_file():
                paths.setdefault(str(path).casefold(), path)
        if len(paths) > 1:
            found = tuple(paths.values())
            groups.append(DuplicateGroup(found, found[0].stat().st_size))
    groups.sort(key=lambda group: group.paths[0].name.casefold())
    return groups


def keep_one(
    conn: sqlite3.Connection,
    root: Path,
    keep: Path,
    others: Iterable[Path],
    *,
    before_rename: BeforeRename | None = None,
) -> list[MoveResult]:
    """Keep ``keep`` and move the identical ``others`` into ``_Duplicates`` (one batch).

    Each file is checked again first: only exact copies of ``keep`` are moved.
    """
    others = [Path(other) for other in others if Path(other) != keep]
    if not keep.is_file():
        raise DuplicateError(f"{keep.name} is no longer there")
    digest = sha256_file(keep)
    for other in others:
        if not other.is_file() or sha256_file(other) != digest:
            raise DuplicateError(f"{other.name} is no longer an exact copy of {keep.name}")
    target = duplicates_dir(root)
    if others and not target.is_dir():
        target.mkdir()  # AutoShelf's own reserved folder, like _Inbox
    batch = uuid.uuid4().hex
    moved = []
    for other in others:
        try:
            moved.append(
                move_file(
                    conn,
                    other,
                    target,
                    root=root,
                    reason=f"duplicate of {keep.name}",
                    sha256=digest,
                    batch_id=batch,
                    details={"duplicate_of": str(keep)},
                    before_rename=before_rename,
                )
            )
        except MoveError as exc:
            raise DuplicateError(str(exc)) from exc
    return moved


def base_name(name: str) -> str:
    """A name without extension and version markers: "Report_v2 final (3).pdf" -> "report"."""
    stem = unicodedata.normalize("NFKC", PurePath(name).stem).casefold()
    previous = None
    while previous != stem:
        previous = stem
        stem = _VERSION_MARKER.sub("", stem).strip(" _-.")
    return re.sub(r"[\s_\-.]+", " ", stem) or PurePath(name).stem.casefold()


def name_similarity(a: str, b: str) -> float:
    """How alike two file names are once version markers are removed (0 to 1)."""
    return difflib.SequenceMatcher(None, base_name(a), base_name(b)).ratio()


def possible_versions(conn: sqlite3.Connection, root: Path, model: str) -> list[VersionGroup]:
    """Groups of files that look like versions of each other (exact copies excluded)."""
    items = []
    for file_id, stored, blob in repository.file_vectors(conn, model):
        path = Path(stored)
        if zone_of(path, root) in _SEARCHED_ZONES and os.path.isfile(path):
            record = repository.get_file(conn, file_id)
            items.append((path, record.sha256 if record else None, from_blob(blob)))
    parent = list(range(len(items)))
    lowest: dict[tuple[int, int], float] = {}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            path_a, hash_a, vector_a = items[i]
            path_b, hash_b, vector_b = items[j]
            if hash_a is not None and hash_a == hash_b:
                continue  # exact copies are listed as duplicates
            similarity = float(vector_a @ vector_b)
            same_base = base_name(path_a.name) == base_name(path_b.name)
            if (similarity >= SAME_NAME_SIMILARITY and same_base) or (
                similarity >= VERSION_SIMILARITY
                and name_similarity(path_a.name, path_b.name) >= NAME_SIMILARITY
            ):
                root_a, root_b = find(i), find(j)
                parent[root_b] = root_a
                lowest[(i, j)] = similarity
    members: dict[int, list[int]] = {}
    for i in range(len(items)):
        members.setdefault(find(i), []).append(i)
    groups = []
    for indexes in members.values():
        if len(indexes) < 2:
            continue
        links = [value for (i, j), value in lowest.items() if i in indexes and j in indexes]
        paths = tuple(sorted((items[i][0] for i in indexes), key=lambda p: p.name.casefold()))
        groups.append(VersionGroup(paths, min(links)))
    groups.sort(key=lambda group: group.paths[0].name.casefold())
    return groups
