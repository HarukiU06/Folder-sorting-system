"""User rules that override the classifier (SPEC section 4.4).

Rules are checked in priority order before classification; the first enabled rule that
matches sends the file straight to its folder, logged with the reason "Rule: <name>".
Kinds:

- ``name``: a regular expression searched in the file name (any case),
- ``extension``: one or more extensions, e.g. ``.ipynb`` or ``pdf, docx``,
- ``text``: words the file's text (or title or metadata) contains (any case).
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.db.repository import RuleRecord
from autoshelf.extract import Extraction

RULE_KINDS = {
    "name": "file name matches",
    "extension": "extension is",
    "text": "text contains",
}
REASON_PREFIX = "Rule: "


class RuleError(Exception):
    """A rule can't be saved as given."""


@dataclass(frozen=True)
class RuleMatch:
    """The rule that decided, and what it matched."""

    rule: RuleRecord
    match: str


def check_rule(name: str, kind: str, pattern: str) -> None:
    """Raise RuleError if a rule is incomplete or its pattern is invalid."""
    if not name.strip():
        raise RuleError("Give the rule a name")
    if kind not in RULE_KINDS:
        raise RuleError(f"Unknown kind of rule {kind!r}; use one of {', '.join(RULE_KINDS)}")
    if not pattern.strip():
        raise RuleError("The rule needs something to look for")
    if kind == "name":
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RuleError(f"{pattern!r} is not a valid regular expression: {exc}") from exc
    elif kind == "extension" and not extensions(pattern):
        raise RuleError(f"{pattern!r} contains no extension")


def extensions(pattern: str) -> set[str]:
    """``".pdf, DOCX md"`` -> ``{".pdf", ".docx", ".md"}``."""
    parts = re.split(r"[\s,;]+", pattern.strip())
    return {"." + part.lstrip(".*").casefold() for part in parts if part.lstrip(".*")}


def describe_condition(kind: str, pattern: str) -> str:
    """The condition of a rule in words, e.g. 'file name matches /invoice|receipt/'."""
    if kind == "name":
        return f"{RULE_KINDS[kind]} /{pattern}/"
    if kind == "extension":
        return f"{RULE_KINDS[kind]} {', '.join(sorted(extensions(pattern)))}"
    return f'{RULE_KINDS.get(kind, kind)} "{pattern}"'


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def match_rule(rule: RuleRecord, path: Path, extraction: Extraction | None) -> str | None:
    """What ``rule`` matched in this file, or None if it doesn't apply."""
    if rule.kind == "name":
        try:
            found = re.search(rule.pattern, path.name, re.IGNORECASE)
        except re.error:
            return None
        return f"{path.name!r} contains {found.group()!r}" if found else None
    if rule.kind == "extension":
        suffix = path.suffix.casefold()
        return f"the extension is {suffix}" if suffix in extensions(rule.pattern) else None
    if rule.kind == "text" and extraction is not None:
        wanted = _fold(rule.pattern.strip())
        haystack = _fold(
            "\n".join([extraction.title or "", *extraction.metadata.values(), extraction.text])
        )
        return f"the text contains {rule.pattern.strip()!r}" if wanted in haystack else None
    return None


def first_match(
    rules: Iterable[RuleRecord], path: Path, extraction: Extraction | None
) -> RuleMatch | None:
    """The first enabled rule (in the given order) that matches the file."""
    for rule in rules:
        if not rule.enabled:
            continue
        found = match_rule(rule, path, extraction)
        if found is not None:
            return RuleMatch(rule, found)
    return None


def reason_for(rule: RuleRecord) -> str:
    """The history reason of a rule-based move."""
    return f"{REASON_PREFIX}{rule.name}"
