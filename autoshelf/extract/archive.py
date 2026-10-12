"""ZIP archives: the names of the files inside (contents are not unpacked)."""

import zipfile
from typing import BinaryIO

from autoshelf.extract.base import Content, TextBudget, non_empty
from autoshelf.extract.plaintext import LEGACY_ENCODINGS

MAX_MEMBERS = 500


def extract(file: BinaryIO, max_chars: int) -> Content:
    """List member names (folders and files) and the archive comment."""
    # Names without the UTF-8 flag are usually in the system code page (e.g. cp932).
    with zipfile.ZipFile(file, metadata_encoding=LEGACY_ENCODINGS[0]) as archive:
        names = archive.namelist()
        comment = archive.comment.decode("utf-8", errors="replace")
    budget = TextBudget(max_chars)
    for name in names[:MAX_MEMBERS]:
        if not budget.add(name.replace("/", " / ")):
            break
    return Content(
        text=budget.text,
        metadata=non_empty(comment=comment),
        truncated=budget.truncated or len(names) > MAX_MEMBERS,
    )
