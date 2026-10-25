"""How long AutoShelf will take to get ready and sort what is waiting, before it starts,
and how long is left while it works.

The estimate looks at names and sizes only: it reads no file, so iCloud downloads nothing.
Files already in your folders are read once, to learn what each folder holds (iCloud
"online-only" ones by their names only); files AutoShelf already knows, unchanged, are
skipped. The per-file costs come from measured runs: names only, about 4,000 files in
100 seconds on a laptop; a file read for its text costs several times more.
"""

import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.ml.embedder import is_downloaded
from autoshelf.ml.library import scan_library

MODEL_BYTES = 470_000_000  # the default model's download
NAME_ONLY_SECONDS = 0.025  # an online-only file: its name is all that is read
READ_SECONDS = 0.12  # a file read for its text (up to 20,000 characters)...
READ_SECONDS_PER_MB = 0.05  # ...plus a little for bigger files
READ_SECONDS_MAX = 2.0
SORT_SECONDS = 0.3  # one new file: read, compare with your folders, move
LOAD_SECONDS = 6.0  # starting the language model once it is on this PC
DOWNLOAD_BYTES_PER_SECOND = (2_000_000, 20_000_000)  # a slow and a fast connection
SPREAD = (0.7, 1.5)  # the range shown around the estimate
MTIME_TOLERANCE = 2.0  # seconds; iCloud and FAT round modification times


@dataclass(frozen=True)
class Estimate:
    """What AutoShelf has to do before everything is sorted, and how long it may take."""

    library_files: int  # files already in your folders
    name_only: int  # of those, online-only: read by name only
    to_read: int  # of those, not yet known to AutoShelf (read once now)
    to_sort: int  # new files waiting in the root and the Inbox
    download_bytes: int  # the language model, if it still has to be downloaded
    low_seconds: float
    high_seconds: float
    reading_seconds: float = 0.0  # expected time to read the files in your folders
    sorting_seconds: float = 0.0  # expected time to sort the new files

    @property
    def seconds(self) -> float:
        """The middle of the range."""
        return (self.low_seconds + self.high_seconds) / 2


def read_seconds(size: int, online_only: bool) -> float:
    """The cost of indexing one file already in your folders."""
    if online_only:
        return NAME_ONLY_SECONDS
    return min(READ_SECONDS_MAX, READ_SECONDS + READ_SECONDS_PER_MB * size / 1_000_000)


def estimate(
    root: Path,
    model: str,
    model_dir: Path,
    *,
    loose_files: int,
    conn: sqlite3.Connection | None = None,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
) -> Estimate:
    """Estimate getting ready in ``root`` and sorting ``loose_files`` new files."""
    scan = scan_library(root, ignore_patterns)
    known = _known_files(conn) if conn is not None else {}
    reading = 0.0
    to_read = 0
    for item in scan.files:
        stored = known.get(str(item.path).casefold())
        if stored is not None and stored[0] == item.size and _same_time(stored[1], item.mtime):
            continue
        to_read += 1
        reading += read_seconds(item.size, item.online_only)
    sorting = loose_files * SORT_SECONDS
    work = reading + sorting
    download = 0 if is_downloaded(model, model_dir) else MODEL_BYTES
    slow, fast = DOWNLOAD_BYTES_PER_SECOND
    low = (work + LOAD_SECONDS) * SPREAD[0] + download / fast
    high = (work + LOAD_SECONDS) * SPREAD[1] + download / slow
    return Estimate(
        library_files=len(scan.files),
        name_only=sum(1 for item in scan.files if item.online_only),
        to_read=to_read,
        to_sort=loose_files,
        download_bytes=download,
        low_seconds=low,
        high_seconds=high,
        reading_seconds=reading,
        sorting_seconds=sorting,
    )


def _known_files(conn: sqlite3.Connection) -> dict[str, tuple[int | None, float | None]]:
    rows = conn.execute("SELECT path, size, mtime FROM files").fetchall()
    return {str(row["path"]).casefold(): (row["size"], row["mtime"]) for row in rows}


def _same_time(stored: float | None, actual: float) -> bool:
    return stored is not None and abs(stored - actual) <= MTIME_TOLERANCE


class Eta:
    """Time left for ``total`` steps, from the speed seen so far.

    Until a few steps have been timed it falls back on ``expected`` seconds for the whole
    job (the estimate); after that the measured speed, smoothed, takes over.
    """

    SMOOTHING = 0.3  # weight of the newest speed measurement
    MIN_SECONDS = 2.0  # time to watch before trusting the measured speed

    def __init__(self, total: float, expected: float | None = None, now: float | None = None):
        self.total = total
        self.expected = expected
        self.started = time.monotonic() if now is None else now
        self._last: tuple[float, float] = (self.started, 0.0)
        self._rate: float | None = None  # steps per second
        self.done = 0.0

    def update(self, done: float, now: float | None = None) -> float | None:
        """Record progress; returns the seconds left (None if there is no way to tell)."""
        now = time.monotonic() if now is None else now
        last_time, last_done = self._last
        if now > last_time and done > last_done:
            rate = (done - last_done) / (now - last_time)
            self._rate = (
                rate
                if self._rate is None
                else (self.SMOOTHING * rate + (1 - self.SMOOTHING) * self._rate)
            )
            self._last = (now, done)
        self.done = done
        return self.left(now)

    def left(self, now: float | None = None) -> float | None:
        """Seconds left, as of the last update."""
        now = time.monotonic() if now is None else now
        remaining = max(0.0, self.total - self.done)
        if remaining == 0:
            return 0.0
        trusted = self._rate is not None and now - self.started >= self.MIN_SECONDS
        if trusted and self._rate:
            return remaining / self._rate
        if self.expected is not None and self.total:
            return self.expected * remaining / self.total
        return None
