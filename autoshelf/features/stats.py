"""Library statistics for the dashboard (SPEC section 5.12)."""

import os
import sqlite3
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autoshelf.actions.inbox import waiting_files
from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.ml.library import scan_library

STALE_DAYS = 182  # "untouched for 6+ months"
MONTHS_SHOWN = 12
STALE_SHOWN = 50
STALE_NOTE = (
    "Not changed, added or opened from AutoShelf for 6 months or more. Windows doesn't "
    "reliably record when a file is opened, so files you opened in other programs can be "
    "listed too."
)


@dataclass(frozen=True)
class StaleFile:
    """A file that hasn't been changed, added or opened from AutoShelf for a long time."""

    path: Path
    last_used: datetime


@dataclass(frozen=True)
class LibraryStats:
    """Numbers about the library and how well sorting goes."""

    total_files: int
    files_per_folder: list[tuple[str, int]]  # files directly in each folder, biggest first
    added_per_month: list[tuple[str, int]]  # "2026-03", oldest first, the last 12 months
    inbox: int
    auto_moves: int
    auto_corrected: int  # automatic moves the user undid or moved elsewhere
    rule_moves: int
    corrected_pairs: list[tuple[str, str, int]]  # (from, to, count), most frequent first
    stale: list[StaleFile]  # oldest first
    stale_total: int

    @property
    def accuracy(self) -> float | None:
        """Share of automatic moves the user kept (None before the first one)."""
        if not self.auto_moves:
            return None
        return 1 - self.auto_corrected / self.auto_moves


def _created(st: os.stat_result) -> float:
    """When a file was created (added to this folder or copied here)."""
    return getattr(st, "st_birthtime", None) or st.st_ctime


def _months(now: datetime) -> list[str]:
    months = []
    year, month = now.year, now.month
    for _ in range(MONTHS_SHOWN):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(months))


def library_stats(
    conn: sqlite3.Connection,
    root: Path,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    *,
    now: datetime | None = None,
) -> LibraryStats:
    """Collect the dashboard's numbers (reads file metadata only, never contents).

    "Last used" is the latest of: when the file was changed, when it was created (put in
    its place), and when it was last opened from AutoShelf. Windows' last-access time isn't
    used: Windows often doesn't update it, and backups or virus scans touch it.
    """
    now = now or datetime.now(UTC)
    patterns = tuple(ignore_patterns)
    scan = scan_library(root, patterns)
    per_folder = Counter({key: 0 for key in scan.folders})
    added: Counter[str] = Counter()
    opened = repository.last_opened(conn)
    cutoff = now - timedelta(days=STALE_DAYS)
    stale: list[StaleFile] = []
    for item in scan.files:
        per_folder[item.folder] += 1
        try:
            st = item.path.stat()
        except OSError:
            continue
        created = datetime.fromtimestamp(_created(st), UTC)
        added[f"{created.year:04d}-{created.month:02d}"] += 1
        last_used = datetime.fromtimestamp(max(st.st_mtime, _created(st)), UTC)
        if str(item.path).casefold() in opened:
            last_used = max(last_used, datetime.fromisoformat(opened[str(item.path).casefold()]))
        if last_used < cutoff:
            stale.append(StaleFile(item.path, last_used))
    stale.sort(key=lambda item: item.last_used)
    counts = repository.decision_counts(conn)
    return LibraryStats(
        total_files=len(scan.files),
        files_per_folder=sorted(per_folder.items(), key=lambda pair: (-pair[1], pair[0])),
        added_per_month=[(month, added.get(month, 0)) for month in _months(now)],
        inbox=len(waiting_files(root, patterns)),
        auto_moves=counts["auto"],
        auto_corrected=counts["auto_corrected"],
        rule_moves=counts["rule"],
        corrected_pairs=repository.corrected_pairs(conn),
        stale=stale[:STALE_SHOWN],
        stale_total=len(stale),
    )
