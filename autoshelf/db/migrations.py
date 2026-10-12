"""Schema migrations tracked with ``PRAGMA user_version``."""

import sqlite3

from autoshelf.db import schema

# Index i holds the script that upgrades the database from version i to i + 1.
MIGRATIONS: tuple[str, ...] = (schema.V1,)


class MigrationError(Exception):
    """Raised when the database cannot be brought to the current schema."""


def current_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in the database file."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations, each in its own transaction, and return the new version."""
    version = current_version(conn)
    if version > len(MIGRATIONS):
        raise MigrationError(
            f"The database is schema version {version}, but this AutoShelf only knows "
            f"up to {len(MIGRATIONS)}. Update AutoShelf."
        )
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        try:
            conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration to version {number} failed: {exc}") from exc
    return len(MIGRATIONS)
