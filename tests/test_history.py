from pathlib import Path

from helpers import make

from autoshelf.actions.history import reconcile_pending
from autoshelf.db import repository
from autoshelf.fsutil import sha256_file


def pending_row(conn, root: Path, src: Path, dst: Path, sha256: str | None = None) -> int:
    """Simulate a crash right after the history row was written, before the move finished."""
    file_id = repository.upsert_file(conn, src, sha256=sha256, size=None, mtime=None)
    return repository.insert_history(
        conn,
        action="move",
        src=src,
        dst=dst,
        reason="test",
        root=root,
        file_id=file_id,
        sha256=sha256,
    )


def test_move_that_completed_is_marked_done(conn, root: Path) -> None:
    src, dst = root / "a.txt", root / "_Inbox" / "a.txt"
    make(dst, "content")
    history_id = pending_row(conn, root, src, dst, sha256_file(dst))

    (result,) = reconcile_pending(conn)

    assert (result.history_id, result.state) == (history_id, "done")
    assert repository.get_history(conn, history_id).state == "done"
    assert repository.get_file_by_path(conn, dst).status == "inbox"


def test_move_that_never_happened_is_marked_failed(conn, root: Path) -> None:
    src = make(root / "a.txt")
    history_id = pending_row(conn, root, src, root / "_Inbox" / "a.txt")

    reconcile_pending(conn)

    entry = repository.get_history(conn, history_id)
    assert entry.state == "failed"
    assert "had not happened" in entry.details["note"]
    assert src.exists()


def test_both_exist_is_failed(conn, root: Path) -> None:
    src = make(root / "a.txt", "mine")
    dst = make(root / "_Inbox" / "a.txt", "someone else's")
    history_id = pending_row(conn, root, src, dst, sha256_file(src))
    reconcile_pending(conn)
    assert repository.get_history(conn, history_id).state == "failed"
    assert src.read_text() == "mine"
    assert dst.read_text() == "someone else's"


def test_different_content_at_destination_is_failed(conn, root: Path) -> None:
    src = root / "a.txt"
    dst = make(root / "_Inbox" / "a.txt", "other")
    history_id = pending_row(conn, root, src, dst, sha256="0" * 64)
    reconcile_pending(conn)
    assert repository.get_history(conn, history_id).state == "failed"


def test_file_missing_everywhere_is_failed(conn, root: Path) -> None:
    history_id = pending_row(conn, root, root / "a.txt", root / "_Inbox" / "a.txt")
    reconcile_pending(conn)
    entry = repository.get_history(conn, history_id)
    assert entry.state == "failed"
    assert "missing" in entry.details["note"]


def test_completed_undo_marks_its_target_undone(conn, root: Path) -> None:
    original = repository.insert_history(
        conn,
        action="move",
        src=root / "a.txt",
        dst=root / "_Inbox" / "a.txt",
        reason="test",
        root=root,
        state="done",
    )
    repository.insert_history(
        conn,
        action="undo",
        src=root / "_Inbox" / "a.txt",
        dst=root / "a.txt",
        reason="undo",
        root=root,
        state="done",
        undo_of=original,
    )

    reconcile_pending(conn)

    assert repository.get_history(conn, original).state == "undone"


def test_nothing_pending_changes_nothing(conn) -> None:
    assert reconcile_pending(conn) == []
