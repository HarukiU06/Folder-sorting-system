"""Better names for files with junk names (SPEC section 5.2), always with approval.

Names like ``scan_0034.pdf``, ``document(3).docx``, ``download.pdf`` or a UUID say nothing.
A clean name is proposed from the document's title, else its first heading, else (if the
optional LLM is on and the user asks) by the LLM. A rename only happens when the user
approves it, is recorded in history, and can be undone.
"""

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePath

from autoshelf.actions.mover import BeforeRename, MoveError, MoveResult, move_file
from autoshelf.layout import Zone, zone_of
from autoshelf.llm.base import LLMError, Message, Provider, clip

MAX_STEM_CHARS = 100
REASON_RENAME = "renamed (you approved)"

_JUNK_WORDS = (
    r"scan|scanned|img|image|dsc|dscn|dcim|photo|pic|document|doc|file|untitled|download|"
    r"downloads|new document|new file|attachment|print|output|export|temp|tmp|copy|"
    r"スキャン|新規ドキュメント|名称未設定|無題|ドキュメント"
)
_JUNK = re.compile(
    rf"^(?:(?:{_JUNK_WORDS})[\s_\-.]*(?:\(?\d+\)?)?(?:[\s_\-.]*\(?\d+\)?)*|"  # scan_0034
    r"[\d\s_\-.()]+|"  # 20250314_1201, 001
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}|"  # a UUID
    r"[0-9a-f]{16,}|"  # a long hash
    r".{1,2})$",  # one or two characters
    re.IGNORECASE,
)
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10))} | {
    f"lpt{i}" for i in range(1, 10)
}
_TITLE_NOISE = re.compile(
    r"^(microsoft (word|powerpoint|excel) - |untitled|document\d*$)", re.IGNORECASE
)


@dataclass(frozen=True)
class RenameProposal:
    """A better name for a file, and where it came from ("title", "heading" or "AI")."""

    path: Path
    new_name: str
    source: str


class RenameError(Exception):
    """A file can't be renamed as asked. Nothing was renamed."""


def is_junk_name(name: str) -> bool:
    """True for names that say nothing about the file (scan_0034.pdf, document(3).docx...)."""
    stem = unicodedata.normalize("NFKC", PurePath(name).stem).strip()
    return bool(_JUNK.fullmatch(stem))


def clean_stem(text: str) -> str:
    """Text made safe as a Windows file name (without extension), or "" if nothing is left."""
    text = unicodedata.normalize("NFKC", text)
    text = _INVALID.sub(" ", text)
    text = " ".join(text.split()).strip(" .")
    if len(text) > MAX_STEM_CHARS:
        text = text[:MAX_STEM_CHARS].rsplit(" ", 1)[0].strip(" .") or text[:MAX_STEM_CHARS]
    if text.casefold() in _RESERVED:
        text = f"{text} file"
    return text


def _from_title(title: str | None) -> str:
    if not title:
        return ""
    title = re.sub(r"\.(docx?|pptx?|xlsx?|pdf|txt)$", "", title.strip(), flags=re.IGNORECASE)
    title = re.sub(r"^microsoft (word|powerpoint|excel) - ", "", title, flags=re.IGNORECASE)
    if _TITLE_NOISE.match(title) or is_junk_name(title + ".x"):
        return ""
    return clean_stem(title)


def _from_heading(text: str | None) -> str:
    for line in (text or "").splitlines()[:15]:
        line = line.strip().lstrip("#").strip()
        letters = sum(char.isalpha() for char in line)
        if 3 <= len(line) <= 80 and letters >= 3 and not is_junk_name(line + ".x"):
            return clean_stem(line)
    return ""


def propose_name(
    path: Path, title: str | None, text: str | None, provider: Provider | None = None
) -> RenameProposal | None:
    """A better name from the title or first heading, or (with ``provider``) the LLM."""
    suffix = path.suffix
    for stem, source in ((_from_title(title), "title"), (_from_heading(text), "heading")):
        if stem and stem.casefold() != path.stem.casefold():
            return RenameProposal(path, stem + suffix, source)
    if provider is None:
        return None
    stem = clean_stem(ask_for_name(provider, path.name, title, text or ""))
    if not stem or is_junk_name(stem + suffix):
        return None
    return RenameProposal(path, stem + suffix, "AI")


def ask_for_name(provider: Provider, name: str, title: str | None, text: str) -> str:
    """The LLM's suggestion for a file name (without extension)."""
    system = (
        "You name files in a personal library. Reply with one descriptive file name of at "
        "most 8 words, without extension, quotes or explanation, in the document's language. "
        "Example: Andrew Ng - Unsupervised Learning Lecture Notes"
    )
    prompt = f"Current name: {name}\nTitle: {title or '-'}\n\nText:\n{clip(text, 4000)}"
    reply = provider.complete(system, [Message("user", prompt)], max_tokens=60)
    line = reply.strip().splitlines()[0] if reply.strip() else ""
    return line.strip().strip("\"'`")


def rename_suggestions(conn: sqlite3.Connection, root: Path) -> list[RenameProposal]:
    """Junk-named files in the library and Inbox with a proposal from their title or text.

    Uses what AutoShelf already read; nothing is opened and no LLM is used.
    """
    proposals = []
    rows = conn.execute(
        "SELECT id, path, title, text_excerpt FROM files WHERE status IN ('sorted', 'inbox')"
        " ORDER BY path COLLATE NOCASE"
    )
    for row in rows:
        path = Path(row["path"])
        if zone_of(path, root) not in (Zone.CATEGORY, Zone.INBOX) or not is_junk_name(path.name):
            continue
        if not path.is_file():
            continue
        proposal = propose_name(path, row["title"], row["text_excerpt"])
        if proposal is not None:
            proposals.append(proposal)
    return proposals


def rename(
    conn: sqlite3.Connection,
    path: Path,
    new_name: str,
    *,
    root: Path,
    before_rename: BeforeRename | None = None,
) -> MoveResult:
    """Rename a file in place (after the user approved). Never overwrites: a taken name
    gets " (2)". Recorded in history and undoable."""
    suffix = path.suffix
    stem = clean_stem(PurePath(new_name).stem if new_name.endswith(suffix) else new_name)
    if not stem:
        raise RenameError("The new name is empty")
    try:
        return move_file(
            conn,
            path,
            path.parent,
            root=root,
            reason=REASON_RENAME,
            action="rename",
            new_name=stem + suffix,
            details={"explanation": f"renamed from {path.name!r}"},
            before_rename=before_rename,
        )
    except MoveError as exc:
        raise RenameError(str(exc)) from exc


__all__ = [
    "LLMError",
    "RenameError",
    "RenameProposal",
    "clean_stem",
    "is_junk_name",
    "propose_name",
    "rename",
    "rename_suggestions",
]
