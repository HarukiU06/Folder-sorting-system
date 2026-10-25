"""Words for what the core reports as data, shared by the app and the command line.

The core (``actions``, ``ml``, ``features``) returns and stores structured results, such
as the reasons of ``autoshelf.reasons``; the sentences users read are built here, when they
are shown, so the app and the command line say the same thing and the wording lives in one
place. History rows written before reasons were stored as data keep their English text,
which is shown as it is.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
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


def waiting_text(reason: Mapping | None) -> str:
    """Why a file waits in the Inbox, as a plain sentence without scores."""
    code = (reason or {}).get("code")
    if code == reasons.LOW_SCORE:
        return f"None of your folders is a clear match. The closest is {reason['folder']}."
    if code == reasons.TOO_CLOSE:
        if reason.get("rival") is None:
            return f"AutoShelf isn't sure about {reason['folder']}."
        return (
            f"It fits {reason['folder']} and {reason['rival']} about equally well, "
            "so AutoShelf is asking you."
        )
    if code == reasons.NO_FOLDERS:
        return "There are no folders to sort into yet."
    if code == reasons.NO_CLASSIFIER:
        return "AutoShelf can't read files to sort them right now."
    if code == reasons.RULE_FOLDER_MISSING:
        return (
            f"Your rule {reason['rule']!r} sends it to {reason['folder']}, "
            "but that folder no longer exists."
        )
    if code == reasons.FOLDER_GONE:
        return f"Its folder {reason['folder']} no longer exists."
    text = reason_text(reason)
    return text[:1].upper() + text[1:] if text else ""


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
        return f"a {str(reason.get('to', '')).upper()} version of {what}"
    if operation == "images_to_pdf":
        return f"a PDF of {what}"
    return f"a new file from {what}" if sources else "a new file"


# --- rules -----------------------------------------------------------------------------------


def digest(sorted_files: int, waiting: int, learned: int) -> str:
    """ "This week: 23 sorted, 3 waiting in Inbox, 1 correction learned"."""
    corrections = "correction" if learned == 1 else "corrections"
    return (
        f"This week: {sorted_files} sorted, {waiting} waiting in Inbox, "
        f"{learned} {corrections} learned"
    )


def size_text(size: int) -> str:
    """1536 -> "1.5 KB"."""
    value = float(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value):,} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


def condition(item: Mapping) -> str:
    """A rule's condition in words, e.g. 'file name matches /invoice|receipt/'."""
    kind = item.get("kind")
    label = RULE_KINDS.get(kind, str(kind))
    if kind == "size":
        low, high = item.get("min"), item.get("max")
        if low is not None and high is not None:
            return f"{label} {size_text(low)} to {size_text(high)}"
        return (
            f"{label} at least {size_text(low)}"
            if low is not None
            else (f"{label} at most {size_text(high)}")
        )
    if kind == "date":
        start, end = item.get("from"), item.get("to")
        if start and end:
            return f"{label} {start} to {end}"
        return f"{label} {start} or later" if start else f"{label} {end} or earlier"
    pattern = str(item.get("pattern", ""))
    if kind == "name":
        return f"{label} /{pattern}/"
    if kind == "extension":
        return f"{label} {', '.join(sorted(extensions(pattern)))}"
    return f'{label} "{pattern}"'


def conditions(rule) -> str:
    """All of a rule's conditions in one phrase (a ``RuleRecord``)."""
    joiner = " or " if rule.match_any else " and "
    return joiner.join(condition(item) for item in rule.conditions)


def rule_actions(target: str, tags: Sequence[str] = (), rename: str = "") -> str:
    """What a rule does: "move to Finance; tag Taxes; rename to {date} {title}"."""
    parts = [f"move to {target}" if target else "AutoShelf decides the folder"]
    if tags:
        parts.append("tag " + ", ".join(tags))
    if rename:
        parts.append(f"rename to {rename}")
    return "; ".join(parts)


def rule_match(found: Mapping) -> str:
    """What one condition of a rule found in a file."""
    kind = found.get("kind")
    if kind == "name":
        return f"{found['name']!r} contains {found['found']!r}"
    if kind == "extension":
        return f"the extension is {found['found']}"
    if kind == "text":
        return f"the text contains {found['found']!r}"
    if kind == "size":
        return f"its size is {size_text(int(found['found']))}"
    if kind == "date":
        return f"it is dated {found['found']}"
    return str(found.get("found", ""))


def rule_matches(matched: Sequence[Mapping]) -> str:
    """What a rule's conditions found, in one phrase."""
    return " and ".join(rule_match(found) for found in matched)


def rule_test(test) -> str:
    """The answer to "which rule would handle this file?" (an ``AppService.RuleTest``)."""
    if test.rule is None:
        return "No rule matches this file; AutoShelf would decide by its content."
    does = []
    if test.target:
        does.append(f"it would go to {test.target}")
    else:
        does.append("AutoShelf would decide its folder")
    if test.rule.tags:
        does.append("be tagged " + ", ".join(test.rule.tags))
    if test.new_name:
        does.append(f"be renamed to {test.new_name!r}")
    return f"Rule {test.rule.name!r} matches ({rule_matches(test.matched)}): " + ", ".join(does)


def rule_suggestion(item) -> str:
    """A suggested rule in words (a ``features.rule_suggestions.RuleSuggestion``)."""
    what = (
        f"with {item.condition['pattern']!r} in their names"
        if item.condition["kind"] == "name"
        else f"ending in {item.condition['pattern']}"
    )
    return f"You put {len(item.files)} files {what} into {item.target}. Make this a rule?"


# --- the "Why?" of a history row ---------------------------------------------------------------


def decision_lines(details: Mapping, root: Path | None = None) -> list[str]:
    """Readable lines explaining a stored decision (empty if nothing was stored)."""
    lines: list[str] = []
    rule = details.get("rule")
    if rule:
        if "conditions" in rule:
            when = " and ".join(condition(item) for item in rule["conditions"])
            does = rule_actions(rule["target"], rule.get("tags") or (), rule.get("rename", ""))
            lines.append(f"Rule {rule['name']!r}: {when} -> {does}")
            if rule.get("matched"):
                lines.append(f"  matched: {rule_matches(rule['matched'])}")
        else:  # written before rules were stored as data
            lines.append(f"Rule {rule['name']!r}: {rule['condition']} -> {rule['target']}")
            if rule.get("match"):
                lines.append(f"  matched: {rule['match']}")
    code = (details.get("reason") or {}).get("code", reasons.RULE)
    why = explanation(details)
    if code == reasons.CREATED:
        lines.append(f"AutoShelf made {why}")
    elif why and not (rule and code == reasons.RULE):
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
    if details.get("renamed_from"):
        lines.append(f"Renamed by its rule; it was called {details['renamed_from']!r}")
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
        return f"Made {created(entry.details.get('reason') or {})}"
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


def why_here(path: Path, entry: HistoryEntry | None, here: bool) -> str:
    """Why a file is where it is, in a sentence ("Why is this here?" in Explorer's menu).

    ``entry`` is AutoShelf's last move of it; ``here`` says whether that move put it where
    it is now."""
    name = path.name
    if entry is None:
        return f"AutoShelf has no record of moving {name}: you or another program put it here."
    when = _when(entry.ts)
    if not here:
        return (
            f"AutoShelf last moved {name} on {when}; since then you or another program moved "
            "it here."
        )
    if entry.action == "undo":
        return f"You undid AutoShelf's move of {name} on {when}, so it is back where it was."
    if entry.action == "create":
        return f"AutoShelf made {name} on {when}: {created(entry.details.get('reason') or {})}."
    if entry.action == "rename":
        return f"AutoShelf renamed it to {name} on {when} ({history_reason(entry)})."
    return f"AutoShelf moved {name} here on {when} ({history_reason(entry)})."


def _when(ts: str) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M")


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
