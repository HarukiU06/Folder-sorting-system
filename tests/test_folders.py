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


def test_rename_onto_the_name_of_a_trashed_folder(conn, shelf: Path) -> None:
    make(shelf / "Uni" / "Notes" / "Week 1" / "old.txt", "old notes")
    make(shelf / "Uni" / "Lectures" / "Week 1" / "intro.txt", "first lecture")
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    repository.add_known_folders(
        conn, ["Uni", "Uni/Notes", "Uni/Notes/Week 1", "Uni/Lectures", "Uni/Lectures/Week 1"]
    )
    repository.set_folder_description(conn, "Uni/Lectures", "lecture slides")
    folders.trash(conn, shelf, [], ["Uni/Notes"])

    key = folders.rename_folder(conn, shelf, "Uni/Lectures", "Notes")

    assert key == "Uni/Notes"
    assert repository.list_history(conn, 1)[0].state == "done"
    known = repository.known_folders(conn)
    assert {"uni/notes", "uni/notes/week 1"} <= known
    assert not {"uni/lectures", "uni/lectures/week 1"} & known
    assert repository.folder_descriptions(conn)["uni/notes"] == "lecture slides"
    assert index.refresh().relocations == ()
    assert repository.list_corrections(conn) == []


# --- undo and crash recovery --------------------------------------------------------------


def test_undo_folder_rename_restores_keys(conn, shelf: Path) -> None:
    from autoshelf.actions.undo import undo

    repository.set_folder_description(conn, "My Folder", "lectures")
    folders.rename_folder(conn, shelf, "My Folder", "Lectures")
    entry = repository.list_history(conn, 1)[0]

    result = undo(conn, entry.id)

    assert result.dst == shelf / "My Folder"
    assert (shelf / "My Folder" / "lecture notes 1.txt").exists()
    assert repository.folder_descriptions(conn) == {"my folder": "lectures"}
    assert repository.get_history(conn, entry.id).state == "undone"
    back = repository.get_history(conn, result.history_id)
    assert (back.action, back.undo_of, back.details["folder"]) == ("undo", entry.id, True)


def test_undo_trashed_folder_restores_status_sorted(conn, shelf: Path) -> None:
    from autoshelf.actions.undo import undo_batch

    note = shelf / "My Folder" / "lecture notes 1.txt"
    repository.upsert_file(conn, note, sha256=None, size=1, mtime=1.0)
    changes = folders.trash(conn, shelf, [shelf / "Finance" / "invoice march.txt"], ["My Folder"])

    undo_batch(conn, changes.batch_id)

    assert note.exists() and (shelf / "Finance" / "invoice march.txt").exists()
    assert repository.get_file_by_path(conn, note).status == "sorted"
    assert list((shelf / "_Trash").iterdir()) == []


def test_undo_folder_refused_when_old_parent_gone(conn, shelf: Path) -> None:
    from autoshelf.actions.undo import UndoError, undo

    folders.move_folder(conn, shelf, "My Folder/Ünïcode notes", "Finance")
    moved = repository.list_history(conn, 1)[0]
    folders.trash(conn, shelf, [], ["My Folder"])

    with pytest.raises(UndoError, match="recreate"):
        undo(conn, moved.id)

    assert repository.get_history(conn, moved.id).state == "done"
    assert (shelf / "Finance" / "Ünïcode notes").is_dir()


def test_undo_trashed_file(conn, shelf: Path) -> None:
    from autoshelf.actions.undo import undo

    invoice = shelf / "Finance" / "invoice march.txt"
    changes = folders.trash(conn, shelf, [invoice], [])

    undo(conn, changes.history_ids[0])

    assert invoice.exists()
    assert repository.get_file_by_path(conn, invoice).status == "sorted"


def test_reconcile_pending_folder_rows(conn, shelf: Path) -> None:
    from autoshelf.actions.history import reconcile_pending

    note = shelf / "My Folder" / "lecture notes 1.txt"
    repository.upsert_file(conn, note, sha256=None, size=1, mtime=1.0)
    repository.set_folder_description(conn, "My Folder", "lectures")
    renamed = repository.insert_history(
        conn,
        action="folder",
        src=shelf / "My Folder",
        dst=shelf / "Lectures",
        reason="user",
        root=shelf,
        details={"kind": "rename", "folder": True},
    )
    (shelf / "My Folder").rename(shelf / "Lectures")  # AutoShelf stopped right after this
    never = repository.insert_history(
        conn,
        action="folder",
        src=shelf / "Finance",
        dst=shelf / "Money",
        reason="user",
        root=shelf,
        details={"kind": "rename", "folder": True},
    )

    reconcile_pending(conn)

    assert repository.get_history(conn, renamed).state == "done"
    assert repository.get_file_by_path(conn, shelf / "Lectures" / note.name) is not None
    assert repository.folder_descriptions(conn) == {"lectures": "lectures"}
    assert repository.get_history(conn, never).state == "failed"


def test_history_wording_for_folder_changes() -> None:
    from autoshelf import wording

    def row(**changes) -> repository.HistoryEntry:
        fields = dict(
            id=7,
            ts="t",
            file_id=None,
            action="folder",
            src=None,
            dst=None,
            reason="user",
            confidence=None,
            details={},
            batch_id=None,
            state="done",
            root=None,
            sha256=None,
            undo_of=None,
        )
        return repository.HistoryEntry(**{**fields, **changes})

    renamed = row(src="C:/S/A", dst="C:/S/B", details={"kind": "rename", "folder": True})
    assert wording.history_sentence(renamed, "(root)", "(root)") == "Renamed folder A to B"
    moved = row(src="C:/S/A", dst="C:/S/Work/A", details={"kind": "move", "folder": True})
    assert wording.history_sentence(moved, "(root)", "Work") == "Moved folder A to Work"
    binned = row(
        src="C:/S/A",
        dst="C:/S/_Trash/A (2)",
        reason="trash",
        details={"kind": "trash", "folder": True},
    )
    assert wording.history_sentence(binned, "(root)", "_Trash") == "Moved folder A to the Trash"
    assert wording.history_how(binned) == "you deleted it"
    file_binned = row(action="move", src="C:/S/F/a.pdf", dst="C:/S/_Trash/a.pdf", reason="trash")
    assert wording.history_sentence(file_binned, "F", "_Trash") == "Moved a.pdf to the Trash"
    back = row(
        action="undo",
        src="C:/S/_Trash/A",
        dst="C:/S/A",
        undo_of=3,
        reason="undo",
        details={"kind": "trash", "folder": True},
    )
    assert wording.history_sentence(back, "_Trash", "(root)") == (
        "Put folder A back in your AutoShelf folder"
    )
    resorted = row(action="move", src="C:/S/F/n.txt", dst="C:/S/F/Notes/n.txt", reason="resort")
    assert wording.history_how(resorted) == "sorted again when a new folder appeared"
