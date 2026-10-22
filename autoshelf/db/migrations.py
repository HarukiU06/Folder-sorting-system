"""Schema migrations tracked with ``PRAGMA user_version``."""

import sqlite3
from dataclasses import dataclass

from autoshelf.db import schema


@dataclass(frozen=True)
class Rebuild:
    """A migration that rebuilds tables other tables refer to (to change a constraint).

    It runs with foreign keys off, as SQLite requires for dropping and renaming such a
    table, and is only committed if ``PRAGMA foreign_key_check`` finds nothing wrong.
    """

    script: str


# Index i holds the migration that upgrades the database from version i to i + 1.
MIGRATIONS: tuple[str | Rebuild, ...] = (
    schema.V1,
    schema.V2,
    schema.V3,
    schema.V4,
    schema.V5,
    Rebuild(schema.V6),
)


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
    for number, migration in enumerate(MIGRATIONS[version:], start=version + 1):
        if isinstance(migration, Rebuild):
            _rebuild(conn, number, migration.script)
            continue
        try:
            conn.executescript(f"BEGIN;\n{migration}\nPRAGMA user_version = {number};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration to version {number} failed: {exc}") from exc
    return len(MIGRATIONS)


def _rebuild(conn: sqlite3.Connection, number: int, script: str) -> None:
    # PRAGMA foreign_keys does nothing inside a transaction, so it is switched around it.
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};")
        problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            conn.execute("ROLLBACK")
            raise MigrationError(
                f"Migration to version {number} would break {len(problems)} reference(s)"
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise MigrationError(f"Migration to version {number} failed: {exc}") from exc
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
