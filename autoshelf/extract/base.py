"""Shared types and helpers for extractors.

An extractor is a function ``extract(file, max_chars) -> Content`` that receives a
binary file object opened by the registry. It only reads, stops collecting text once
``max_chars`` is reached, and may raise on unreadable files (the registry turns that
into a filename-only result). Because the registry owns the open file, the OS handle is
always closed when extraction returns, so a library that leaks objects on errors can't
keep the file locked and block the move that follows.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import BinaryIO

MAX_TEXT_CHARS = 20_000
# Files that must be parsed as a whole (e.g. notebooks) are skipped above this size.
MAX_WHOLE_FILE_BYTES = 50 * 1024 * 1024

# Soft hyphen, zero-width space/non-joiner/joiner, word joiner and BOM.
_INVISIBLE_CHARS = (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)
_INVISIBLE = re.compile("[" + "".join(map(chr, _INVISIBLE_CHARS)) + "]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SPACES = re.compile(r"[^\S\n]+")
_SPACES_AROUND_NEWLINE = re.compile(r" ?\n ?")
_BLANK_LINES = re.compile(r"\n{3,}")


class ExtractionError(Exception):
    """A file can't be read for a known reason (e.g. it is password-protected)."""


@dataclass
class Content:
    """What an extractor found in a file."""

    text: str = ""
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    truncated: bool = False


def clean_text(text: str) -> str:
    """Normalize Unicode (NFKC), drop control characters, and collapse whitespace."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub(" ", text)
    text = _SPACES.sub(" ", text)
    text = _SPACES_AROUND_NEWLINE.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def clean_inline(value: object) -> str:
    """Clean a metadata value into a single line ("" for None)."""
    if value is None:
        return ""
    return clean_text(str(value)).replace("\n", " ")


class TextBudget:
    """Collects cleaned text pieces until ``max_chars`` is reached."""

    def __init__(self, max_chars: int) -> None:
        self.remaining = max_chars
        self.truncated = False
        self._parts: list[str] = []

    def add(self, piece: str | None) -> bool:
        """Add a piece of text. Returns False once the budget is used up (stop reading)."""
        if self.remaining <= 0:
            self.truncated = True
            return False
        cleaned = clean_text(piece or "")
        if not cleaned:
            return True
        if len(cleaned) > self.remaining:
            cleaned = cleaned[: self.remaining]
            self.truncated = True
        self._parts.append(cleaned)
        self.remaining -= len(cleaned) + 1  # +1 for the joining newline
        return self.remaining > 0

    @property
    def text(self) -> str:
        return "\n".join(self._parts)


def read_whole(file: BinaryIO, max_bytes: int = MAX_WHOLE_FILE_BYTES) -> bytes:
    """Read a whole file, refusing files larger than ``max_bytes``."""
    data = file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ExtractionError(f"the file is too large to parse (over {max_bytes:,} bytes)")
    return data


def non_empty(**values: object) -> dict[str, str]:
    """Return the cleaned values that aren't empty, for ``Content.metadata``."""
    cleaned = {key: clean_inline(value) for key, value in values.items()}
    return {key: value for key, value in cleaned.items() if value}
