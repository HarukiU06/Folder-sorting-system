"""The weekly digest (SPEC Phase 15): what AutoShelf did in the last seven days.

"This week: 23 sorted, 3 waiting in Inbox, 1 correction learned", counted from the
history and corrections tables and the Inbox folder. The app shows it once a week; it
isn't always running, so it checks when it starts and then every hour.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autoshelf.actions.inbox import waiting_files
from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.layout import Zone, zone_of

DAYS = 7


@dataclass(frozen=True)
class Digest:
    """One week of AutoShelf: files put into your folders (each once, not undone), files
    waiting in the Inbox now, and corrections AutoShelf learned from."""

    sorted: int
    waiting: int
    learned: int
    since: datetime

    @property
    def empty(self) -> bool:
        """True if nothing happened and nothing waits."""
        return not (self.sorted or self.waiting or self.learned)


def weekly_digest(
    conn: sqlite3.Connection,
    root: Path,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    *,
    now: datetime | None = None,
) -> Digest:
    """The digest of the seven days before ``now``."""
    since = (now or datetime.now(UTC)) - timedelta(days=DAYS)
    after = since.astimezone(UTC).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, file_id, dst FROM history h WHERE action = 'move' AND state = 'done'"
        " AND ts >= ? AND NOT EXISTS (SELECT 1 FROM history u WHERE u.undo_of = h.id"
        " AND u.state = 'done')",
        (after,),
    ).fetchall()
    files = {
        row["file_id"] if row["file_id"] is not None else -row["id"]
        for row in rows
        if row["dst"] and zone_of(Path(row["dst"]), root) is Zone.CATEGORY
    }
    learned = conn.execute("SELECT COUNT(*) FROM corrections WHERE ts >= ?", (after,)).fetchone()
    waiting = len(waiting_files(root, ignore_patterns))
    return Digest(len(files), waiting, learned[0], since)


def due(last_shown: str, now: datetime) -> bool:
    """True if the digest was last shown ``DAYS`` or more days before ``now``."""
    try:
        shown = datetime.fromisoformat(last_shown)
    except ValueError:
        return True
    if shown.tzinfo is None:
        shown = shown.replace(tzinfo=UTC)
    return now - shown >= timedelta(days=DAYS)
