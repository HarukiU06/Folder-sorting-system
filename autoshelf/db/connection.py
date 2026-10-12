"""SQLite connections. Each thread opens its own connection."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from autoshelf.db.migrations import migrate

BUSY_TIMEOUT_SECONDS = 10.0


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection in autocommit mode, with WAL and foreign keys enabled.

    Autocommit means every statement is durable as soon as it returns, which is what
    the "write history before moving" rule relies on. Use ``transaction()`` to group
    statements.
    """
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def open_database(db_path: Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    conn = connect(db_path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run the enclosed statements atomically."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
