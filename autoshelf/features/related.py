"""Related files (SPEC section 5.4): a file's nearest neighbours across the whole library."""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autoshelf.db import repository
from autoshelf.features.search import folder_label
from autoshelf.layout import Zone, zone_of
from autoshelf.ml.vectors import from_blob

RELATED_COUNT = 5


@dataclass(frozen=True)
class Related:
    """A file similar to the one being looked at."""

    path: Path
    folder: str
    similarity: float


def related_files(
    conn: sqlite3.Connection, root: Path, path: Path, count: int = RELATED_COUNT
) -> list[Related]:
    """The ``count`` files most similar to ``path`` (in your folders or the Inbox).

    Only vectors made by the same model as the file's own are compared.
    """
    model = repository.vector_model(conn, path)
    if model is None:
        return []
    rows = repository.file_vectors(conn, model)
    target = next((vector for _, stored, vector in rows if Path(stored) == path), None)
    if target is None:
        return []
    vector = from_blob(target)
    found = []
    for _, stored, blob in rows:
        other = Path(stored)
        if other == path or zone_of(other, root) not in (Zone.CATEGORY, Zone.INBOX):
            continue
        if not os.path.isfile(other):
            continue
        found.append(Related(other, folder_label(other, root), float(from_blob(blob) @ vector)))
    found.sort(key=lambda item: item.similarity, reverse=True)
    return found[:count]


def similarity_matrix(vectors: list[np.ndarray]) -> np.ndarray:
    """Cosine similarities between unit vectors, as a square matrix."""
    if not vectors:
        return np.empty((0, 0), dtype=np.float32)
    matrix = np.stack(vectors)
    return matrix @ matrix.T
