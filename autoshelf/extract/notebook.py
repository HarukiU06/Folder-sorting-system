"""Jupyter notebooks (.ipynb): markdown and code cells, without outputs."""

import json
import re
from typing import BinaryIO

from autoshelf.extract.base import Content, TextBudget, non_empty, read_whole
from autoshelf.extract.plaintext import decode_text

_HEADING = re.compile(r"^\s*#{1,6}\s+(.+)$", re.MULTILINE)


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read cell sources in order; the first markdown heading becomes the title."""
    notebook = json.loads(decode_text(read_whole(file)))
    budget = TextBudget(max_chars)
    title = None
    for cell in notebook.get("cells", []):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        if title is None and cell.get("cell_type") == "markdown":
            heading = _HEADING.search(text)
            title = heading.group(1) if heading else None
        if not budget.add(text):
            break
    kernel = notebook.get("metadata", {}).get("kernelspec", {})
    return Content(
        text=budget.text,
        title=title,
        metadata=non_empty(language=kernel.get("language")),
        truncated=budget.truncated,
    )
