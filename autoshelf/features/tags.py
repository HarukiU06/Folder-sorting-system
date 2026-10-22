"""Tags and virtual views (SPEC section 5.1).

A file lives in one folder but can carry any number of tags, kept in AutoShelf's database
(the iPhone only sees folders). When a file fits two folders about equally, the runner-up
folders are offered as tags. Tagging is logged in history (it isn't a move, so there is
nothing to undo: remove the tag instead).
"""

import sqlite3
from pathlib import Path

from autoshelf.db import repository
from autoshelf.fsutil import sha256_file
from autoshelf.ml.classifier import Classification, is_related

TAG_CLOSE = 0.08  # runner-ups within this of the best folder are offered as tags
MAX_TAG_CHARS = 40


class TagError(Exception):
    """A tag can't be added as given."""


def clean_tag(name: str) -> str:
    """A tag as stored: single spaces, at most MAX_TAG_CHARS characters."""
    cleaned = " ".join(name.split())
    if not cleaned:
        raise TagError("A tag needs a name")
    if len(cleaned) > MAX_TAG_CHARS:
        raise TagError(f"Keep tags under {MAX_TAG_CHARS} characters")
    return cleaned


def suggest_tags(classification: Classification | None, chosen: str | None = None) -> list[str]:
    """Names of runner-up folders that scored close to the best one (tag suggestions).

    ``chosen`` is the folder the file goes to; it and its parents and subfolders aren't
    suggested.
    """
    if classification is None or not classification.scores:
        return []
    chosen = chosen or (classification.choice.folder if classification.choice else None)
    best = classification.scores[0].score
    floor = max(best - TAG_CLOSE, classification.threshold - TAG_CLOSE)
    suggestions: list[str] = []
    for item in classification.top(3):
        if chosen is not None and is_related(item.folder, chosen):
            continue
        name = item.folder.rsplit("/", 1)[-1]
        if item.score >= floor and name not in suggestions:
            suggestions.append(name)
    return suggestions


def _record(conn: sqlite3.Connection, path: Path) -> repository.FileRecord:
    record = repository.get_file_by_path(conn, path)
    if record is not None:
        return record
    st = path.stat()
    file_id = repository.upsert_file(
        conn, path, sha256=sha256_file(path), size=st.st_size, mtime=st.st_mtime
    )
    return repository.get_file(conn, file_id)


def tag_file(conn: sqlite3.Connection, path: Path, names: list[str], root: Path) -> list[str]:
    """Add tags to a file; returns the tags that were new to it."""
    if not path.is_file():
        raise TagError(f"{path.name} is no longer there")
    record = _record(conn, path)
    added = []
    for name in dict.fromkeys(clean_tag(name) for name in names):
        stored = repository.add_tag(conn, record.id, name)
        if stored is not None and stored not in added:
            added.append(stored)
            name = stored
            repository.insert_history(
                conn,
                action="tag",
                src=path,
                dst=path,
                reason="tag",
                root=root,
                state="done",
                file_id=record.id,
                sha256=record.sha256,
                details={"tag": name},
            )
    return added


def untag_file(conn: sqlite3.Connection, path: Path, name: str, root: Path) -> bool:
    """Remove a tag from a file. Returns False if it didn't have it."""
    record = repository.get_file_by_path(conn, path)
    if record is None or not repository.remove_tag(conn, record.id, name):
        return False
    repository.insert_history(
        conn,
        action="tag",
        src=path,
        dst=path,
        reason="untag",
        root=root,
        state="done",
        file_id=record.id,
        details={"tag": name},
    )
    return True


def file_tags(conn: sqlite3.Connection, path: Path) -> list[str]:
    """The tags of the file at ``path``."""
    record = repository.get_file_by_path(conn, path)
    return repository.tags_of(conn, record.id) if record else []


def tagged_files(conn: sqlite3.Connection, name: str) -> list[Path]:
    """Every file (still there) with the tag ``name``: a virtual folder."""
    return [
        Path(record.path)
        for record in repository.files_tagged(conn, name)
        if Path(record.path).is_file()
    ]
