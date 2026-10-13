"""Short summaries of files (SPEC section 5.5), made by the optional LLM.

A summary is made only when asked for, then cached in the database (and dropped when
the file's content changes), so the same file is never sent twice.
"""

import sqlite3
from collections.abc import Callable
from pathlib import Path

from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.llm.base import LLMError, Message, Provider, clip

SUMMARY_INPUT_CHARS = 8000  # at most this much of a file's text is sent
SYSTEM = (
    "You summarize documents for a personal file library. Write 2 or 3 short sentences "
    "saying what the document is and what it covers, in the document's own language. "
    "No preamble, no lists, no markdown."
)


def summarize_text(provider: Provider, name: str, title: str | None, text: str) -> str:
    """A 2-3 sentence summary of a document."""
    if not (text.strip() or title):
        raise LLMError(f"{name} has no text to summarize")
    prompt = f"File name: {name}\n"
    if title:
        prompt += f"Title: {title}\n"
    prompt += f"\nText:\n{clip(text, SUMMARY_INPUT_CHARS)}"
    return " ".join(provider.complete(SYSTEM, [Message("user", prompt)], max_tokens=400).split())


def file_summary(
    conn: sqlite3.Connection,
    provider: Provider,
    path: Path,
    *,
    refresh: bool = False,
    extractor: Callable[[Path], Extraction] = extract,
) -> str:
    """The cached summary of a file, made now if there is none (or ``refresh``)."""
    record = repository.get_file_by_path(conn, path)
    if record is not None and not refresh:
        cached = repository.get_summary(conn, record.id)
        if cached:
            return cached
    extraction = extractor(path)
    summary = summarize_text(provider, path.name, extraction.title, extraction.text)
    if record is not None:
        repository.set_summary(conn, record.id, summary)
    return summary
