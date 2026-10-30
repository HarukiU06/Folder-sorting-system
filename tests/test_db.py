import sqlite3

import pytest

from autoshelf.db import repository, schema
from autoshelf.db.connection import connect, open_database, transaction
from autoshelf.db.migrations import MIGRATIONS, MigrationError, current_version, migrate
from autoshelf.db.repository import get_setting, now_iso, set_setting

EXPECTED_TABLES = {
    "files",
    "chunks",
    "chunks_fts",
    "folders",
    "tags",
    "file_tags",
    "rules",
    "history",
    "corrections",
    "settings",
    "file_vectors",
}


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def test_migrate_creates_all_tables(conn) -> None:
    assert _tables(conn) >= EXPECTED_TABLES
    assert current_version(conn) == len(MIGRATIONS)


def test_migrate_is_idempotent(paths) -> None:
    open_database(paths.db_path).close()
    conn = open_database(paths.db_path)
    assert migrate(conn) == len(MIGRATIONS)
    conn.close()


def test_database_newer_than_app_is_refused(paths) -> None:
    conn = connect(paths.db_path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    conn.close()
    with pytest.raises(MigrationError, match="only knows"):
        open_database(paths.db_path)


def test_wal_and_foreign_keys_enabled(conn) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fts_index_follows_chunks(conn) -> None:
    conn.execute(
        "INSERT INTO files (path, added_at) VALUES (?, ?)", (r"C:\shelf\notes.txt", now_iso())
    )
    conn.execute("INSERT INTO chunks (file_id, idx, text) VALUES (1, 0, 'k-means initialization')")
    match = "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?"
    assert conn.execute(match, ("initialization",)).fetchall() != []
    conn.execute("DELETE FROM chunks")
    assert conn.execute(match, ("initialization",)).fetchall() == []


def test_history_state_is_constrained(conn) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO history (ts, action, state) VALUES (?, 'move', 'bogus')", (now_iso(),)
        )


def test_transaction_rolls_back_on_error(conn) -> None:
    with pytest.raises(RuntimeError), transaction(conn):
        set_setting(conn, "a", 1)
        raise RuntimeError("boom")
    assert get_setting(conn, "a") is None


def test_settings_values_are_json(conn) -> None:
    set_setting(conn, "patterns", ["*.tmp", "~$*"])
    set_setting(conn, "patterns", ["*.part"])
    assert get_setting(conn, "patterns") == ["*.part"]
    assert get_setting(conn, "missing", default=3) == 3


def test_upgrade_from_version_1_keeps_data(paths) -> None:
    conn = connect(paths.db_path)
    conn.executescript("".join(["BEGIN;", schema.V1, "PRAGMA user_version = 1; COMMIT;"]))
    set_setting(conn, "root", "C:/shelf")
    conn.close()

    conn = open_database(paths.db_path)
    assert current_version(conn) == len(MIGRATIONS)
    assert "file_vectors" in _tables(conn)
    assert get_setting(conn, "root") == "C:/shelf"
    conn.close()


def test_vectors_are_stored_per_file_and_deleted_with_it(conn) -> None:
    file_id = repository.upsert_file(conn, r"C:\shelf\a.pdf", sha256="x", size=1, mtime=2.0)
    repository.set_vector(
        conn, file_id, model="m1", size=1, mtime=2.0, source="content", vector=b"1234"
    )
    repository.set_vector(
        conn, file_id, model="m2", size=1, mtime=2.0, source="name", vector=b"5678"
    )

    stored = repository.get_vector(conn, file_id)
    assert (stored.model, stored.source, stored.vector) == ("m2", "name", b"5678")

    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    assert repository.get_vector(conn, file_id) is None


def test_folder_descriptions_are_case_insensitive(conn) -> None:
    repository.set_folder_description(conn, "Machine Learning/NLP", "text models")
    repository.set_folder_description(conn, "machine learning/nlp", "language models")
    repository.set_folder_description(conn, "Empty", "")
    assert repository.folder_descriptions(conn) == {"machine learning/nlp": "language models"}


def test_top_suggestion_comes_from_the_latest_decision(conn) -> None:
    file_id = repository.upsert_file(conn, r"C:\shelf\a.pdf", sha256="x", size=1, mtime=2.0)
    assert repository.top_suggestion(conn, file_id) is None
    for folder in ("Finance", "Recipes"):
        repository.insert_history(
            conn,
            action="move",
            src="a",
            dst="b",
            reason="ask",
            root="C:/shelf",
            file_id=file_id,
            details={"candidates": [{"folder": folder, "score": 0.3}]},
        )
    repository.insert_history(
        conn, action="move", src="b", dst="c", reason="user", root="C:/shelf", file_id=file_id
    )
    assert repository.top_suggestion(conn, file_id) == "Recipes"


def test_nested_transactions_roll_back_only_the_inner_part(conn) -> None:
    with transaction(conn):
        repository.set_setting(conn, "outer", 1)
        with pytest.raises(LookupError), transaction(conn):
            repository.set_setting(conn, "inner", 2)
            raise LookupError("inner step failed")
    assert repository.get_setting(conn, "outer") == 1
    assert repository.get_setting(conn, "inner") is None


# --- folder changes (migration V8) ------------------------------------------------------


def test_migration_v8_allows_folder_rows(paths) -> None:
    conn = connect(paths.db_path)
    for number, migration in enumerate(MIGRATIONS[:7], start=1):
        script = getattr(migration, "script", migration)
        conn.executescript(f"BEGIN;{script}PRAGMA user_version = {number}; COMMIT;")
    conn.execute(
        "INSERT INTO history (id, ts, action, src, dst, reason, state)"
        " VALUES (7, 't', 'move', 'a', 'b', 'auto', 'done')"
    )
    conn.close()

    conn = open_database(paths.db_path)
    try:
        assert current_version(conn) == len(MIGRATIONS) >= 8
        assert [tuple(row) for row in conn.execute("SELECT id, action FROM history")] == [
            (7, "move")
        ]
        repository.insert_history(conn, action="folder", src="A", dst="B", reason="user", root="r")
        assert "folder" in repository.UNDOABLE_ACTIONS
    finally:
        conn.close()


def _file(conn, path: str) -> int:
    return repository.upsert_file(conn, path, sha256=None, size=1, mtime=1.0)


def test_follow_folder_moves_paths_and_keys(conn, tmp_path) -> None:
    root = tmp_path / "shelf"
    inside = _file(conn, root / "A_1" / "x.pdf")
    deeper = _file(conn, root / "A_1" / "Sub" / "y.pdf")
    near = _file(conn, root / "A_10" / "z.pdf")
    odd = _file(conn, root / "A%1" / "w.pdf")
    repository.set_suggestion(conn, inside, "a_1/Sub")
    repository.set_folder_description(conn, "A_1", "maths")
    repository.set_folder_description(conn, "A_10", "other")
    repository.record_correction(
        conn, file_id=inside, from_folder="Old", to_folder="A_1/Sub", source="external"
    )
    rule = repository.insert_rule(conn, name="r", target_folder="A_1", kind="ext", pattern=".pdf")
    repository.add_flashcards(conn, [("q", "a")], file_id=None, folder="A_1/Sub")
    version = repository.library_version(conn)

    changed = repository.follow_folder(
        conn, root / "A_1", root / "B 2", old_key="A_1", new_key="B 2", status=None
    )

    assert changed == 2
    assert repository.get_file(conn, inside).path == str(root / "B 2" / "x.pdf")
    assert repository.get_file(conn, deeper).path == str(root / "B 2" / "Sub" / "y.pdf")
    assert repository.get_file(conn, near).path == str(root / "A_10" / "z.pdf")
    assert repository.get_file(conn, odd).path == str(root / "A%1" / "w.pdf")
    assert repository.get_file(conn, inside).suggestion == "B 2/Sub"
    assert repository.folder_descriptions(conn) == {"b 2": "maths", "a_10": "other"}
    assert repository.list_corrections(conn)[0]["to_folder"] == "B 2/Sub"
    assert repository.get_rule(conn, rule).target_folder == "B 2"
    assert repository.list_flashcards(conn)[0].folder == "B 2/Sub"
    assert repository.library_version(conn) > version
    # Doing it again changes nothing (the watcher may see the same rename).
    assert (
        repository.follow_folder(
            conn, root / "A_1", root / "B 2", old_key="A_1", new_key="B 2", status=None
        )
        == 0
    )


def test_follow_folder_for_trash_changes_paths_only(conn, tmp_path) -> None:
    root = tmp_path / "shelf"
    inside = _file(conn, root / "Notes" / "x.pdf")
    repository.set_folder_description(conn, "Notes", "lecture notes")

    repository.follow_folder(
        conn,
        root / "Notes",
        root / "_Trash" / "Notes",
        old_key=None,
        new_key=None,
        status="ignored",
    )

    record = repository.get_file(conn, inside)
    assert record.path == str(root / "_Trash" / "Notes" / "x.pdf")
    assert record.status == "ignored"
    assert repository.folder_descriptions(conn) == {"notes": "lecture notes"}


def test_known_folders_round_trip(conn) -> None:
    assert repository.known_folders(conn) == set()
    repository.set_folder_description(conn, "Finance", "money")
    repository.add_known_folders(conn, ["Finance", "Ünïcode notes", "Ünïcode notes"])
    assert repository.known_folders(conn) == {"finance", "ünïcode notes"}
    assert repository.folder_descriptions(conn) == {"finance": "money"}
