"""Files AutoShelf makes (Phase 11): recorded as 'create', never overwriting, undone into
_Trash (never deleted), settled after a crash; and the history table that allows them."""

import sqlite3
from pathlib import Path

import pytest
from helpers import make

from autoshelf import wording
from autoshelf.actions.create import (
    TRASH_DIRNAME,
    CreateError,
    created_status,
    place_new_file,
)
from autoshelf.actions.history import reconcile_pending
from autoshelf.actions.undo import UndoError, undo, undo_last
from autoshelf.db import repository, schema
from autoshelf.db.connection import connect, open_database
from autoshelf.db.migrations import MIGRATIONS, current_version
from autoshelf.fsutil import sha256_file


@pytest.fixture
def made(tmp_path: Path) -> Path:
    """A finished new file in AutoShelf's working folder (outside the library)."""
    return make(tmp_path / "work" / "out.zip", "zipped bytes")


def test_a_new_file_goes_next_to_its_source(conn, root: Path, made: Path) -> None:
    source = make(root / "Finance" / "report.pdf", "the original")
    taken = make(root / "Finance" / "report.zip", "someone else's zip")

    created = place_new_file(
        conn, made, source.parent, "report.zip", root=root, operation="zip", sources=[source]
    )

    assert created.dst == root / "Finance" / "report (2).zip"  # never overwrites
    assert taken.read_text(encoding="utf-8") == "someone else's zip"
    assert created.dst.read_text(encoding="utf-8") == "zipped bytes"
    assert source.read_text(encoding="utf-8") == "the original"  # the original is kept
    entry = repository.get_history(conn, created.history_id)
    assert (entry.action, entry.state, entry.reason) == ("create", "done", "zip")
    assert (entry.src, entry.dst) == (str(source), str(created.dst))
    assert entry.sha256 == sha256_file(created.dst)
    assert entry.file_id == created.file_id
    assert entry.details["reason"] == {
        "code": "created",
        "operation": "zip",
        "sources": [str(source)],
    }
    assert wording.history_reason(entry) == "Made a ZIP of report.pdf"
    assert repository.get_file(conn, created.file_id).status == "sorted"  # in a folder


def test_a_name_taken_at_the_last_moment_is_not_overwritten(conn, root: Path, made: Path) -> None:
    source = make(root / "notes.txt", "notes")
    planned = root / "notes.zip"

    def someone_else_writes_first(dst: Path) -> None:
        if dst == planned and not dst.exists():
            dst.write_text("arrived just now", encoding="utf-8")

    created = place_new_file(
        conn,
        made,
        root,
        "notes.zip",
        root=root,
        operation="zip",
        sources=[source],
        before_create=someone_else_writes_first,
    )

    assert created.dst == root / "notes (2).zip"
    assert planned.read_text(encoding="utf-8") == "arrived just now"
    assert repository.get_history(conn, created.history_id).dst == str(created.dst)


def test_new_files_in_the_root_are_left_alone_unless_asked(conn, root: Path, made: Path) -> None:
    source = make(root / "a.txt")
    left = place_new_file(conn, made, root, "a.zip", root=root, operation="zip", sources=[source])
    sorted_ = place_new_file(
        conn, made, root, "a.zip", root=root, operation="zip", sources=[source], sort=True
    )
    assert repository.get_file(conn, left.file_id).status == "ignored"
    assert repository.get_file(conn, sorted_.file_id).status == "new"
    assert created_status(root / "_Inbox" / "x.zip", root) == "ignored"


@pytest.mark.parametrize("where", ["outside", "_Duplicates"])
def test_new_files_are_only_made_inside_the_library(
    conn, root: Path, tmp_path: Path, made: Path, where: str
) -> None:
    folder = tmp_path / "elsewhere" if where == "outside" else root / where
    folder.mkdir()
    with pytest.raises(CreateError):
        place_new_file(
            conn, made, folder, "x.zip", root=root, operation="zip", sources=[root / "a.txt"]
        )
    assert repository.list_history(conn) == []


def test_undoing_a_new_file_sets_it_aside_in_trash(conn, root: Path, made: Path) -> None:
    source = make(root / "Finance" / "report.pdf")
    created = place_new_file(
        conn, made, source.parent, "report.zip", root=root, operation="zip", sources=[source]
    )

    result = undo(conn, created.history_id)

    assert not created.dst.exists()
    assert result.dst == root / TRASH_DIRNAME / "report.zip"
    assert result.dst.read_text(encoding="utf-8") == "zipped bytes"  # not deleted
    assert source.exists()
    assert repository.get_history(conn, created.history_id).state == "undone"
    undo_row = repository.get_history(conn, result.history_id)
    assert (undo_row.action, undo_row.undo_of) == ("undo", created.history_id)
    assert repository.get_file_by_path(conn, result.dst).status == "ignored"


def test_a_new_file_changed_since_is_only_set_aside_when_forced(
    conn, root: Path, made: Path
) -> None:
    source = make(root / "report.pdf")
    created = place_new_file(
        conn, made, root, "report.zip", root=root, operation="zip", sources=[source]
    )
    created.dst.write_text("edited by the user", encoding="utf-8")
    with pytest.raises(UndoError, match="changed since AutoShelf made it"):
        undo(conn, created.history_id)
    assert created.dst.exists()
    undo(conn, created.history_id, force=True)
    assert (root / TRASH_DIRNAME / "report.zip").exists()


def test_undo_last_includes_new_files(conn, root: Path, made: Path) -> None:
    source = make(root / "report.pdf")
    created = place_new_file(
        conn, made, root, "report.zip", root=root, operation="zip", sources=[source]
    )
    (result,) = undo_last(conn, 1)
    assert result.src == created.dst


# --- a crash while making a file --------------------------------------------------------------


def pending_create(conn, root: Path, source: Path, dst: Path, digest: str) -> int:
    return repository.insert_history(
        conn,
        action="create",
        src=source,
        dst=dst,
        reason="zip",
        root=root,
        sha256=digest,
        details={"reason": {"code": "created", "operation": "zip", "sources": [str(source)]}},
    )


def test_a_new_file_made_before_a_crash_is_recorded(conn, root: Path) -> None:
    source = make(root / "a.txt")
    dst = make(root / "a.zip", "complete")
    history_id = pending_create(conn, root, source, dst, sha256_file(dst))

    (settled,) = reconcile_pending(conn)

    assert settled.state == "done"
    entry = repository.get_history(conn, history_id)
    assert entry.state == "done"
    assert repository.get_file(conn, entry.file_id).status == "ignored"


def test_an_incomplete_or_missing_new_file_is_failed_but_kept(conn, root: Path) -> None:
    source = make(root / "a.txt")
    partial = make(root / "a.zip", "half")
    first = pending_create(conn, root, source, partial, "0" * 64)
    second = pending_create(conn, root, source, root / "b.zip", "0" * 64)

    reconcile_pending(conn)

    assert repository.get_history(conn, first).state == "failed"
    assert "incomplete" in repository.get_history(conn, first).details["note"]
    assert partial.exists()  # never deleted, even AutoShelf's own unfinished copy
    assert repository.get_history(conn, second).state == "failed"


# --- the history table (migration V6) -----------------------------------------------------------


def test_upgrade_to_v6_keeps_history_and_its_references(paths) -> None:
    conn = connect(paths.db_path)
    script = "".join(schema_v for schema_v in (schema.V1, schema.V2, schema.V3, schema.V4))
    conn.executescript(f"BEGIN;{script}{schema.V5}PRAGMA user_version = 5; COMMIT;")
    conn.execute(
        "INSERT INTO files (id, path, status, added_at) VALUES (1, 'C:/x/a.pdf', 'sorted', 't')"
    )
    conn.execute(
        "INSERT INTO history (id, ts, file_id, action, src, dst, reason, state, details_json)"
        " VALUES (1, 't', 1, 'move', 'a', 'b', 'auto', 'undone', '{\"choice\": \"B\"}')"
    )
    conn.execute(
        "INSERT INTO history (id, ts, file_id, action, src, dst, reason, state, undo_of)"
        " VALUES (2, 't', 1, 'undo', 'b', 'a', 'undo #1', 'done', 1)"
    )
    conn.execute(
        "INSERT INTO corrections (file_id, from_folder, to_folder, source, ts, history_id)"
        " VALUES (1, 'B', 'C', 'undo', 't', 1)"
    )
    conn.close()

    conn = open_database(paths.db_path)
    try:
        assert current_version(conn) == len(MIGRATIONS) >= 6
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        rows = conn.execute("SELECT id, action, undo_of, details_json FROM history").fetchall()
        assert [tuple(row) for row in rows] == [
            (1, "move", None, '{"choice": "B"}'),
            (2, "undo", 1, "{}"),
        ]
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(history)")}
        assert {"history_state", "history_batch", "history_file"} <= indexes
        repository.insert_history(conn, action="create", src="a", dst="c", reason="zip", root="r")
        with pytest.raises(sqlite3.IntegrityError):
            repository.insert_history(conn, action="delete", src="a", dst="c", reason="", root="r")
        assert repository.list_corrections(conn)[0]["history_id"] == 1
    finally:
        conn.close()


def test_the_why_of_a_new_file_reads_well(conn, root: Path, made: Path) -> None:
    source = make(root / "scan.png")
    created = place_new_file(
        conn,
        made,
        root,
        "scan.pdf",
        root=root,
        operation="convert",
        sources=[source],
        extra={"to": "pdf"},
    )
    entry = repository.get_history(conn, created.history_id)
    assert wording.decision_lines(entry.details, root) == [
        "AutoShelf made a PDF version of scan.png"
    ]
    assert wording.history_reason(entry) == "Made a PDF version of scan.png"
