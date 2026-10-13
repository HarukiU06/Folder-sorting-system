"""Ask your library (SPEC section 5.10): answers from your own files, with citations.

The question is matched against the stored text chunks (``features.search``); the best
passages are sent to the optional LLM with the instruction to answer only from them and
to cite them as [1], [2], ... Each citation links back to its file.
"""

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from autoshelf.features.search import Passage, SearchIndex
from autoshelf.llm.base import LLMError, Message, Provider, clip
from autoshelf.ml.embedder import Embedder

PASSAGES = 8
PASSAGE_CHARS = 1500
HISTORY_TURNS = 4  # earlier questions and answers kept for follow-ups
SYSTEM = (
    "You answer questions about the user's own files. Use only the numbered excerpts "
    "given with the question. Cite the excerpts you use like [1] or [2][3] right after the "
    "statement they support. If the excerpts don't contain the answer, say so plainly. "
    "Answer in the language of the question, briefly."
)
_CITATION = re.compile(r"\[(\d+)\]")


@dataclass(frozen=True)
class Source:
    """A numbered excerpt the answer may cite."""

    number: int
    path: Path
    folder: str
    text: str


@dataclass(frozen=True)
class Answer:
    """The LLM's answer and the sources it cited (or, if none, the ones it was given)."""

    question: str
    text: str
    sources: tuple[Source, ...]
    cited: tuple[int, ...]


def build_prompt(question: str, sources: list[Source]) -> str:
    """The user message: numbered excerpts, then the question."""
    parts = [
        f"[{source.number}] {source.path.name} (in {source.folder or 'the root'}):\n"
        f"{clip(source.text, PASSAGE_CHARS)}"
        for source in sources
    ]
    return "Excerpts:\n\n" + "\n\n".join(parts) + f"\n\nQuestion: {question}"


def cited_numbers(text: str, count: int) -> tuple[int, ...]:
    """Citation numbers used in an answer, in order of first use (valid ones only)."""
    seen: list[int] = []
    for match in _CITATION.finditer(text):
        number = int(match.group(1))
        if 1 <= number <= count and number not in seen:
            seen.append(number)
    return tuple(seen)


def ask(
    conn: sqlite3.Connection,
    root: Path,
    provider: Provider,
    embedder: Embedder,
    index: SearchIndex,
    question: str,
    history: list[Answer] | None = None,
) -> Answer:
    """Answer a question from the library's files. Raises LLMError."""
    question = question.strip()
    if not question:
        raise LLMError("Ask a question first")
    passages: list[Passage] = index.top_chunks(conn, root, embedder, question, count=PASSAGES)
    if not passages:
        raise LLMError("AutoShelf hasn't read any of your files yet")
    sources = [
        Source(number, passage.path, passage.folder, passage.text)
        for number, passage in enumerate(passages, start=1)
    ]
    messages: list[Message] = []
    for earlier in (history or [])[-HISTORY_TURNS:]:
        messages.append(Message("user", earlier.question))
        messages.append(Message("assistant", earlier.text))
    messages.append(Message("user", build_prompt(question, sources)))
    text = provider.complete(SYSTEM, messages, max_tokens=1200)
    return Answer(question, text, tuple(sources), cited_numbers(text, len(sources)))
