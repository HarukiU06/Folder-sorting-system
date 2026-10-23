from pathlib import Path

import pytest
from helpers import make

from autoshelf.actions.mover import move_file
from autoshelf.actions.undo import UndoError, undo, undo_last
from autoshelf.db import repository


@pytest.fixture
def inbox(root: Path) -> Path:
    folder = root / "_Inbox"
    folder.mkdir()
    return folder


def test_undo_moves_file_back(conn, root: Path, inbox: Path) -> None:
    src = make(root / "Unsupervised learning 教師なし.pdf", "pca")
    moved = move_file(conn, src, inbox, root=root, reason="test")

    result = undo(conn, moved.history_id)

    assert result.dst == src
    assert src.read_text(encoding="utf-8") == "pca"
    assert not moved.dst.exists()
    assert repository.get_history(conn, moved.history_id).state == "undone"
    undo_entry = repository.get_history(conn, result.history_id)
    assert (undo_entry.action, undo_entry.state, undo_entry.undo_of) == (
        "undo",
        "done",
        moved.history_id,
    )
    record = repository.get_file_by_path(conn, src)
    assert record.status == "ignored"  # the watcher must not sort it again


def test_undo_never_overwrites_a_file_now_at_the_original_spot(
    conn, root: Path, inbox: Path
) -> None:
    src = make(root / "a.txt", "original")
    moved = move_file(conn, src, inbox, root=root, reason="test")
    make(root / "a.txt", "newcomer")

    result = undo(conn, moved.history_id)

    assert result.dst == root / "a (2).txt"
    assert (root / "a.txt").read_text() == "newcomer"
    assert result.dst.read_text() == "original"


def test_undo_refuses_when_file_is_gone(conn, root: Path, inbox: Path) -> None:
    moved = move_file(conn, make(root / "a.txt"), inbox, root=root, reason="test")
    (root / "Elsewhere").mkdir()
    moved.dst.rename(root / "Elsewhere" / "a.txt")

    with pytest.raises(UndoError, match="no longer in"):
        undo(conn, moved.history_id)
    assert repository.get_history(conn, moved.history_id).state == "done"
    assert (root / "Elsewhere" / "a.txt").exists()


def test_undo_refuses_changed_file_unless_forced(conn, root: Path, inbox: Path) -> None:
    src = make(root / "a.txt", "v1")
    moved = move_file(conn, src, inbox, root=root, reason="test")
    moved.dst.write_text("v2 edited")

    with pytest.raises(UndoError, match="changed"):
        undo(conn, moved.history_id)
    assert moved.dst.exists()

    undo(conn, moved.history_id, force=True)
    assert src.read_text() == "v2 edited"


def test_undo_refuses_to_recreate_a_deleted_folder(conn, root: Path) -> None:
    category = root / "Old Category"
    src = make(category / "a.txt")
    (root / "New").mkdir()
    moved = move_file(conn, src, root / "New", root=root, reason="test")
    category.rmdir()

    with pytest.raises(UndoError, match="won't recreate"):
        undo(conn, moved.history_id)
    assert not category.exists()
    assert moved.dst.exists()


def test_cannot_undo_twice(conn, root: Path, inbox: Path) -> None:
    moved = move_file(conn, make(root / "a.txt"), inbox, root=root, reason="test")
    undo(conn, moved.history_id)
    with pytest.raises(UndoError, match="undone"):
        undo(conn, moved.history_id)


def test_cannot_undo_an_undo(conn, root: Path, inbox: Path) -> None:
    moved = move_file(conn, make(root / "a.txt"), inbox, root=root, reason="test")
    result = undo(conn, moved.history_id)
    with pytest.raises(UndoError, match="only moves, renames and new files"):
        undo(conn, result.history_id)


def test_unknown_history_id(conn) -> None:
    with pytest.raises(UndoError, match="no history entry"):
        undo(conn, 999)


def test_undo_last_goes_newest_first(conn, root: Path, inbox: Path) -> None:
    moves = [
        move_file(conn, make(root / f"{name}.txt"), inbox, root=root, reason="test")
        for name in ("one", "two", "three")
    ]

    results = undo_last(conn, 2)

    assert [r.src.name for r in results] == ["three.txt", "two.txt"]
    assert (root / "three.txt").exists()
    assert (root / "two.txt").exists()
    assert (inbox / "one.txt").exists()
    assert repository.get_history(conn, moves[0].history_id).state == "done"


def test_undo_last_stops_at_first_failure_and_reports_progress(
    conn, root: Path, inbox: Path
) -> None:
    first = move_file(conn, make(root / "one.txt"), inbox, root=root, reason="test")
    move_file(conn, make(root / "two.txt"), inbox, root=root, reason="test")
    move_file(conn, make(root / "three.txt"), inbox, root=root, reason="test")
    (inbox / "two.txt").write_text("edited")

    with pytest.raises(UndoError) as exc:
        undo_last(conn, 3)

    assert [r.src.name for r in exc.value.completed] == ["three.txt"]
    assert (inbox / "two.txt").exists()
    assert repository.get_history(conn, first.history_id).state == "done"
