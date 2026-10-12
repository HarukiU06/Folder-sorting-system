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
