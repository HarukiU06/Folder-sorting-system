"""EPUB e-books via ebooklib."""

from typing import BinaryIO

import ebooklib
from ebooklib import epub

from autoshelf.extract.base import Content, TextBudget, check_zip_container, non_empty
from autoshelf.extract.webpage import html_to_text


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read chapters in reading (spine) order, plus Dublin Core title/creator/subject."""
    check_zip_container(file)
    book = epub.read_epub(file, options={"ignore_ncx": True})
    budget = TextBudget(max_chars)
    for item_id, _linear in book.spine:
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if not budget.add(html_to_text(item.get_content())):
            break
    return Content(
        text=budget.text,
        title=_first(book, "title"),
        metadata=non_empty(
            author=_first(book, "creator"),
            subject=_first(book, "subject"),
            description=_first(book, "description"),
        ),
        truncated=budget.truncated,
    )


def _first(book: epub.EpubBook, name: str) -> str | None:
    values = book.get_metadata("DC", name)
    return values[0][0] if values else None
