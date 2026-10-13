"""The ingestion pipeline (SPEC section 4.1) for one file that has become stable.

Steps: check location and ignore rules, hash, look for exact duplicates, extract text,
check the user's rules, classify, act. A matching rule or a clear match moves the file
straight into its folder. Anything else goes to ``_Inbox`` (or, if it is already there,
stays) for the user to decide: "when unsure, ask". Every move is stored in history
with why it happened (``ml.explain``).
"""

import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import numpy as np

from autoshelf.actions import rules as rule_engine
from autoshelf.actions.mover import MoveError, free_destination, move_file
from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.fsutil import sha256_file
from autoshelf.layout import INBOX_DIRNAME, PROCESSED_ZONES, Zone, inbox_dir, zone_of
from autoshelf.ml import explain
from autoshelf.ml.classifier import Classification
from autoshelf.ml.library import TEXT_EXCERPT_CHARS, LibraryIndex, store_content, store_vector
from autoshelf.watcher.ignore import ignore_reason
from autoshelf.watcher.selfmoves import SelfMoveRegistry

__all__ = ["TEXT_EXCERPT_CHARS", "Decision", "Outcome", "OutcomeKind", "Pipeline"]

log = logging.getLogger(__name__)

REASON_AUTO = "auto"  # moved by the classifier
REASON_ASK = "ask"  # sent to _Inbox for the user to decide


class OutcomeKind(StrEnum):
    MOVED = "moved"
    WOULD_MOVE = "would move"
    WAITING = "waiting"  # left in _Inbox for the user to decide
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
    classification: Classification | None = None
    details: dict | None = None  # why: what is stored with the move in history

    @property
    def needs_decision(self) -> bool:
        """True if the file now waits in _Inbox for the user to choose a folder."""
        if self.kind is OutcomeKind.WAITING:
            return True
        return (
            self.kind is OutcomeKind.MOVED
            and self.dst is not None
            and self.dst.parent.name.casefold() == INBOX_DIRNAME.casefold()
        )


@dataclass(frozen=True)
class Decision:
    """Where a file should go. ``target_dir`` None means it stays where it is (in _Inbox).

    ``suggestion`` is the folder AutoShelf chose or, when asking, thinks most likely.
    """

    target_dir: Path | None
    reason: str
    message: str
    confidence: float | None = None
    suggestion: str | None = None


class Pipeline:
    """Processes stable files from the root and ``_Inbox``. Use from a single thread.

    Without a ``library`` (e.g. the language model couldn't be loaded) every file waits
    in ``_Inbox`` for the user.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        *,
        dry_run: bool = False,
        ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
        self_moves: SelfMoveRegistry | None = None,
        extractor: Callable[[Path], Extraction] = extract,
        library: LibraryIndex | None = None,
        threshold: float = DEFAULT_AUTO_THRESHOLD,
        margin: float = DEFAULT_AUTO_MARGIN,
    ) -> None:
        self.conn = conn
        self.root = root
        self.dry_run = dry_run
        self.ignore_patterns = tuple(ignore_patterns)
        self.self_moves = self_moves or SelfMoveRegistry()
        self.extractor = extractor
        self.library = library
        self.threshold = threshold
        self.margin = margin

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
        st = path.stat()
        known = repository.get_file_by_path(self.conn, path)
        if known is not None and known.sha256 == digest:
            if known.status == "ignored":
                return Outcome(OutcomeKind.SKIPPED, path, "left in place: you undid its last move")
            if zone is Zone.INBOX and known.status == "inbox":
                return Outcome(OutcomeKind.SKIPPED, path, "waiting in _Inbox for your decision")
        duplicates = self._duplicates_of(path, digest)
        extraction = self.extractor(path)
        vector, classification = self._classify(path, extraction)
        rule = rule_engine.first_match(
            repository.list_rules(self.conn, enabled_only=True), path, extraction
        )
        if rule is not None:
            decision = self._rule_decision(zone, rule)
        else:
            decision = self._decide(zone, classification)
        if decision.target_dir is not None and not self._can_receive(decision.target_dir):
            decision = self._fallback_to_inbox(zone, decision)
        details = self._details(path, extraction, classification, rule)

        def outcome(kind: OutcomeKind, message: str, **extra) -> Outcome:
            return Outcome(
                kind,
                path,
                message,
                duplicates=duplicates,
                extraction=extraction,
                classification=classification,
                details=details,
                **extra,
            )

        if decision.target_dir is None:
            if not self.dry_run:
                self._record_waiting(path, digest, st, extraction, vector, decision.suggestion)
            return outcome(OutcomeKind.WAITING, decision.message)
        if self.dry_run:
            dst = self._planned_destination(decision.target_dir, path.name)
            return outcome(OutcomeKind.WOULD_MOVE, decision.message, dst=dst)

        self._ensure_inbox_if_target(decision.target_dir)
        result = move_file(
            self.conn,
            path,
            decision.target_dir,
            root=self.root,
            reason=decision.reason,
            confidence=decision.confidence,
            sha256=digest,
            details=details,
            before_rename=self._expect_self_move,
        )
        repository.set_suggestion(self.conn, result.file_id, decision.suggestion)
        store_content(self.conn, result.file_id, extraction)
        if vector is not None:
            self._store_vector(result.file_id, vector, st)
        return outcome(
            OutcomeKind.MOVED, decision.message, dst=result.dst, history_id=result.history_id
        )

    def _classify(
        self, path: Path, extraction: Extraction
    ) -> tuple[np.ndarray | None, Classification | None]:
        """Embed the file and score the folders. Returns (None, None) without a library."""
        if self.library is None:
            return None, None
        try:
            self.library.refresh()  # pick up folders and files the user added meanwhile
            vector = self.library.vectorize(extraction, path.name)
            classification = self.library.classify(
                vector, threshold=self.threshold, margin=self.margin
            )
        except Exception:
            log.exception("Couldn't classify %s; it will wait in _Inbox", path)
            return None, None
        return vector, classification

    def _decide(self, zone: Zone, classification: Classification | None) -> Decision:
        if classification is None:
            why = "it can't be sorted by content right now"
        elif classification.choice is not None:
            choice = classification.choice
            target = self.root.joinpath(*choice.folder.split("/"))
            return Decision(
                target, REASON_AUTO, classification.explanation, choice.score, choice.folder
            )
        else:
            why = classification.explanation
        best = classification.scores[0] if classification and classification.scores else None
        return self._ask(zone, why, best.score if best else None, best.folder if best else None)

    def _ask(
        self, zone: Zone, why: str, score: float | None = None, suggestion: str | None = None
    ) -> Decision:
        """Leave the file in (or send it to) _Inbox for the user to decide."""
        if zone is Zone.INBOX:
            return Decision(None, REASON_ASK, f"waiting in _Inbox: {why}", score, suggestion)
        return Decision(inbox_dir(self.root), REASON_ASK, why, score, suggestion)

    def _rule_decision(self, zone: Zone, match: rule_engine.RuleMatch) -> Decision:
        """A rule decides, overriding the classifier; it never creates its folder."""
        rule = match.rule
        target = self.root.joinpath(*rule.target_folder.split("/"))
        if not target.is_dir() or zone_of(target / "x", self.root) is not Zone.CATEGORY:
            why = f"rule {rule.name!r} matched, but its folder {rule.target_folder!r} doesn't exist"
            return self._ask(zone, why)
        message = f"rule {rule.name!r} matched: {match.match}"
        return Decision(
            target, rule_engine.reason_for(rule), message, suggestion=rule.target_folder
        )

    def _details(
        self,
        path: Path,
        extraction: Extraction,
        classification: Classification | None,
        rule: rule_engine.RuleMatch | None,
    ) -> dict | None:
        """Why the file goes where it goes, as stored with its move in history."""
        details: dict = {}
        if classification is not None:
            try:
                details = self.library.explain(classification, extraction, path.name)
            except Exception:
                log.exception("Couldn't explain the decision for %s", path)
                details = classification.to_details()
        if rule is not None:
            details.update(
                explain.rule_details(
                    rule.rule.name,
                    rule_engine.describe_condition(rule.rule.kind, rule.rule.pattern),
                    rule.rule.target_folder,
                    rule.match,
                )
            )
        return details or None

    def _can_receive(self, target_dir: Path) -> bool:
        """True if files can be moved to ``target_dir`` (``_Inbox`` is created when needed)."""
        return target_dir == inbox_dir(self.root) or target_dir.is_dir()

    def _fallback_to_inbox(self, zone: Zone, decision: Decision) -> Decision:
        """The chosen folder vanished after scoring: ask instead. Folders are never created."""
        return self._ask(zone, f"its folder {decision.target_dir.name!r} no longer exists")

    def _record_waiting(
        self,
        path: Path,
        digest: str,
        st: os.stat_result,
        extraction: Extraction,
        vector: np.ndarray | None,
        suggestion: str | None,
    ) -> None:
        """Remember a file that stays in _Inbox, so it isn't classified again and again."""
        file_id = repository.upsert_file(
            self.conn, path, sha256=digest, size=st.st_size, mtime=st.st_mtime
        )
        repository.set_file_status(self.conn, file_id, "inbox")
        repository.set_suggestion(self.conn, file_id, suggestion)
        store_content(self.conn, file_id, extraction)
        if vector is not None:
            self._store_vector(file_id, vector, st)

    def _store_vector(self, file_id: int, vector: np.ndarray, st: os.stat_result) -> None:
        # A rename keeps size and mtime, so the vector stays valid at the new path.
        store_vector(
            self.conn, file_id, vector, model=self.library.model, size=st.st_size, mtime=st.st_mtime
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
