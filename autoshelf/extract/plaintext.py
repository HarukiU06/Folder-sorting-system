"""Plain text files: notes, Markdown, code, CSV, JSON and similar."""

import codecs
import locale
from pathlib import Path
from typing import BinaryIO

from autoshelf.extract.base import Content, TextBudget

_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
# Tried in order after UTF-8; the system code page (e.g. cp932 on Japanese Windows) first.
LEGACY_ENCODINGS = tuple(dict.fromkeys((locale.getencoding(), "cp1252")))
_SNIFF_BYTES = 4096


def decode_text(data: bytes, *, complete: bool = True) -> str:
    """Decode bytes using a BOM if present, else UTF-8, else the legacy code pages.

    With ``complete=False`` a multi-byte character cut off at the end is dropped instead
    of failing the whole decode (used when only a prefix of the file was read).
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return _decode(data[len(bom) :], encoding, complete, errors="replace")
    for encoding in ("utf-8", *LEGACY_ENCODINGS):
        try:
            return _decode(data, encoding, complete, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1")


def _decode(data: bytes, encoding: str, complete: bool, errors: str) -> str:
    decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
    return decoder.decode(data, final=complete)


def read_text(file: BinaryIO, max_chars: int) -> tuple[str, bool]:
    """Read and decode the start of a text file. Returns (text, was_cut_short)."""
    limit = max_chars * 4 + 4  # enough bytes for max_chars even in UTF-32
    data = file.read(limit + 1)
    cut = len(data) > limit
    return decode_text(data[:limit], complete=not cut), cut


def looks_like_text(path: Path) -> bool:
    """Guess whether a file with an unknown extension is plain text."""
    with open(path, "rb") as file:
        data = file.read(_SNIFF_BYTES)
    if not data or b"\x00" in data:
        return False
    try:
        _decode(data, "utf-8", complete=len(data) < _SNIFF_BYTES, errors="strict")
    except UnicodeDecodeError:
        return False
    return True


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read the start of a text file."""
    text, cut = read_text(file, max_chars)
    budget = TextBudget(max_chars)
    budget.add(text)
    return Content(text=budget.text, truncated=budget.truncated or cut)
