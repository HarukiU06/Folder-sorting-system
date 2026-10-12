"""Repository functions: all SQL used by the rest of the app lives here."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoshelf.db.connection import transaction


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string, the format stored in every ``ts`` column."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- settings ---------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Return a JSON-decoded setting, or ``default`` if it isn't stored."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Store a setting as JSON, replacing any previous value."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# --- files ------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    """A row of the ``files`` table."""

    id: int
    path: str
    sha256: str | None
    size: int | None
    mtime: float | None
    ext: str | None
    status: str
    added_at: str


def _file_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        path=row["path"],
        sha256=row["sha256"],
        size=row["size"],
        mtime=row["mtime"],
        ext=row["ext"],
        status=row["status"],
        added_at=row["added_at"],
    )


def get_file(conn: sqlite3.Connection, file_id: int) -> FileRecord | None:
    """Return a file by id."""
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return None if row is None else _file_from_row(row)


def get_file_by_path(conn: sqlite3.Connection, path: str | Path) -> FileRecord | None:
    """Return the file recorded at ``path`` (case-insensitive), if any."""
    row = conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
    return None if row is None else _file_from_row(row)


def find_files_by_sha256(conn: sqlite3.Connection, sha256: str) -> list[FileRecord]:
    """Return every recorded file with this content hash."""
    rows = conn.execute("SELECT * FROM files WHERE sha256 = ? ORDER BY id", (sha256,))
    return [_file_from_row(row) for row in rows]


def upsert_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    sha256: str | None,
    size: int | None,
    mtime: float | None,
) -> int:
    """Record (or refresh) the file at ``path`` and return its id."""
    existing = get_file_by_path(conn, path)
    if existing is not None:
        conn.execute(
            "UPDATE files SET sha256 = ?, size = ?, mtime = ? WHERE id = ?",
            (sha256, size, mtime, existing.id),
        )
        return existing.id
    cursor = conn.execute(
        "INSERT INTO files (path, sha256, size, mtime, ext, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), sha256, size, mtime, Path(path).suffix.lower(), now_iso()),
    )
    return cursor.lastrowid


def set_file_location(
    conn: sqlite3.Connection, file_id: int, path: str | Path, status: str
) -> None:
    """Point a file record at its new path and status after a move.

    A stale record still claiming ``path`` (a file that left without the app noticing)
    is dropped first. This only removes database metadata, never a file on disk.
    """
    with transaction(conn):
        conn.execute("DELETE FROM files WHERE path = ? AND id != ?", (str(path), file_id))
        conn.execute(
            "UPDATE files SET path = ?, status = ? WHERE id = ?", (str(path), status, file_id)
        )


# --- history ----------------------------------------------------------------

UNDOABLE_ACTIONS = ("move", "rename")


@dataclass(frozen=True)
class HistoryEntry:
    """A row of the ``history`` table."""

    id: int
    ts: str
    file_id: int | None
    action: str
    src: str | None
    dst: str | None
    reason: str
    confidence: float | None
    details: dict[str, Any]
    batch_id: str | None
    state: str
    root: str | None
    sha256: str | None
    undo_of: int | None


def _history_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"],
        ts=row["ts"],
        file_id=row["file_id"],
        action=row["action"],
        src=row["src"],
        dst=row["dst"],
        reason=row["reason"],
        confidence=row["confidence"],
        details=json.loads(row["details_json"]),
        batch_id=row["batch_id"],
        state=row["state"],
        root=row["root"],
        sha256=row["sha256"],
        undo_of=row["undo_of"],
    )


def insert_history(
    conn: sqlite3.Connection,
    *,
    action: str,
    src: str | Path,
    dst: str | Path,
    reason: str,
    root: str | Path,
    state: str = "pending",
    file_id: int | None = None,
    sha256: str | None = None,
    confidence: float | None = None,
    batch_id: str | None = None,
    undo_of: int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Insert a history row (``pending`` by default) and return its id.

    The connection is in autocommit mode, so the row is durable when this returns.
    """
    cursor = conn.execute(
        "INSERT INTO history (ts, file_id, action, src, dst, reason, confidence, details_json,"
        " batch_id, state, root, sha256, undo_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            file_id,
            action,
            str(src),
            str(dst),
            reason,
            confidence,
            json.dumps(details or {}),
            batch_id,
            state,
            str(root),
            sha256,
            undo_of,
        ),
    )
    return cursor.lastrowid


def update_history_dst(conn: sqlite3.Connection, history_id: int, dst: str | Path) -> None:
    """Change the planned destination of a pending row (used when a name was taken)."""
    conn.execute("UPDATE history SET dst = ? WHERE id = ?", (str(dst), history_id))


def set_history_state(
    conn: sqlite3.Connection, history_id: int, state: str, *, note: str | None = None
) -> None:
    """Set a row's state, optionally recording a note (e.g. an error) in its details."""
    if note is None:
        conn.execute("UPDATE history SET state = ? WHERE id = ?", (state, history_id))
    else:
        conn.execute(
            "UPDATE history SET state = ?, details_json = json_set(details_json, '$.note', ?)"
            " WHERE id = ?",
            (state, note, history_id),
        )


def get_history(conn: sqlite3.Connection, history_id: int) -> HistoryEntry | None:
    """Return one history row."""
    row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
    return None if row is None else _history_from_row(row)


def list_history(conn: sqlite3.Connection, limit: int = 50) -> list[HistoryEntry]:
    """Return the most recent history rows, newest first."""
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
    return [_history_from_row(row) for row in rows]


def list_history_in_state(conn: sqlite3.Connection, state: str) -> list[HistoryEntry]:
    """Return every row in ``state``, oldest first."""
    rows = conn.execute("SELECT * FROM history WHERE state = ? ORDER BY id", (state,))
    return [_history_from_row(row) for row in rows]


def last_undoable(conn: sqlite3.Connection, count: int) -> list[HistoryEntry]:
    """Return the ``count`` most recent completed moves/renames, newest first."""
    rows = conn.execute(
        "SELECT * FROM history WHERE state = 'done' AND action IN (?, ?) ORDER BY id DESC LIMIT ?",
        (*UNDOABLE_ACTIONS, count),
    )
    return [_history_from_row(row) for row in rows]


def undos_with_stale_target(conn: sqlite3.Connection) -> list[int]:
    """Return ids of rows that a completed undo reversed but that aren't marked undone yet."""
    rows = conn.execute(
        "SELECT target.id FROM history AS u JOIN history AS target ON target.id = u.undo_of"
        " WHERE u.action = 'undo' AND u.state = 'done' AND target.state = 'done'"
    )
    return [row["id"] for row in rows]
