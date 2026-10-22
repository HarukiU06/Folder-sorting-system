"""Organize an existing folder (SPEC section 5.7): a preview first, then one undoable batch.

Point it at the root (loose files), ``_Inbox``, one of your folders, or a folder outside
the library (e.g. an old Downloads mess, on the same drive). Every file gets a proposed
folder, from a rule or the classifier, with its confidence. Clear proposals are ticked,
unclear ones aren't. The user can untick rows or change targets, then apply them as one
batch, which History can undo as a unit. Nothing is moved by the preview.
"""

import logging
import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from autoshelf import reasons
from autoshelf.actions import learning
from autoshelf.actions import rules as rule_engine
from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.fsutil import is_within
from autoshelf.layout import INBOX_DIRNAME, RESERVED_PREFIX, Zone, zone_of
from autoshelf.ml.library import LibraryIndex
from autoshelf.reasons import Reason
from autoshelf.watcher.ignore import ignore_reason

log = logging.getLogger(__name__)

REASON_ORGANIZE = "organize"
ROOT_LABEL = "(root)"


class OrganizeError(Exception):
    """A folder can't be organized as asked."""


@dataclass
class Proposal:
    """Where one file would go. ``target`` None means it stays where it is."""

    path: Path
    current: str  # where it is now: a folder key, "(root)", "_Inbox", or the outside folder
    suggested: str | None  # where AutoShelf would put it
    target: str | None  # where it will go (the user may change it)
    confidence: float | None
    reason: str  # "auto", "Rule: <name>" or "ask" (a best guess, not ticked)
    why: Reason  # the reason as data (wording.reason_text puts it in words)
    selected: bool
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OrganizeResult:
    """What applying a preview did."""

    batch_id: str
    moved: list[MoveResult]
    failed: list[tuple[Path, str]]


def where(path: Path, root: Path) -> str:
    """Where a file is, as shown in the preview."""
    zone = zone_of(path, root)
    if zone is Zone.DROP:
        return ROOT_LABEL
    if zone is Zone.INBOX:
        return INBOX_DIRNAME
    if zone is Zone.CATEGORY:
        return "/".join(path.relative_to(root).parts[:-1])
    return str(path.parent)


def files_in(
    folder: Path,
    root: Path,
    *,
    recursive: bool = False,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
) -> list[Path]:
    """Files to organize in ``folder``: no hidden, ignored or linked files, and nothing in
    ``_``-prefixed folders inside the root."""
    patterns = tuple(ignore_patterns)
    found = []
    pending = [folder]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            log.warning("Couldn't list %s: %s", directory, exc)
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                if entry.is_symlink() or entry.is_junction():
                    continue
                if entry.is_dir():
                    if recursive and not entry.name.startswith((RESERVED_PREFIX, ".")):
                        pending.append(path)
                    continue
                if not entry.is_file() or ignore_reason(path, patterns) is not None:
                    continue
            except OSError:
                continue
            if zone_of(path, root) is not Zone.RESERVED:
                found.append(path)
    return sorted(found, key=lambda path: str(path).casefold())


def plan(
    conn: sqlite3.Connection,
    root: Path,
    library: LibraryIndex | None,
    folder: Path,
    *,
    recursive: bool = False,
    threshold: float = DEFAULT_AUTO_THRESHOLD,
    margin: float = DEFAULT_AUTO_MARGIN,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    extractor: Callable[[Path], Extraction] = extract,
    progress: Callable[[int, int], None] | None = None,
) -> list[Proposal]:
    """Propose a folder for every file in ``folder`` (reads the files, moves nothing).

    Files whose proposal is the folder they are already in are left out. Without a
    library (no language model) only rules can propose anything.
    """
    rules = repository.list_rules(conn, enabled_only=True)
    paths = files_in(folder, root, recursive=recursive, ignore_patterns=ignore_patterns)
    proposals = []
    for done, path in enumerate(paths, start=1):
        try:
            proposal = _propose(conn, root, library, path, rules, threshold, margin, extractor)
        except OSError as exc:
            log.warning("Couldn't read %s: %s", path, exc)
            proposal = None
        if proposal is not None:
            proposals.append(proposal)
        if progress is not None:
            progress(done, len(paths))
    return proposals


def _propose(
    conn: sqlite3.Connection,
    root: Path,
    library: LibraryIndex | None,
    path: Path,
    rules: list[repository.RuleRecord],
    threshold: float,
    margin: float,
    extractor: Callable[[Path], Extraction],
) -> Proposal | None:
    current = where(path, root)
    extraction = extractor(path)
    match = rule_engine.first_match(rules, path, extraction)
    classification = None
    if library is not None:
        vector = library.stored_vector(path)
        if vector is None:
            vector = library.vectorize(extraction, path.name)
        classification = library.classify(vector, threshold=threshold, margin=margin, exclude=path)
    details = classification.to_details() if classification else {}
    if match is not None:
        rule = match.rule
        suggested, confidence = rule.target_folder, None
        why = reasons.reason(reasons.RULE, rule=rule.name, matched=list(match.matched))
        reason, selected = rule_engine.reason_for(rule), True
    elif classification is not None and classification.choice is not None:
        suggested, confidence = classification.choice.folder, classification.choice.score
        reason, why, selected = "auto", classification.reason, True
    elif classification is not None and classification.scores:
        best = classification.scores[0]
        suggested, confidence = best.folder, best.score
        reason, why, selected = "ask", classification.reason, False
    else:
        why = classification.reason if classification else reasons.reason(reasons.NO_CLASSIFIER)
        return Proposal(path, current, None, None, None, "ask", why, False, details)
    if suggested.casefold() == current.casefold():
        return None  # already where it belongs
    return Proposal(path, current, suggested, suggested, confidence, reason, why, selected, details)


def apply(
    conn: sqlite3.Connection,
    root: Path,
    proposals: Iterable[Proposal],
    *,
    folder: Path | None = None,
    before_rename: BeforeRename | None = None,
) -> OrganizeResult:
    """Move every ticked proposal with a target, as one batch. Failures don't stop the rest.

    ``folder`` is the folder that was organized: files outside the root are only taken
    from inside it (it must have been checked with ``config.validate_outside_folder``).
    A target the user changed from AutoShelf's suggestion is learned from, like an
    answer in the Inbox. Folders are never created: a missing target fails that row.
    """
    batch = uuid.uuid4().hex
    moved: list[MoveResult] = []
    failed: list[tuple[Path, str]] = []
    for proposal in proposals:
        if not proposal.selected or not proposal.target:
            continue
        outside = None
        if not is_within(proposal.path, root):
            if folder is None or not is_within(proposal.path, folder):
                failed.append((proposal.path, "it is outside the folder being organized"))
                continue
            outside = folder
        details = {**proposal.details, "reason": proposal.why}
        if proposal.suggested:
            details["suggested"] = proposal.suggested
        try:
            result = move_file(
                conn,
                proposal.path,
                root.joinpath(*proposal.target.split("/")),
                root=root,
                reason=REASON_ORGANIZE,
                confidence=proposal.confidence if proposal.target == proposal.suggested else None,
                batch_id=batch,
                details=details,
                before_rename=before_rename,
                allowed_source=outside,
            )
        except MoveError as exc:
            failed.append((proposal.path, str(exc)))
            continue
        moved.append(result)
        repository.set_suggestion(conn, result.file_id, proposal.target)
        if proposal.suggested and proposal.target.casefold() != proposal.suggested.casefold():
            learning.record_choice(
                conn,
                file_id=result.file_id,
                from_folder=proposal.suggested,
                to_folder=proposal.target,
                source=learning.SOURCE_INBOX,
            )
    return OrganizeResult(batch, moved, failed)
