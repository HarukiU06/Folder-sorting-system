"""Sort a folder's loose files again when a new folder appears inside it.

When the user makes "Notes" inside "Folder" (in the Library, in Explorer, on the iPhone, or
while AutoShelf was closed), the files lying directly in Folder are looked at again, and
each may move down into one of Folder's subfolders, never anywhere else:

- **Moved by itself**: a file whose name (or title) has every word of a subfolder's name
  ("Calculus notes.md" -> Notes), or one of the user's rules sends there. Measured with the
  real model, names never sent a file to the wrong folder.
- **Offered** ("might belong"): a file whose content scores well for a subfolder, clearly
  better than for the other subfolders and than for Folder's own name. Content alone,
  against a folder that is still empty, was wrong too often to move files by itself, so
  the user ticks these.

For a new folder at the top level the files waiting in the Inbox are checked instead (loose
files in the root are sorted anyway), and the clear decisions for the new folder move.

Nothing here reads files: the library index already holds every file's vector, and names
and titles are known. ``find_new_folders`` decides which folders are new; it records
nothing (``remember_folders`` does), so a paused AutoShelf finds them again later.
"""

import logging
import re
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from autoshelf.actions import learning
from autoshelf.actions import rules as rule_engine
from autoshelf.actions.folders import folder_path
from autoshelf.actions.inbox import waiting_files
from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import FILENAME_ONLY, Extraction, extract
from autoshelf.extract.filename import filename_tokens, name_tokens
from autoshelf.ml.library import LibraryIndex
from autoshelf.reasons import RESORTED, reason

log = logging.getLogger(__name__)

REASON_RESORT = "resort"
REASON_USER = "user"
NAME, RULE, CONTENT, INBOX = "name", "rule", "content", "inbox"
SEEDED = "folders_seeded"
# Names Windows, iOS and macOS give a folder before the user types one.
_PLACEHOLDER = re.compile(
    r"^(?:new folder|新しいフォルダー|untitled folder|名称未設定フォルダ)(?:\s*\(\d+\)|\s+\d+)?$"
)


@dataclass(frozen=True)
class Candidate:
    """A file and the subfolder it would go to."""

    path: Path
    target: str  # the folder's key
    match: str  # NAME, RULE, CONTENT (offered only) or INBOX
    score: float | None = None


@dataclass(frozen=True)
class ResortPlan:
    """What sorting ``parent``'s loose files again would do ("" stands for the Inbox)."""

    parent: str
    triggers: tuple[str, ...]  # the new folders that caused it
    moves: list[Candidate] = field(default_factory=list)  # moved without asking
    maybes: list[Candidate] = field(default_factory=list)  # offered to the user


@dataclass(frozen=True)
class ResortResult:
    """What moving some of a plan's files did."""

    plan: ResortPlan
    batch_id: str | None
    moved: list[MoveResult]
    failed: list[tuple[Path, str]]


@dataclass(frozen=True)
class FolderNews:
    """What a scan of the folders found compared with the folders seen before."""

    seeded: bool  # nothing was known yet: every folder is simply remembered
    new: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)  # appeared holding known files
    parents: dict[str, list[str]] = field(default_factory=dict)  # folder -> its new folders


def is_placeholder(name: str) -> bool:
    """True for a name a folder gets before the user names it ("New folder (2)")."""
    return bool(_PLACEHOLDER.match(unicodedata.normalize("NFKC", name).strip().casefold()))


def _word(token: str) -> str:
    word = unicodedata.normalize("NFKC", token).casefold()
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def name_matches(folder_name: str, file_name: str, title: str | None = None) -> bool:
    """True if every word of ``folder_name`` is in the file's name or title ("Notes" and
    "Calculus notes.md"; a final "s" doesn't matter). Words in scripts without spaces
    (Japanese) may also be part of a longer word."""
    wanted = {_word(token) for token in name_tokens(folder_name) if len(token) >= 2}
    if not wanted:
        return False
    have = {_word(token) for token in filename_tokens(file_name)}
    if title:
        have |= {_word(token) for token in name_tokens(title)}
    text = unicodedata.normalize("NFKC", f"{Path(file_name).stem} {title or ''}").casefold()
    return all(word in have or (not word.isascii() and word in text) for word in wanted)


def _leaf(key: str) -> str:
    return key.rpartition("/")[2]


def _parent(key: str) -> str:
    return key.rpartition("/")[0]


def plan_resort(
    conn: sqlite3.Connection,
    root: Path,
    library: LibraryIndex,
    parent: str,
    *,
    triggers: Sequence[str] = (),
    threshold: float = DEFAULT_AUTO_THRESHOLD,
    margin: float = DEFAULT_AUTO_MARGIN,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
) -> ResortPlan:
    """Where ``parent``'s loose files would go among its subfolders (moves nothing).

    ``parent`` "" checks the files waiting in the Inbox against ``triggers`` (new top-level
    folders). ``library`` must be current (refreshed after the new folders appeared)."""
    if not parent:
        return _plan_inbox(conn, root, library, tuple(triggers), threshold, margin, ignore_patterns)
    wanted = parent.casefold()
    children = [key for key in library.folders if _parent(key).casefold() == wanted]
    plan = ResortPlan(parent, tuple(triggers))
    if not children:
        return plan
    rules = repository.list_rules(conn, enabled_only=True)
    profiles = {profile.key.casefold(): profile for profile in library.profiles()}
    parent_profile = profiles.get(wanted)
    for entry in library.entries():
        if entry.folder.casefold() != wanted:
            continue
        title, excerpt = _content(conn, entry.path)
        target, match = _by_name_or_rule(entry.path, title, excerpt, children, rules)
        if target is not None:
            plan.moves.append(Candidate(entry.path, target, match))
            continue
        guess = _by_content(library, entry, children, parent_profile, threshold, margin)
        if guess is not None:
            plan.maybes.append(guess)
    plan.maybes.sort(key=lambda item: -(item.score or 0.0))
    return plan


def _content(conn: sqlite3.Connection, path: Path) -> tuple[str | None, str | None]:
    record = repository.get_file_by_path(conn, path)
    return repository.get_file_content(conn, record.id) if record else (None, None)


def _by_name_or_rule(
    path: Path,
    title: str | None,
    excerpt: str | None,
    children: list[str],
    rules: list[repository.RuleRecord],
) -> tuple[str | None, str]:
    """The one subfolder the file's name or a rule points to, or (None, "")."""
    named = [key for key in children if name_matches(_leaf(key), path.name, title)]
    if len(named) == 1:
        return named[0], NAME
    if named:
        return None, ""  # it fits two subfolders by name: the user decides
    extraction = Extraction(
        FILENAME_ONLY, tuple(filename_tokens(path.name)), text=excerpt or "", title=title
    )
    found = rule_engine.first_match(rules, path, extraction)
    if found is not None:
        target = found.rule.target_folder.casefold()
        for key in children:
            if target == key.casefold():
                return key, RULE
    return None, ""


def _by_content(library, entry, children, parent_profile, threshold, margin) -> Candidate | None:
    """A subfolder the file's content clearly points to, to offer (or None)."""
    scores = library.classify(entry.vector, exclude=entry.path).scores
    ranked = [item for item in scores if item.folder in children]
    if not ranked:
        return None
    best = ranked[0]
    if best.score < threshold:
        return None
    if len(ranked) > 1 and best.score - ranked[1].score < margin:
        return None
    if parent_profile is not None and best.score - float(parent_profile.vector @ entry.vector) < (
        margin
    ):
        return None
    return Candidate(entry.path, best.folder, CONTENT, best.score)


def _plan_inbox(conn, root, library, triggers, threshold, margin, ignore_patterns) -> ResortPlan:
    plan = ResortPlan("", triggers)
    folded = [key.casefold() for key in triggers]
    for path in waiting_files(root, ignore_patterns):
        title, _ = _content(conn, path)
        named = [key for key in triggers if name_matches(_leaf(key), path.name, title)]
        if len(named) == 1:
            plan.moves.append(Candidate(path, named[0], NAME))
            continue
        vector = library.stored_vector(path)
        if vector is None:
            try:
                vector = library.vectorize(extract(path), path.name)
            except OSError as exc:
                log.warning("Couldn't read %s: %s", path, exc)
                continue
        choice = library.classify(vector, threshold=threshold, margin=margin).choice
        if choice is not None and any(
            choice.folder.casefold() == key or choice.folder.casefold().startswith(key + "/")
            for key in folded
        ):
            plan.moves.append(Candidate(path, choice.folder, INBOX, choice.score))
    return plan


def apply_moves(
    conn: sqlite3.Connection,
    root: Path,
    plan: ResortPlan,
    candidates: Sequence[Candidate],
    *,
    chosen_by_user: bool,
    before_rename: BeforeRename | None = None,
) -> ResortResult:
    """Move ``candidates`` (some of the plan's) as one batch in History.

    ``chosen_by_user``: the user ticked them in the list of files that might belong; each
    then teaches AutoShelf, like an answer in the Inbox."""
    batch = uuid.uuid4().hex if len(candidates) > 1 else None
    moved: list[MoveResult] = []
    failed: list[tuple[Path, str]] = []
    for item in candidates:
        trigger = item.target if item.target in plan.triggers else next(iter(plan.triggers), None)
        why = reason(RESORTED, folder=item.target, trigger=trigger, match=item.match)
        try:
            result = move_file(
                conn,
                item.path,
                folder_path(root, item.target),
                root=root,
                reason=REASON_USER if chosen_by_user else REASON_RESORT,
                confidence=item.score,
                batch_id=batch,
                details={"reason": why},
                before_rename=before_rename,
            )
        except MoveError as exc:
            failed.append((item.path, str(exc)))
            continue
        moved.append(result)
        repository.set_suggestion(conn, result.file_id, item.target)
        if chosen_by_user and plan.parent:
            learning.record_choice(
                conn,
                file_id=result.file_id,
                from_folder=plan.parent,
                to_folder=item.target,
                source=learning.SOURCE_INBOX,  # chosen from AutoShelf's suggestions
                history_id=result.history_id,
            )
    return ResortResult(plan, batch, moved, failed)


def find_new_folders(
    conn: sqlite3.Connection, folders: Sequence[str], relocations: Sequence = ()
) -> FolderNews:
    """Which of ``folders`` (a scan's keys) are new since the folders were last remembered.

    Not new: folders named like "New folder" (until renamed), and folders that appeared
    holding files AutoShelf knows from elsewhere (``relocations`` of the same scan: the
    folder was moved or renamed). A new folder inside another new one (a pasted tree) is
    new but sorts nothing. Records nothing."""
    if not repository.get_setting(conn, SEEDED, False):
        return FolderNews(seeded=True)
    known = repository.known_folders(conn)
    unknown = [key for key in folders if key.casefold() not in known]
    unknown_set = {key.casefold() for key in unknown}
    moved_into = {str(item.to_folder).casefold() for item in relocations if item.to_folder}
    news = FolderNews(seeded=False)
    for key in sorted(unknown, key=str.casefold):
        if is_placeholder(_leaf(key)):
            continue
        folded = key.casefold()
        if any(place == folded or place.startswith(folded + "/") for place in moved_into):
            news.renamed.append(key)
            continue
        news.new.append(key)
        parent = _parent(key)
        if parent.casefold() not in unknown_set:
            news.parents.setdefault(parent, []).append(key)
    return news


def remember_folders(conn: sqlite3.Connection, keys: Iterable[str]) -> None:
    """Remember folders as seen (placeholders aren't), and that seeding has happened."""
    repository.add_known_folders(conn, [key for key in keys if not is_placeholder(_leaf(key))])
    repository.set_setting(conn, SEEDED, True)
