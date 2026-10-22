"""Words for what the core reports as data, shared by the app and the command line.

The core (``actions``, ``ml``, ``features``) returns and stores structured results, such
as the reasons of ``autoshelf.reasons``; the sentences users read are built here, when they
are shown, so the app and the command line say the same thing and the wording lives in one
place. History rows written before reasons were stored as data keep their English text,
which is shown as it is.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from autoshelf import reasons
from autoshelf.actions.rules import RULE_KINDS, extensions
from autoshelf.db.repository import HistoryEntry
from autoshelf.ml.calibrate import MAX_WRONG, MIN_FILES, MIN_FOLDERS, Calibration, LevelResult

# --- why something happened ------------------------------------------------------------------


def reason_text(reason: Mapping | None) -> str:
    """A stored reason in words ("" for None)."""
    if not reason:
        return ""
    code = reason.get("code")
    if code == reasons.CLEAR_MATCH:
        text = f"best match {reason['folder']!r} {reason['score']:.2f}"
        if reason.get("rival") is not None:
            text += f", next {reason['rival']!r} {reason['rival_score']:.2f}"
        return text
    if code == reasons.LOW_SCORE:
        return (
            f"not sure: best match {reason['folder']!r} scored {reason['score']:.2f}, "
            f"below the {reason['threshold']:.2f} needed to move it automatically"
        )
    if code == reasons.TOO_CLOSE:
        if reason.get("rival") is None:
            return f"not sure about {reason['folder']!r} ({reason['score']:.2f})"
        return (
            f"not sure: {reason['folder']!r} ({reason['score']:.2f}) is too close to "
            f"{reason['rival']!r} ({reason['rival_score']:.2f})"
        )
    if code == reasons.NO_FOLDERS:
        return "there are no folders to sort into yet"
    if code == reasons.NO_CLASSIFIER:
        return "it can't be sorted by content right now"
    if code == reasons.RULE:
        return f"rule {reason['rule']!r} matched: {rule_matches(reason.get('matched') or [])}"
    if code == reasons.RULE_FOLDER_MISSING:
        return f"rule {reason['rule']!r} matched, but its folder {reason['folder']!r} doesn't exist"
    if code == reasons.FOLDER_GONE:
        return f"its folder {reason['folder']!r} no longer exists"
    if code == reasons.USER_MOVED:
        return f"you moved it from {reason['from_folder']!r} to {reason['to_folder']!r}"
    if code == reasons.RENAMED:
        return f"renamed from {reason['old_name']!r}"
    if code == reasons.CREATED:
        return created(reason)
    return str(code or "")


def explanation(details: Mapping | None) -> str:
    """Why a history row happened, from its stored details (older rows stored the words)."""
    if not details:
        return ""
    if details.get("reason"):
        return reason_text(details["reason"])
    return str(details.get("explanation") or "")


def created(reason: Mapping) -> str:
    """What a file AutoShelf made is: "a ZIP of 3 files", "a smaller copy of a.pdf"..."""
    sources = [Path(source).name for source in reason.get("sources") or []]
    what = sources[0] if len(sources) == 1 else f"{len(sources)} files"
    operation = reason.get("operation")
    if operation == "zip":
        return f"a ZIP of {what}"
    if operation in ("shrink_pdf", "shrink_image"):
        return f"a smaller copy of {what}"
    if operation == "convert":
        return f"{what} converted to {str(reason.get('to', '')).upper()}"
    if operation == "images_to_pdf":
        return f"a PDF of {what}"
    return f"made from {what}" if sources else "made by AutoShelf"


# --- rules -----------------------------------------------------------------------------------


def condition(item: Mapping) -> str:
    """A rule's condition in words, e.g. 'file name matches /invoice|receipt/'."""
    kind, pattern = item.get("kind"), str(item.get("pattern", ""))
    if kind == "name":
        return f"{RULE_KINDS[kind]} /{pattern}/"
    if kind == "extension":
        return f"{RULE_KINDS[kind]} {', '.join(sorted(extensions(pattern)))}"
    return f'{RULE_KINDS.get(kind, kind)} "{pattern}"'


def rule_match(found: Mapping) -> str:
    """What one condition of a rule found in a file."""
    kind = found.get("kind")
    if kind == "name":
        return f"{found['name']!r} contains {found['found']!r}"
    if kind == "extension":
        return f"the extension is {found['found']}"
    if kind == "text":
        return f"the text contains {found['found']!r}"
    return str(found.get("found", ""))


def rule_matches(matched: Sequence[Mapping]) -> str:
    """What a rule's conditions found, in one phrase."""
    return " and ".join(rule_match(found) for found in matched)


def rule_test(test) -> str:
    """The answer to "which rule would handle this file?" (an ``AppService.RuleTest``)."""
    if test.rule is None:
        return "No rule matches this file; AutoShelf would decide by its content."
    return (
        f"Rule {test.rule.name!r} matches ({rule_matches(test.matched)}): it would go to "
        f"{test.target}"
    )


# --- the "Why?" of a history row ---------------------------------------------------------------


def decision_lines(details: Mapping, root: Path | None = None) -> list[str]:
    """Readable lines explaining a stored decision (empty if nothing was stored)."""
    lines: list[str] = []
    rule = details.get("rule")
    if rule:
        if "conditions" in rule:
            when = " and ".join(condition(item) for item in rule["conditions"])
            lines.append(f"Rule {rule['name']!r}: {when} -> {rule['target']}")
            if rule.get("matched"):
                lines.append(f"  matched: {rule_matches(rule['matched'])}")
        else:  # written before rules were stored as data
            lines.append(f"Rule {rule['name']!r}: {rule['condition']} -> {rule['target']}")
            if rule.get("match"):
                lines.append(f"  matched: {rule['match']}")
    code = (details.get("reason") or {}).get("code", reasons.RULE)
    why = explanation(details)
    if why and not (rule and code == reasons.RULE):
        lines.append(f"Decision: {why}")
    candidates = details.get("candidates") or []
    if candidates:
        lines.append("Best folders:")
        for item in candidates:
            parts = []
            if "name" in item:
                parts.append(f"name/description {item['name']:.2f}")
            if "files" in item:
                parts.append(f"files {item['files']:.2f}")
            if item.get("learned"):
                parts.append(f"learned {item['learned']:+.2f}")
            detail = f"  ({', '.join(parts)})" if parts else ""
            lines.append(f"  {item['score']:.2f}  {item['folder']}{detail}")
    focus = details.get("focus")
    similar = details.get("similar_files") or []
    if similar:
        lines.append(f"Most similar files in {focus}:")
        for item in similar:
            lines.append(f"  {item['similarity']:.2f}  {shown_path(item['path'], root)}")
    keywords = details.get("keywords") or []
    if keywords:
        lines.append(f"Words in common with {focus}: {', '.join(keywords)}")
    for adjustment in details.get("learned") or []:
        examples = ", ".join(
            f"{item['file'] or 'a file'} -> {item['to']}" for item in adjustment["examples"]
        )
        lines.append(
            f"Learned {adjustment['amount']:+.2f} for {adjustment['folder']}: you moved "
            f"similar files away from it ({examples})"
        )
    if details.get("corrects"):
        lines.append(f"You moved it here, correcting #{details['corrects']}")
    return lines


def shown_path(path: str, root: Path | None) -> str:
    """A path as shown: relative to the root when inside it, else just its name."""
    if root is not None:
        try:
            return Path(path).relative_to(root).as_posix()
        except ValueError:
            pass
    return Path(path).name


_REASON_LABELS = {
    "auto": "Sorted automatically",
    "ask": "Not sure: sent to Inbox",
    "user": "You chose",
    "organize": "Organized (you approved)",
    "rename": "Renamed (you approved)",
    "renamed (you approved)": "Renamed (you approved)",  # rows written before codes
}


def history_reason(entry: HistoryEntry) -> str:
    """A history row's reason in a few words, for the History list."""
    if entry.action == "undo":
        return f"Undo of #{entry.undo_of}" if entry.undo_of else "Undo"
    if entry.action == "create":
        made = created(entry.details.get("reason") or {})
        return f"Created {made}"
    if entry.reason == "user" and entry.details.get("corrects"):
        return f"You corrected #{entry.details['corrects']}"
    if entry.reason == "duplicate":
        original = Path(str(entry.details.get("duplicate_of", ""))).name
        return f"Set aside: duplicate of {original}"
    if entry.reason.startswith("duplicate of "):  # rows written before codes
        return f"Set aside: {entry.reason}"
    if entry.reason in ("tag", "untag"):
        verb = "Tagged" if entry.reason == "tag" else "Untagged"
        return f"{verb} '{entry.details.get('tag', '')}'"
    return _REASON_LABELS.get(entry.reason, entry.reason)


# --- how well would AutoShelf sort this library? ------------------------------------------------


def percent(share: float) -> str:
    """0.873 -> "87%"."""
    return f"{share:.0%}"


def level_result(result: LevelResult) -> str:
    """One line of a calibration: "At Balanced: 87% correct, 9% to the Inbox, 4% wrong"."""
    return (
        f"At {result.level.name}: {percent(result.correct_share)} correct, "
        f"{percent(result.inbox_share)} to the Inbox, {percent(result.wrong_share)} wrong"
    )


def calibration(result: Calibration, current: int | None = None) -> list[str]:
    """What a calibration found, line by line. ``current``: the level in use, if a preset."""
    if not result.enough:
        return [
            f"Not enough files to tell yet: AutoShelf needs at least {MIN_FILES} files in "
            f"{MIN_FOLDERS} or more folders ({result.files} found). Try again later."
        ]
    lines = [
        f"Each of the {result.files} files in your {result.folders} folders was sorted as if "
        "it had just arrived:"
    ]
    for index, item in enumerate(result.results):
        lines.append(level_result(item) + ("  (now)" if index == current else ""))
    if result.recommended is not None:
        level = result.results[result.recommended].level
        lines.append(
            f"Recommended: {level.name}, the most automatic setting that puts at most "
            f"{percent(MAX_WRONG)} of the files in a wrong folder."
        )
    return lines
