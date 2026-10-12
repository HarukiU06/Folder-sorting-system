"""The ingestion pipeline (SPEC section 4.1) for one file that has become stable.

Steps: check location and ignore rules, hash, look for exact duplicates, extract text,
decide, act. Rules (Phase 5) and classification (Phase 3) slot in before ``decide``.
Until the classifier exists, every dropped file is sent to ``_Inbox`` for the user to
decide, which is the spec's "when unsure, ask" path.
"""

import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from autoshelf.actions.mover import MoveError, free_destination, move_file
from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.fsutil import sha256_file
from autoshelf.layout import PROCESSED_ZONES, Zone, inbox_dir, zone_of
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.selfmoves import SelfMoveRegistry

log = logging.getLogger(__name__)

# How much extracted text is stored with each file for previews (the Inbox, Phase 4).
TEXT_EXCERPT_CHARS = 1000


class OutcomeKind(StrEnum):
    MOVED = "moved"
    WOULD_MOVE = "would move"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class Outcome:
    """What the pipeline did (or, in a dry run, would do) with one file."""

    kind: OutcomeKind
    path: Path
    message: str
    dst: Path | None = None
    history_id: int | None = None
    duplicates: tuple[Path, ...] = ()
    extraction: Extraction | None = None


@dataclass(frozen=True)
class Decision:
    """Where a file should go. ``target_dir`` None means leave it where it is."""

    target_dir: Path | None
    reason: str
    confidence: float | None = None


def decide(zone: Zone, root: Path) -> Decision:
    """Phase 1 stand-in for the classifier: every dropped file needs the user's decision."""
    if zone is Zone.INBOX:
        return Decision(None, "waiting in _Inbox for your decision")
    return Decision(inbox_dir(root), "no classifier yet, so it waits in _Inbox for you")


class Pipeline:
    """Processes stable files from the root and ``_Inbox``. Use from a single thread."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        *,
        dry_run: bool = False,
        ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
        self_moves: SelfMoveRegistry | None = None,
        extractor: Callable[[Path], Extraction] = extract,
    ) -> None:
        self.conn = conn
        self.root = root
        self.dry_run = dry_run
        self.ignore_patterns = tuple(ignore_patterns)
        self.self_moves = self_moves or SelfMoveRegistry()
        self.extractor = extractor

    def process(self, path: Path) -> Outcome:
        """Run the pipeline for one file. Never raises; failures come back as FAILED."""
        try:
            return self._process(path)
        except MoveError as exc:
            return Outcome(OutcomeKind.FAILED, path, str(exc))
        except OSError as exc:
            return Outcome(OutcomeKind.FAILED, path, f"couldn't read the file: {exc}")
        except Exception as exc:
            log.exception("Unexpected error while processing %s", path)
            return Outcome(OutcomeKind.FAILED, path, f"unexpected error: {exc}")

    def _process(self, path: Path) -> Outcome:
        zone = zone_of(path, self.root)
        if zone not in PROCESSED_ZONES:
            return Outcome(OutcomeKind.SKIPPED, path, f"not processed in a {zone.value} location")
        if not path.is_file():
            return Outcome(OutcomeKind.SKIPPED, path, "it no longer exists")
        reason = ignore_reason(path, self.ignore_patterns)
        if reason is not None:
            return Outcome(OutcomeKind.SKIPPED, path, f"ignored: {reason}")

        digest = sha256_file(path)
        known = repository.get_file_by_path(self.conn, path)
        if known is not None and known.status == "ignored" and known.sha256 == digest:
            return Outcome(OutcomeKind.SKIPPED, path, "left in place: you undid its last move")
        duplicates = self._duplicates_of(path, digest)
        extraction = self.extractor(path)

        decision = decide(zone, self.root)
        if decision.target_dir is None:
            return Outcome(
                OutcomeKind.SKIPPED,
                path,
                decision.reason,
                duplicates=duplicates,
                extraction=extraction,
            )
        if self.dry_run:
            return Outcome(
                OutcomeKind.WOULD_MOVE,
                path,
                decision.reason,
                dst=self._planned_destination(decision.target_dir, path.name),
                duplicates=duplicates,
                extraction=extraction,
            )

        self._ensure_inbox_if_target(decision.target_dir)
        result = move_file(
            self.conn,
            path,
            decision.target_dir,
            root=self.root,
            reason=decision.reason,
            confidence=decision.confidence,
            sha256=digest,
            before_rename=self._expect_self_move,
        )
        repository.set_file_content(
            self.conn,
            result.file_id,
            title=extraction.title,
            text_excerpt=extraction.text[:TEXT_EXCERPT_CHARS] or None,
        )
        return Outcome(
            OutcomeKind.MOVED,
            path,
            decision.reason,
            dst=result.dst,
            history_id=result.history_id,
            duplicates=duplicates,
            extraction=extraction,
        )

    def _duplicates_of(self, path: Path, digest: str) -> tuple[Path, ...]:
        """Return other existing files with identical content. They are only reported."""
        others = (
            Path(record.path) for record in repository.find_files_by_sha256(self.conn, digest)
        )
        return tuple(other for other in others if other != path and other.is_file())

    @staticmethod
    def _planned_destination(target_dir: Path, name: str) -> Path:
        if target_dir.is_dir():
            return free_destination(target_dir, name)
        return target_dir / name

    def _ensure_inbox_if_target(self, target_dir: Path) -> None:
        """Create ``_Inbox`` on first use. Category folders are never created."""
        inbox = inbox_dir(self.root)
        if target_dir == inbox and not inbox.is_dir():
            inbox.mkdir()
            log.info("Created %s for files waiting on your decision", inbox)

    def _expect_self_move(self, src: Path, dst: Path) -> None:
        # Only the destination: a new file may legitimately appear at ``src`` right away.
        self.self_moves.expect(dst)
