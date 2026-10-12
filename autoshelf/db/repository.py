"""Repository functions: all SQL used by the rest of the app lives here."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any


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
