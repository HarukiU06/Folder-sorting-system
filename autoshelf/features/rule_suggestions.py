"""Rules suggested from the user's own choices (Phase 12).

Each time the user puts a file in a folder (answering the Inbox, correcting AutoShelf in
History, or moving a file in Explorer or on the iPhone), a correction is recorded. When
several of those files share something simple, the same word in their names or the same
extension, and went to the same folder, a rule could do it for them. A rule is suggested
only if:

- at least ``MIN_FILES`` files show the pattern and went to that folder;
- at least ``MIN_SHARE`` of all the files with the pattern the user placed went there;
- the library agrees: of the files already in folders with that pattern, at least
  ``MIN_SHARE`` are in that folder (or below it), so the rule wouldn't contradict how
  things are sorted (unless there are too few such files to tell);
- no rule does the same yet, and the user hasn't dismissed the suggestion.
"""

import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract.filename import filename_tokens
from autoshelf.ml.library import LibraryScan, scan_library

MIN_FILES = 3
MIN_SHARE = 0.8
MAX_SUGGESTIONS = 5
# Words in file names that say nothing about where a file belongs.
_EMPTY_WORDS = frozenset(
    """
    scan scanned img image photo pic document doc file untitled download copy final draft
    new old version page pages the and for from with
    """.split()
)
_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿]")


@dataclass(frozen=True)
class RuleSuggestion:
    """A rule the user's choices point to: files matching ``condition`` go to ``target``."""

    key: str  # stable, e.g. "name:invoice->Finance" (remembered when dismissed)
    name: str  # a name for the rule
    condition: dict  # {"kind": "name"|"extension", "pattern": ...}
    target: str
    files: tuple[str, ...]  # the files that show it


def words(name: str) -> set[str]:
    """The telling words of a file name, lowercased: no numbers, short or empty words."""
    found = set()
    for token in filename_tokens(name):
        word = unicodedata.normalize("NFKC", token).casefold()
        long_enough = len(word) >= 3 or (len(word) >= 2 and _CJK.search(word))
        if long_enough and not word.isdigit() and word not in _EMPTY_WORDS:
            found.add(word)
    return found


def _shows(kind: str, pattern: str, name: str) -> bool:
    if kind == "name":
        return pattern in words(name)
    return Path(name).suffix.casefold() == pattern


def suggest_rules(
    conn: sqlite3.Connection,
    root: Path,
    ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
    dismissed: Iterable[str] = (),
) -> list[RuleSuggestion]:
    """Rules the user's corrections point to, most supported first (reads no file)."""
    scan = scan_library(root, ignore_patterns)
    folders = {key.casefold(): key for key in scan.folders}
    choices: list[tuple[str, str]] = []  # (file name, folder key)
    for row in repository.list_corrections(conn):
        folder = folders.get(str(row["to_folder"]).casefold())
        if folder is None or not row["path"]:
            continue  # the folder is gone, or the file record is
        choice = (Path(row["path"]).name, folder)
        if choice not in choices:  # one file placed twice counts once
            choices.append(choice)

    candidates: dict[tuple[str, str, str], list[str]] = {}
    for name, folder in choices:
        for word in words(name):
            candidates.setdefault(("name", word, folder), []).append(name)
        suffix = Path(name).suffix.casefold()
        if suffix:
            candidates.setdefault(("extension", suffix, folder), []).append(name)

    skip = set(dismissed)
    rules = repository.list_rules(conn)
    found = []
    for (kind, pattern, folder), names in candidates.items():
        if len(names) < MIN_FILES:
            continue
        showing = [target for name, target in choices if _shows(kind, pattern, name)]
        if names and len(names) / len(showing) < MIN_SHARE:
            continue
        key = f"{kind}:{pattern}->{folder}"
        if key in skip or _covered(rules, kind, pattern, folder):
            continue
        if not _library_agrees(scan, kind, pattern, folder):
            continue
        found.append(
            RuleSuggestion(
                key,
                _rule_name(kind, pattern),
                {"kind": kind, "pattern": pattern},
                folder,
                tuple(names),
            )
        )
    found.sort(key=lambda item: (-len(item.files), item.condition["kind"] != "name", item.key))
    return found[:MAX_SUGGESTIONS]


def _library_agrees(scan: LibraryScan, kind: str, pattern: str, folder: str) -> bool:
    matching = [item for item in scan.files if _shows(kind, pattern, item.path.name)]
    if len(matching) < MIN_FILES:
        return True  # too few files in the library to tell
    inside = [
        item for item in matching if item.folder == folder or item.folder.startswith(folder + "/")
    ]
    return len(inside) / len(matching) >= MIN_SHARE


def _covered(rules: Sequence[repository.RuleRecord], kind: str, pattern: str, folder: str) -> bool:
    """True if a rule already sends files like these to ``folder``."""
    for rule in rules:
        if rule.target_folder.casefold() != folder.casefold():
            continue
        for condition in rule.conditions:
            if condition.get("kind") != kind:
                continue
            if kind == "name" and str(condition.get("pattern", "")).casefold() == pattern:
                return True
            if kind == "extension" and pattern in _extensions(str(condition.get("pattern", ""))):
                return True
    return False


def _extensions(pattern: str) -> set[str]:
    from autoshelf.actions.rules import extensions

    return extensions(pattern)


def _rule_name(kind: str, pattern: str) -> str:
    if kind == "name":
        return f"Files named {pattern}"
    return f"{pattern} files"
