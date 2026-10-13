"""Search the library by meaning and by exact words (SPEC section 5.3).

Every file's text is stored in chunks with a vector each (see ``ml.library.store_vector``).
A query is embedded with the same model and compared with every chunk; a file scores as
its best chunk. Words of the query are also looked up exactly (SQLite FTS5 with trigrams,
or a plain substring search for words shorter than three characters, e.g. 給与). Files
containing all the words come first, then files that only match by meaning.

At hundreds of files, all chunk vectors fit in memory: ``SearchIndex`` keeps them and
reloads only when chunks were added or removed.
"""

import os
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autoshelf.db import repository
from autoshelf.layout import INBOX_DIRNAME, Zone, zone_of
from autoshelf.ml.embedder import Embedder
from autoshelf.ml.vectors import from_blob

KEYWORD_BONUS = 0.25  # added to files that contain every word of the query
MIN_SIMILARITY = 0.25  # files below this (and without the words) are not shown
SNIPPET_CHARS = 220
_SHORT_PHRASE = 3  # FTS trigrams need at least three characters


@dataclass(frozen=True)
class SearchHit:
    """One file that matches a query."""

    path: Path
    folder: str  # "Finance", "_Inbox", or "" for the root
    score: float
    similarity: float  # best chunk's similarity to the query
    keyword: bool  # contains every word of the query
    snippet: str


@dataclass
class _Chunks:
    signature: tuple[int, int]
    ids: np.ndarray
    file_ids: np.ndarray
    texts: list[str]
    matrix: np.ndarray
    paths: dict[int, str]


def query_words(query: str) -> list[str]:
    """The words of a query: quoted phrases stay together."""
    words = []
    for quoted, plain in re.findall(r'"([^"]+)"|(\S+)', query):
        word = (quoted or plain).strip()
        if word:
            words.append(word)
    return words


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def folder_label(path: Path, root: Path) -> str:
    """Where a file is, for display: its folder key, "_Inbox", or "" for the root."""
    zone = zone_of(path, root)
    if zone is Zone.INBOX:
        return INBOX_DIRNAME
    if zone is Zone.CATEGORY:
        return "/".join(path.relative_to(root).parts[:-1])
    return ""


class SearchIndex:
    """Chunk vectors of the library in memory. Thread-safe; reloads when chunks change."""

    def __init__(self) -> None:
        self._cache: dict[str, _Chunks] = {}
        self._lock = threading.Lock()

    def _chunks(self, conn: sqlite3.Connection, model: str) -> _Chunks:
        signature = repository.chunks_signature(conn)
        with self._lock:
            cached = self._cache.get(model)
            if cached is not None and cached.signature == signature:
                return cached
        rows = repository.searchable_chunks(conn, model)
        vectors = [from_blob(row["embedding"]) for row in rows]
        chunks = _Chunks(
            signature=signature,
            ids=np.array([row["id"] for row in rows], dtype=np.int64),
            file_ids=np.array([row["file_id"] for row in rows], dtype=np.int64),
            texts=[row["text"] for row in rows],
            matrix=np.stack(vectors) if vectors else np.empty((0, 0), dtype=np.float32),
            paths={row["file_id"]: row["path"] for row in rows},
        )
        with self._lock:
            self._cache[model] = chunks
        return chunks

    def search(
        self,
        conn: sqlite3.Connection,
        root: Path,
        embedder: Embedder,
        query: str,
        *,
        limit: int = 20,
    ) -> list[SearchHit]:
        """The files under ``root`` that best match ``query``, best first."""
        query = query.strip()
        if not query:
            return []
        chunks = self._chunks(conn, embedder.model_id)
        if len(chunks.ids) == 0:
            return []
        similarities = chunks.matrix @ embedder.embed([query])[0]
        words = query_words(query)
        matching = self._matching_chunks(conn, chunks, words)

        best: dict[int, int] = {}  # file id -> index of its most similar chunk
        for index, file_id in enumerate(chunks.file_ids.tolist()):
            if file_id not in best or similarities[index] > similarities[best[file_id]]:
                best[file_id] = index
        hits = []
        for file_id, index in best.items():
            path = Path(chunks.paths[file_id])
            if zone_of(path, root) not in (Zone.CATEGORY, Zone.INBOX) or not os.path.isfile(path):
                continue
            file_chunks = np.flatnonzero(chunks.file_ids == file_id)
            keyword = bool(words) and all(
                any(int(chunks.ids[i]) in found for i in file_chunks) for found in matching
            )
            similarity = float(similarities[index])
            if similarity < MIN_SIMILARITY and not keyword:
                continue
            snippet_index = index
            if keyword:
                first = matching[0]
                snippet_index = next(i for i in file_chunks if int(chunks.ids[i]) in first)
            hits.append(
                SearchHit(
                    path=path,
                    folder=folder_label(path, root),
                    score=similarity + (KEYWORD_BONUS if keyword else 0.0),
                    similarity=similarity,
                    keyword=keyword,
                    snippet=snippet(chunks.texts[snippet_index], words if keyword else []),
                )
            )
        hits.sort(key=lambda hit: (hit.keyword, hit.score), reverse=True)
        return hits[:limit]

    @staticmethod
    def _matching_chunks(
        conn: sqlite3.Connection, chunks: _Chunks, words: list[str]
    ) -> list[set[int]]:
        """For each word, the ids of the chunks that contain it."""
        found = []
        for word in words:
            if len(word) >= _SHORT_PHRASE:
                try:
                    found.append(repository.chunks_matching(conn, word))
                    continue
                except sqlite3.Error:
                    pass  # fall back to a substring search
            folded = _fold(word)
            found.append(
                {
                    int(chunk_id)
                    for chunk_id, text in zip(chunks.ids, chunks.texts, strict=True)
                    if folded in _fold(text)
                }
            )
        return found


def snippet(text: str, words: list[str], size: int = SNIPPET_CHARS) -> str:
    """A short piece of ``text``, around the first of ``words`` it contains."""
    flat = " ".join(text.split())
    folded = _fold(flat)
    start = 0
    for word in words:
        position = folded.find(_fold(word))
        if position >= 0:
            start = max(0, position - size // 3)
            break
    piece = flat[start : start + size]
    return ("..." if start > 0 else "") + piece + ("..." if start + size < len(flat) else "")
