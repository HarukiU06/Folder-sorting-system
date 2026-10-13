"""Suggest new folders (SPEC section 5.8): files waiting in the Inbox about a topic no
folder covers yet.

Waiting files are grouped by similarity (average-linkage clustering of their vectors).
A group of at least ``MIN_FILES`` files whose best existing folder scores below the
threshold becomes a suggestion, named from the words its files share (or by the optional
LLM). The folder is created only when the user clicks Create; AutoShelf never makes
folders on its own.
"""

import math
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autoshelf.actions.inbox import waiting_files
from autoshelf.config import DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.extract import extract
from autoshelf.extract.filename import filename_tokens
from autoshelf.layout import RESERVED_PREFIX
from autoshelf.llm.base import Message, Provider
from autoshelf.ml.explain import terms
from autoshelf.ml.library import LibraryIndex
from autoshelf.ml.vectors import normalize

MIN_FILES = 3
# Average similarity that holds a group together: files in one folder have a median of
# ~0.42 with the default model, files in different folders ~0.10.
CLUSTER_SIMILARITY = 0.40
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class FolderNameError(Exception):
    """A folder can't be created with that name."""


@dataclass(frozen=True)
class FolderSuggestion:
    """A group of waiting files and a proposed folder name for them."""

    name: str
    paths: tuple[Path, ...]
    keywords: tuple[str, ...]
    best_existing: str | None  # the closest existing folder, if any
    best_score: float


def clusters(vectors: np.ndarray, threshold: float = CLUSTER_SIMILARITY) -> list[list[int]]:
    """Average-linkage clusters: groups are merged while their mean similarity >= threshold."""
    groups = [[i] for i in range(len(vectors))]
    if len(vectors) < 2:
        return groups
    similarity = vectors @ vectors.T
    while True:
        best, pair = threshold, None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                mean = float(similarity[np.ix_(groups[a], groups[b])].mean())
                if mean >= best:
                    best, pair = mean, (a, b)
        if pair is None:
            return groups
        a, b = pair
        groups[a] = groups[a] + groups[b]
        del groups[b]


def name_from_keywords(
    texts: list[str], others: list[str], count: int = 2
) -> tuple[str, list[str]]:
    """A folder name from the words most typical of ``texts`` compared with ``others``."""
    documents = [set(terms(text)) for text in texts]
    background = [set(terms(text)) for text in others]
    in_group = Counter(word for words in documents for word in words)
    total = len(documents) + len(background)

    def score(word: str) -> float:
        everywhere = in_group[word] + sum(1 for words in background if word in words)
        return in_group[word] * math.log((total + 1) / (everywhere + 1) + 1)

    candidates = [word for word, seen in in_group.items() if seen >= max(2, len(texts) // 2)]
    # Words found in more of the group's files first, then the most telling ones.
    ranked = sorted(candidates, key=lambda word: (-in_group[word], -score(word), word))
    keywords = ranked[:5]
    name = " ".join(word if not word.isascii() else word.capitalize() for word in keywords[:count])
    return name, keywords


def _llm_name(provider: Provider, titles: list[str]) -> str:
    system = (
        "You name folders in a personal file library. Reply with one short folder name "
        "(1 to 3 words) for the files listed, in the files' language, without quotes."
    )
    reply = provider.complete(system, [Message("user", "\n".join(titles[:20]))], max_tokens=20)
    return reply.strip().splitlines()[0].strip().strip("\"'`") if reply.strip() else ""


def suggest_folders(
    conn: sqlite3.Connection,
    root: Path,
    library: LibraryIndex,
    *,
    provider: Provider | None = None,
    threshold: float = DEFAULT_AUTO_THRESHOLD,
    ignore_patterns=DEFAULT_IGNORE_PATTERNS,
    min_files: int = MIN_FILES,
) -> list[FolderSuggestion]:
    """Groups of waiting files that no existing folder fits, with a proposed name."""
    items = []
    for path in waiting_files(root, ignore_patterns):
        vector = library.stored_vector(path)
        record = repository.get_file_by_path(conn, path)
        title, excerpt = repository.get_file_content(conn, record.id) if record else (None, None)
        if vector is None:  # not read yet (e.g. it arrived while AutoShelf was closed)
            try:
                extraction = extract(path)
            except OSError:
                continue
            vector = library.vectorize(extraction, path.name)
            title, excerpt = extraction.title, extraction.text[:1000]
        text = " ".join(
            part for part in (" ".join(filename_tokens(path.name)), title, excerpt) if part
        )
        items.append((path, vector, text, title or path.stem))
    if len(items) < min_files:
        return []
    vectors = np.stack([vector for _, vector, _, _ in items])
    suggestions = []
    for group in clusters(vectors):
        if len(group) < min_files:
            continue
        centroid = normalize(vectors[group].mean(axis=0))
        classification = library.classify(centroid, threshold=threshold)
        best = classification.scores[0] if classification.scores else None
        if best is not None and best.score >= threshold:
            continue  # an existing folder already covers this topic
        texts = [items[i][2] for i in group]
        others = [item[2] for j, item in enumerate(items) if j not in group]
        name, keywords = name_from_keywords(texts, others)
        if provider is not None:
            name = _llm_name(provider, [items[i][3] for i in group]) or name
        suggestions.append(
            FolderSuggestion(
                name=clean_folder_name(name) if name else "New folder",
                paths=tuple(items[i][0] for i in group),
                keywords=tuple(keywords),
                best_existing=best.folder if best else None,
                best_score=best.score if best else 0.0,
            )
        )
    suggestions.sort(key=lambda item: -len(item.paths))
    return suggestions


def clean_folder_name(name: str) -> str:
    """A name made safe for a folder: no invalid characters, no leading "_" or dots."""
    name = " ".join(_INVALID_NAME.sub(" ", name).split()).strip(" .")
    return name.lstrip(RESERVED_PREFIX + " ")[:60].strip(" .")


def create_folder(root: Path, name: str, parent: str = "") -> str:
    """Create a category folder the user asked for and return its key.

    Only ever called when the user clicked Create (AutoShelf never creates folders by
    itself). Refuses reserved names and names that already exist.
    """
    cleaned = clean_folder_name(name)
    if not cleaned or cleaned != name.strip():
        raise FolderNameError(
            f"{name!r} can't be a folder name" + (f"; try {cleaned!r}" if cleaned else "")
        )
    base = root.joinpath(*parent.split("/")) if parent else root
    if not base.is_dir():
        raise FolderNameError(f"There is no folder {parent!r}")
    target = base / cleaned
    if target.exists():
        raise FolderNameError(f"{cleaned!r} already exists")
    target.mkdir()
    return "/".join([*([parent] if parent else []), cleaned])
