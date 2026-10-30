"""Changing folders from the Library: make, rename, move, move to _Trash (never delete)."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions import folders
from autoshelf.actions.folders import FolderError
from autoshelf.db import repository
from autoshelf.ml.library import LibraryIndex


@pytest.fixture
def shelf(root: Path) -> Path:
    make(root / "My Folder" / "lecture notes 1.txt", "notes about eigenvalues")
    make(root / "My Folder" / "Ünïcode notes" / "kanji ノート.txt", "日本語のノート")
    make(root / "Finance" / "invoice march.txt", "invoice amount due")
    (root / "Empty").mkdir()
    return root


def test_make_folder_refuses_reserved_and_existing(shelf: Path) -> None:
    assert folders.make_folder(shelf, "My Folder", "Notes") == "My Folder/Notes"
    assert (shelf / "My Folder" / "Notes").is_dir()
    for bad in ("_Private", "CON", "nul.txt", "Notes", "a/b", ""):
        with pytest.raises(FolderError):
            folders.make_folder(shelf, "My Folder", bad)
    with pytest.raises(FolderError):
        folders.make_folder(shelf, "No such folder", "Notes")


def test_move_files_records_history_and_learning(conn, shelf: Path) -> None:
    first = make(shelf / "My Folder" / "a.txt", "a")
    second = make(shelf / "My Folder" / "b.txt", "b")

    changes = folders.move_files(conn, shelf, [first, second], "Finance")

    assert [change.dst for change in changes.done] == [
        shelf / "Finance" / "a.txt",
        shelf / "Finance" / "b.txt",
    ]
    assert changes.batch_id is not None and changes.failed == []
    rows = [repository.get_history(conn, number) for number in changes.history_ids]
    assert [(row.action, row.reason, row.state) for row in rows] == [("move", "user", "done")] * 2
    learned = repository.list_corrections(conn)
    assert [(row["from_folder"], row["to_folder"], row["source"]) for row in learned] == [
        ("My Folder", "Finance", "external")
    ] * 2


def test_move_files_refuses_the_root(conn, shelf: Path) -> None:
    path = shelf / "Finance" / "invoice march.txt"
    with pytest.raises(FolderError):
        folders.move_files(conn, shelf, [path], "")
    assert path.exists()


def test_rename_folder_follows_and_refuses_collision(conn, shelf: Path) -> None:
    repository.set_folder_description(conn, "My Folder/Ünïcode notes", "kana notes")
    seen = []

    key = folders.rename_folder(
        conn,
        shelf,
        "My Folder/Ünïcode notes",
        "Japanese",
        before_rename=lambda a, b: seen.append(b),
    )

    assert key == "My Folder/Japanese"
    assert (shelf / "My Folder" / "Japanese" / "kanji ノート.txt").exists()
    assert seen == [shelf / "My Folder" / "Japanese"]
    assert repository.folder_descriptions(conn) == {"my folder/japanese": "kana notes"}
    entry = repository.list_history(conn, 1)[0]
    assert (entry.action, entry.state, entry.details["kind"]) == ("folder", "done", "rename")
    with pytest.raises(FolderError):
        folders.rename_folder(conn, shelf, "Finance", "Empty")
    assert folders.rename_folder(conn, shelf, "Finance", "finance") == "finance"
    assert [path.name for path in shelf.iterdir() if path.name.casefold() == "finance"] == [
        "finance"
    ]


def test_move_folder_into_itself_refused_and_collision_gets_suffix(conn, shelf: Path) -> None:
    with pytest.raises(FolderError):
        folders.move_folder(conn, shelf, "My Folder", "My Folder/Ünïcode notes")
    with pytest.raises(FolderError):
        folders.move_folder(conn, shelf, "My Folder", "My Folder")
    (shelf / "Finance" / "Empty").mkdir()

    assert folders.move_folder(conn, shelf, "Empty", "Finance") == "Finance/Empty (2)"
    assert folders.move_folder(conn, shelf, "Finance/Empty (2)", "") == "Empty (2)"


def test_trash_files_and_folders(conn, shelf: Path) -> None:
    invoice = shelf / "Finance" / "invoice march.txt"
    repository.upsert_file(
        conn, shelf / "My Folder" / "lecture notes 1.txt", sha256=None, size=1, mtime=1.0
    )
    (shelf / "Other" / "My Folder").mkdir(parents=True)

    changes = folders.trash(conn, shelf, [invoice], ["My Folder", "Other/My Folder"])

    trash = shelf / "_Trash"
    assert [change.dst for change in changes.done] == [
        trash / "invoice march.txt",
        trash / "My Folder",
        trash / "My Folder (2)",
    ]
    assert not invoice.exists() and (trash / "invoice march.txt").exists()
    assert (trash / "My Folder" / "Ünïcode notes" / "kanji ノート.txt").exists()
    rows = [repository.get_history(conn, number) for number in changes.history_ids]
    assert [(row.action, row.reason) for row in rows] == [
        ("move", "trash"),
        ("folder", "trash"),
        ("folder", "trash"),
    ]
    record = repository.get_file_by_path(conn, trash / "My Folder" / "lecture notes 1.txt")
    assert record is not None and record.status == "ignored"


def test_rename_then_refresh_records_no_corrections(conn, shelf: Path) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()

    folders.rename_folder(conn, shelf, "My Folder", "Lectures")
    folders.move_folder(conn, shelf, "Lectures/Ünïcode notes", "Finance")
    stats = index.refresh()

    assert stats.relocations == ()
    assert repository.list_corrections(conn) == []
    assert "Finance/Ünïcode notes" in index.folders
