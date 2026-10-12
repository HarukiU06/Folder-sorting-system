"""PDF text and document info via PyMuPDF."""

from typing import BinaryIO

import pymupdf

from autoshelf.extract.base import Content, ExtractionError, TextBudget, non_empty, read_whole

MAX_PDF_BYTES = 300 * 1024 * 1024

# Malformed PDFs make MuPDF print to stderr; failures still surface as exceptions.
pymupdf.TOOLS.mupdf_display_errors(False)


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read page text in order until ``max_chars``, plus title/author/subject/keywords."""
    # PyMuPDF needs the bytes in memory (it can't take a file object).
    with pymupdf.open(stream=read_whole(file, MAX_PDF_BYTES), filetype="pdf") as doc:
        if doc.needs_pass:
            raise ExtractionError("the PDF is password-protected")
        info = doc.metadata or {}
        budget = TextBudget(max_chars)
        for page in doc:
            if not budget.add(page.get_text()):
                break
    return Content(
        text=budget.text,
        title=info.get("title") or None,
        metadata=non_empty(
            author=info.get("author"), subject=info.get("subject"), keywords=info.get("keywords")
        ),
        truncated=budget.truncated,
    )
