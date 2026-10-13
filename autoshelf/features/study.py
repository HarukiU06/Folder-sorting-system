"""Study mode (SPEC section 5.11), with the optional LLM: a reading list for a folder, and
flashcards from a file or folder that can be reviewed in the app or exported for Anki."""

import csv
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.llm.base import LLMError, Message, Provider, clip

CARD_INPUT_CHARS = 12_000
LIST_EXCERPT_CHARS = 600
MAX_LIST_FILES = 40
_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@dataclass(frozen=True)
class ReadingItem:
    """One file of a reading list, with why it comes at this point."""

    path: Path
    reason: str


@dataclass(frozen=True)
class Card:
    """A flashcard."""

    question: str
    answer: str


def json_array(reply: str) -> list:
    """The first JSON array in an LLM reply (it may be wrapped in text or a code block)."""
    match = _JSON_ARRAY.search(reply)
    if match is None:
        raise LLMError("The AI's answer had no list in it; try again")
    try:
        value = json.loads(match.group())
    except json.JSONDecodeError as exc:
        raise LLMError(f"The AI's answer wasn't a valid list ({exc.msg}); try again") from exc
    if not isinstance(value, list):
        raise LLMError("The AI's answer wasn't a list; try again")
    return value


def reading_list(
    provider: Provider, files: list[tuple[Path, str | None, str | None]]
) -> list[ReadingItem]:
    """Order files (path, title, text excerpt) from introductory to advanced."""
    files = files[:MAX_LIST_FILES]
    if not files:
        raise LLMError("There are no files to order")
    system = (
        "You plan study order for a student. Order the numbered documents from the most "
        "introductory to the most advanced. Reply with only a JSON array like "
        '[{"n": 2, "why": "short reason"}, ...] that contains every number exactly once. '
        "Write the reasons in the documents' language."
    )
    listing = "\n\n".join(
        f"{number}. {path.name}"
        + (f" - {title}" if title else "")
        + (f"\n{clip(excerpt, LIST_EXCERPT_CHARS)}" if excerpt else "")
        for number, (path, title, excerpt) in enumerate(files, start=1)
    )
    reply = provider.complete(system, [Message("user", listing)], max_tokens=1500)
    ordered: list[ReadingItem] = []
    used: set[int] = set()
    for item in json_array(reply):
        number = item.get("n") if isinstance(item, dict) else item
        if isinstance(number, int) and 1 <= number <= len(files) and number not in used:
            used.add(number)
            reason = str(item.get("why", "")) if isinstance(item, dict) else ""
            ordered.append(ReadingItem(files[number - 1][0], reason))
    # Anything the AI left out keeps its place at the end.
    ordered.extend(
        ReadingItem(path, "") for number, (path, _, _) in enumerate(files, 1) if number not in used
    )
    return ordered


def make_cards(provider: Provider, title: str, text: str, count: int = 10) -> list[Card]:
    """Question-and-answer cards about a text."""
    if not text.strip():
        raise LLMError(f"{title} has no text to make cards from")
    system = (
        f"You write study flashcards. Write {count} cards that test the key ideas of the "
        "text: a precise question and a short answer, in the text's language. Reply with "
        'only a JSON array like [{"q": "question", "a": "answer"}, ...].'
    )
    prompt = f"Title: {title}\n\n{clip(text, CARD_INPUT_CHARS)}"
    reply = provider.complete(system, [Message("user", prompt)], max_tokens=2500)
    cards = [
        Card(str(item["q"]).strip(), str(item["a"]).strip())
        for item in json_array(reply)
        if isinstance(item, dict) and item.get("q") and item.get("a")
    ]
    if not cards:
        raise LLMError("The AI made no usable cards; try again")
    return cards[:count]


def cards_for_file(
    conn: sqlite3.Connection,
    provider: Provider,
    path: Path,
    folder: str | None,
    count: int = 10,
    extractor: Callable[[Path], Extraction] = extract,
) -> list[repository.Flashcard]:
    """Make cards from one file and store them (with the file's folder)."""
    extraction = extractor(path)
    cards = make_cards(provider, extraction.title or path.stem, extraction.text, count)
    record = repository.get_file_by_path(conn, path)
    ids = repository.add_flashcards(
        conn,
        [(card.question, card.answer) for card in cards],
        file_id=record.id if record else None,
        folder=folder,
    )
    stored = {card.id: card for card in repository.list_flashcards(conn)}
    return [stored[card_id] for card_id in ids]


def export_csv(cards: list[repository.Flashcard], target: Path) -> int:
    """Write cards to a new CSV file Anki can import (front, back). Never overwrites."""
    if target.exists():
        raise FileExistsError(f"{target} already exists; choose another name")
    with open(target, "x", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        for card in cards:
            writer.writerow([card.question, card.answer])
    return len(cards)
