"""Rich Text Format files via striprtf."""

import codecs
import re
from typing import BinaryIO

from striprtf.striprtf import rtf_to_text

from autoshelf.extract.base import Content, TextBudget

MAX_RTF_BYTES = 4 * 1024 * 1024  # embedded pictures make RTF files large; text comes first
_CODE_PAGE = re.compile(rb"\\ansicpg(\d+)")


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Strip RTF markup, decoding ``\\'xx`` escapes with the document's declared code page."""
    data = file.read(MAX_RTF_BYTES + 1)
    budget = TextBudget(max_chars)
    budget.add(
        rtf_to_text(
            data[:MAX_RTF_BYTES].decode("latin-1"), encoding=_encoding(data), errors="replace"
        )
    )
    return Content(text=budget.text, truncated=budget.truncated or len(data) > MAX_RTF_BYTES)


def _encoding(data: bytes) -> str:
    match = _CODE_PAGE.search(data[:4096])
    if match:
        candidate = f"cp{int(match.group(1))}"
        try:
            codecs.lookup(candidate)
            return candidate
        except LookupError:
            pass
    return "cp1252"
