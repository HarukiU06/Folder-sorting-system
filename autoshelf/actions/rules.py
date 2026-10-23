"""User rules that override the classifier (SPEC section 4.4; richer since Phase 12).

Rules are checked in priority order before classification; the first enabled rule whose
conditions hold applies. A rule has one or more conditions, which must all hold (or any
one of them, with ``match_any``), and one or more actions:

- move the file to a folder, overriding the classifier (logged as "Rule: <name>");
- add tags;
- rename the file from a template such as ``{date} {title}``. A rule the user wrote counts
  as their approval: the rename is done, logged in history and can be undone.

A rule without a folder lets AutoShelf decide where the file goes; its tags and rename
still apply. Conditions, as stored:

- ``{"kind": "name", "pattern": regex}``: searched in the file name (any case),
- ``{"kind": "extension", "pattern": ".pdf, docx"}``: one or more extensions,
- ``{"kind": "text", "pattern": words}``: the text, title or metadata contains them,
- ``{"kind": "size", "min": bytes, "max": bytes}``: either bound may be None,
- ``{"kind": "date", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}``: the file's modification
  date (either bound may be None).
"""

import re
import string
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from autoshelf.db.repository import RuleRecord
from autoshelf.extract import Extraction

RULE_KINDS = {
    "name": "file name matches",
    "extension": "extension is",
    "text": "text contains",
    "size": "size is",
    "date": "date is",
}
REASON_PREFIX = "Rule: "
# Fields a rename template may use, e.g. "{date} {title}".
TEMPLATE_FIELDS = ("name", "title", "date", "year", "month", "day", "folder")
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgt]?i?b?)?\s*$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


class RuleError(Exception):
    """A rule can't be saved as given."""


@dataclass(frozen=True)
class RuleMatch:
    """The rule that applies, and what its conditions found (JSON-ready, e.g.
    ``{"kind": "name", "name": "scan invoice.pdf", "found": "invoice"}``)."""

    rule: RuleRecord
    matched: tuple[dict, ...]


# --- checking a rule -------------------------------------------------------------------------


def check_rule(
    name: str,
    conditions: Sequence[Mapping],
    *,
    target: str = "",
    tags: Sequence[str] = (),
    rename_template: str = "",
) -> list[dict]:
    """Raise RuleError if a rule is incomplete or invalid; returns its conditions, cleaned."""
    if not name.strip():
        raise RuleError("Give the rule a name")
    if not conditions:
        raise RuleError("The rule needs at least one condition")
    cleaned = [check_condition(condition) for condition in conditions]
    if not (target.strip() or tags or rename_template.strip()):
        raise RuleError("The rule needs something to do: a folder, tags or a new name")
    check_template(rename_template)
    return cleaned


def check_condition(condition: Mapping) -> dict:
    """One condition, checked and cleaned (raises RuleError)."""
    kind = condition.get("kind")
    if kind not in RULE_KINDS:
        raise RuleError(f"Unknown kind of condition {kind!r}; use one of {', '.join(RULE_KINDS)}")
    if kind in ("name", "extension", "text"):
        pattern = str(condition.get("pattern") or "").strip()
        if not pattern:
            raise RuleError("The rule needs something to look for")
        if kind == "name":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise RuleError(f"{pattern!r} is not a valid regular expression: {exc}") from exc
        elif kind == "extension" and not extensions(pattern):
            raise RuleError(f"{pattern!r} contains no extension")
        return {"kind": kind, "pattern": pattern}
    if kind == "size":
        low, high = _size_or_none(condition.get("min")), _size_or_none(condition.get("max"))
        if low is None and high is None:
            raise RuleError("Give a smallest size, a largest size, or both")
        if low is not None and high is not None and low > high:
            raise RuleError("The smallest size is larger than the largest")
        return {"kind": kind, "min": low, "max": high}
    start, end = _date_or_none(condition.get("from")), _date_or_none(condition.get("to"))
    if start is None and end is None:
        raise RuleError("Give a first date, a last date, or both")
    if start is not None and end is not None and start > end:
        raise RuleError("The first date is after the last")
    return {
        "kind": kind,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
    }


def check_template(template: str) -> None:
    """Raise RuleError for a rename template with an unknown {field} or bad braces."""
    try:
        fields = [field for _, field, _, _ in string.Formatter().parse(template) if field]
    except ValueError as exc:
        raise RuleError(f"The new name {template!r} isn't valid: {exc}") from exc
    unknown = [field for field in fields if field not in TEMPLATE_FIELDS]
    if unknown:
        allowed = ", ".join("{" + field + "}" for field in TEMPLATE_FIELDS)
        raise RuleError(f"Unknown field {{{unknown[0]}}} in the new name; use {allowed}")


def extensions(pattern: str) -> set[str]:
    """``".pdf, DOCX md"`` -> ``{".pdf", ".docx", ".md"}``."""
    parts = re.split(r"[\s,;]+", pattern.strip())
    return {"." + part.lstrip(".*").casefold() for part in parts if part.lstrip(".*")}


def parse_size(text: str) -> int:
    """``"10 MB"`` -> 10485760 (KB, MB, GB, TB are powers of 1024). Raises RuleError."""
    found = _SIZE.match(text)
    if found is None:
        raise RuleError(f"{text!r} isn't a size; write it like 500KB or 10MB")
    unit = found.group(2).casefold().rstrip("b").rstrip("i")
    return int(float(found.group(1)) * _SIZE_UNITS[unit])


def _size_or_none(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        if value < 0:
            raise RuleError("A size can't be negative")
        return int(value)
    return parse_size(str(value))


def _date_or_none(value) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise RuleError(f"{value!r} isn't a date; write it like 2025-03-31") from exc


def conditions(rule: RuleRecord) -> list[dict]:
    """A rule's conditions as data, as stored with the decisions it makes."""
    return [dict(condition) for condition in rule.conditions]


# --- matching files ----------------------------------------------------------------------------


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def match_condition(condition: Mapping, path: Path, extraction: Extraction | None) -> dict | None:
    """What one condition found in this file (as data), or None if it doesn't hold."""
    kind = condition.get("kind")
    if kind == "name":
        try:
            found = re.search(condition["pattern"], path.name, re.IGNORECASE)
        except re.error:
            return None
        return {"kind": "name", "name": path.name, "found": found.group()} if found else None
    if kind == "extension":
        suffix = path.suffix.casefold()
        if suffix not in extensions(condition["pattern"]):
            return None
        return {"kind": "extension", "found": suffix}
    if kind == "text":
        if extraction is None:
            return None
        wanted = _fold(str(condition["pattern"]).strip())
        haystack = _fold(
            "\n".join([extraction.title or "", *extraction.metadata.values(), extraction.text])
        )
        return {"kind": "text", "found": condition["pattern"]} if wanted in haystack else None
    if kind in ("size", "date"):
        try:
            st = path.stat()
        except OSError:
            return None
        if kind == "size":
            low, high = condition.get("min"), condition.get("max")
            ok = (low is None or st.st_size >= low) and (high is None or st.st_size <= high)
            return {"kind": "size", "found": st.st_size} if ok else None
        day = datetime.fromtimestamp(st.st_mtime).date()
        start, end = condition.get("from"), condition.get("to")
        ok = (start is None or day >= date.fromisoformat(start)) and (
            end is None or day <= date.fromisoformat(end)
        )
        return {"kind": "date", "found": day.isoformat()} if ok else None
    return None


def match_rule(
    rule: RuleRecord, path: Path, extraction: Extraction | None
) -> tuple[dict, ...] | None:
    """What ``rule``'s conditions found in this file, or None if the rule doesn't apply."""
    found = [match_condition(condition, path, extraction) for condition in rule.conditions]
    if rule.match_any:
        matched = tuple(item for item in found if item is not None)
        return matched or None
    if found and all(item is not None for item in found):
        return tuple(found)
    return None


def first_match(
    rules: Iterable[RuleRecord], path: Path, extraction: Extraction | None
) -> RuleMatch | None:
    """The first enabled rule (in the given order) that applies to the file."""
    for rule in rules:
        if not rule.enabled:
            continue
        found = match_rule(rule, path, extraction)
        if found is not None:
            return RuleMatch(rule, found)
    return None


def reason_for(rule: RuleRecord) -> str:
    """The history reason of a move or rename a rule made."""
    return f"{REASON_PREFIX}{rule.name}"


def new_name(
    rule: RuleRecord, path: Path, extraction: Extraction | None, folder: str | None = None
) -> str | None:
    """The name ``rule``'s template gives the file (keeping its extension), or None.

    None too when the template gives nothing usable or the name it already has.
    ``folder``: where the file is going (for ``{folder}``).
    """
    if not rule.rename_template.strip():
        return None
    from autoshelf.features.rename import clean_stem  # features build on actions

    try:
        st = path.stat()
    except OSError:
        return None
    when = datetime.fromtimestamp(st.st_mtime)
    values = {
        "name": path.stem,
        "title": (extraction.title if extraction and extraction.title else path.stem),
        "date": when.strftime("%Y-%m-%d"),
        "year": when.strftime("%Y"),
        "month": when.strftime("%m"),
        "day": when.strftime("%d"),
        "folder": folder.rsplit("/", 1)[-1] if folder else "",
    }
    try:
        stem = clean_stem(rule.rename_template.format_map(values))
    except (KeyError, ValueError, IndexError):
        return None
    if not stem:
        return None
    name = stem + path.suffix
    return None if name == path.name else name
