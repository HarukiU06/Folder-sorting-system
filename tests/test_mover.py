import os
from pathlib import Path

import pytest
from helpers import make, windows_only

from autoshelf.actions.mover import (
    MoveError,
    candidate_names,
    free_destination,
    move_file,
    status_after_move,
)
from autoshelf.db import repository
from autoshelf.fsutil import sha256_file


@pytest.mark.parametrize(
    ("name", "second", "third"),
    [
        ("report.pdf", "report (2).pdf", "report (3).pdf"),
        ("archive.tar.gz", "archive.tar (2).gz", "archive.tar (3).gz"),
        ("README", "README (2)", "README (3)"),
        ("notes (2).txt", "notes (2) (2).txt", "notes (2) (3).txt"),
        ("Résumé 履歴書 v1.docx", "Résumé 履歴書 v1 (2).docx", "Résumé 履歴書 v1 (3).docx"),
    ],
)
def test_candidate_names(name: str, second: str, third: str) -> None:
    names = candidate_names(name)
    assert [next(names), next(names), next(names)] == [name, second, third]


def test_free_destination_skips_taken_names(root: Path) -> None:
    make(root / "a.txt")
    make(root / "a (2).txt")
    assert free_destination(root, "a.txt") == root / "a (3).txt"
    assert free_destination(root, "b.txt") == root / "b.txt"


def test_move_records_history_and_file(conn, root: Path) -> None:
    src = make(root / "Lecture notes 講義.pdf", "k-means")
    dst_dir = (root / "Machine Learning").resolve()
    dst_dir.mkdir()

    result = move_file(conn, src, dst_dir, root=root, reason="test")

    assert result.dst == dst_dir / "Lecture notes 講義.pdf"
    assert result.dst.read_text(encoding="utf-8") == "k-means"
    assert not src.exists()
    entry = repository.get_history(conn, result.history_id)
    assert entry.state == "done"
    assert entry.action == "move"
    assert (entry.src, entry.dst, entry.root) == (str(src), str(result.dst), str(root))
    assert entry.sha256 == sha256_file(result.dst)
    record = repository.get_file_by_path(conn, result.dst)
    assert record is not None
    assert record.status == "sorted"


def test_history_is_pending_while_the_file_moves(conn, root: Path) -> None:
    src = make(root / "a.txt")
    seen_states = []

    def before_rename(source: Path, dst: Path) -> None:
        (entry,) = repository.list_history(conn, limit=1)
        seen_states.append(entry.state)
        assert source.exists()

    (root / "Docs").mkdir()
    move_file(conn, src, root / "Docs", root=root, reason="test", before_rename=before_rename)
    assert seen_states == ["pending"]


def test_collision_appends_suffix_and_never_overwrites(conn, root: Path) -> None:
    make(root / "Finance" / "invoice.pdf", "existing")
    make(root / "Finance" / "invoice (2).pdf", "existing 2")
    src = make(root / "invoice.pdf", "new")

    result = move_file(conn, src, root / "Finance", root=root, reason="test")

    assert result.dst.name == "invoice (3).pdf"
    assert (root / "Finance" / "invoice.pdf").read_text() == "existing"
    assert (root / "Finance" / "invoice (2).pdf").read_text() == "existing 2"
    assert result.dst.read_text() == "new"


@windows_only
def test_collision_is_case_insensitive_on_windows(conn, root: Path) -> None:
    make(root / "Finance" / "Report.PDF", "existing")
    src = make(root / "report.pdf", "new")
    result = move_file(conn, src, root / "Finance", root=root, reason="test")
    assert result.dst.name == "report (2).pdf"
    assert (root / "Finance" / "Report.PDF").read_text() == "existing"


def test_race_for_the_name_moves_on_to_next_suffix(conn, root: Path) -> None:
    src = make(root / "a.txt", "mine")
    (root / "Docs").mkdir()
    intruder = root / "Docs" / "a.txt"

    def grab_name_first(source: Path, dst: Path) -> None:
        if dst.name == "a.txt":
            intruder.write_text("someone else's")

    result = move_file(
        conn, src, root / "Docs", root=root, reason="test", before_rename=grab_name_first
    )

    assert result.dst.name == "a (2).txt"
    assert intruder.read_text() == "someone else's"
    assert result.dst.read_text() == "mine"
    assert repository.get_history(conn, result.history_id).dst == str(result.dst)


def test_rejects_source_outside_root(conn, root: Path, tmp_path: Path) -> None:
    outside = make(tmp_path / "elsewhere" / "a.txt")
    with pytest.raises(MoveError, match="outside the root"):
        move_file(conn, outside, root, root=root, reason="test")
    assert outside.exists()
    assert repository.list_history(conn) == []


def test_rejects_destination_outside_root(conn, root: Path, tmp_path: Path) -> None:
    src = make(root / "a.txt")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(MoveError, match="outside the root"):
        move_file(conn, src, outside, root=root, reason="test")
    assert src.exists()


def test_rejects_dotdot_escape(conn, root: Path, tmp_path: Path) -> None:
    src = make(root / "a.txt")
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(MoveError, match="outside the root"):
        move_file(conn, src, root / ".." / "elsewhere", root=root, reason="test")
    assert src.exists()


def test_never_creates_missing_destination_folder(conn, root: Path) -> None:
    src = make(root / "a.txt")
    with pytest.raises(MoveError, match="never creates folders"):
        move_file(conn, src, root / "New Category", root=root, reason="test")
    assert not (root / "New Category").exists()
    assert src.exists()


def test_rejects_directories(conn, root: Path) -> None:
    (root / "Folder").mkdir()
    (root / "Other").mkdir()
    with pytest.raises(MoveError, match="Only files"):
        move_file(conn, root / "Folder", root / "Other", root=root, reason="test")


def test_rejects_path_in_new_name(conn, root: Path) -> None:
    src = make(root / "a.txt")
    with pytest.raises(MoveError, match="Invalid file name"):
        move_file(conn, src, root, root=root, reason="test", new_name=r"..\escape.txt")
    assert src.exists()


def test_rejects_moving_onto_itself(conn, root: Path) -> None:
    src = make(root / "a.txt")
    with pytest.raises(MoveError, match="already in"):
        move_file(conn, src, root, root=root, reason="test")


def test_rejects_symlink_escaping_root(conn, root: Path, tmp_path: Path) -> None:
    target = make(tmp_path / "secret" / "real.txt")
    link = root / "link.txt"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights on Windows")
    (root / "Docs").mkdir()
    with pytest.raises(MoveError, match="symbolic link"):
        move_file(conn, link, root / "Docs", root=root, reason="test")
    assert target.exists()


@windows_only
def test_failed_rename_marks_history_failed_and_keeps_file(conn, root: Path) -> None:
    src = make(root / "busy.txt", "in use")
    (root / "Docs").mkdir()
    with open(src, "rb"), pytest.raises(MoveError, match="Could not move"):
        # Python's open() doesn't share delete access, so Windows refuses the rename.
        move_file(conn, src, root / "Docs", root=root, reason="test")
    (entry,) = repository.list_history(conn)
    assert entry.state == "failed"
    assert entry.details["note"]
    assert src.read_text() == "in use"
    assert not (root / "Docs" / "busy.txt").exists()


@pytest.mark.parametrize(
    ("relative", "action", "status"),
    [
        ("_Inbox/a.txt", "move", "inbox"),
        ("Finance/a.txt", "move", "sorted"),
        ("Finance/Taxes/a.txt", "move", "sorted"),
        ("a.txt", "undo", "ignored"),
        ("a.txt", "move", "new"),
        ("_Duplicates/a.txt", "move", "ignored"),
    ],
)
def test_status_after_move(root: Path, relative: str, action: str, status: str) -> None:
    assert status_after_move(root / relative, root, action) == status
