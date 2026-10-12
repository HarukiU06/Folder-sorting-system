"""Word documents (.docx) via python-docx."""

from typing import BinaryIO

import docx
from docx.table import Table

from autoshelf.extract.base import Content, TextBudget, non_empty


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read paragraphs and tables in document order, plus the core properties."""
    document = docx.Document(file)
    budget = TextBudget(max_chars)
    for block in document.iter_inner_content():
        text = _table_text(block) if isinstance(block, Table) else block.text
        if not budget.add(text):
            break
    props = document.core_properties
    return Content(
        text=budget.text,
        title=props.title or None,
        metadata=non_empty(
            author=props.author,
            subject=props.subject,
            keywords=props.keywords,
            category=props.category,
            comments=props.comments,
        ),
        truncated=budget.truncated,
    )


def _table_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = cell.text.strip()
            if text and (not cells or cells[-1] != text):  # merged cells repeat their text
                cells.append(text)
        rows.append(" | ".join(cells))
    return "\n".join(rows)
