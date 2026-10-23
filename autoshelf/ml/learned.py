"""Learning from corrections (SPEC section 4.6): a small, explainable score penalty.

When the user moves a file away from folder A (the folder AutoShelf chose or suggested),
the file's vector is kept as an example. A later file with similar content then scores
a little lower for A, and more so the more such corrections there are. Nothing else is
needed: the corrected file now sits in its right folder, where it already counts as an
example for that folder's neighbours.
"""

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

# Similarities of file vectors with the default model: files in the same folder have a
# median of ~0.42, files in different folders ~0.10 (90% below 0.32). A correction starts
# to count above SIMILAR_FROM and counts fully from SIMILAR_FULL (near-identical files).
# In scripts/benchmark.py's simulation (files arriving one by one, mistakes corrected), the
# penalty made sorting slightly more cautious at every value tried (0.25/0.50 to 0.40/0.80):
# a little fewer wrong moves, a little more waiting in the Inbox. The corrected files, now
# in their right folders, already teach most habits as neighbours.
SIMILAR_FROM = 0.35
SIMILAR_FULL = 0.70
PENALTY_PER_EXAMPLE = 0.08
MAX_PENALTY = 0.20


@dataclass(frozen=True)
class Example:
    """A file the user moved from ``from_folder`` to ``to_folder``."""

    from_folder: str
    to_folder: str
    vector: np.ndarray
    name: str = ""


@dataclass(frozen=True)
class Evidence:
    """One past correction that made a folder score lower."""

    name: str
    to_folder: str
    similarity: float


@dataclass(frozen=True)
class Adjustment:
    """How much a folder's score was lowered, and because of which corrections."""

    folder: str
    amount: float  # negative
    evidence: tuple[Evidence, ...]


def weight(similarity: float) -> float:
    """How strongly a correction applies to a file this similar to the corrected one."""
    return float(np.clip((similarity - SIMILAR_FROM) / (SIMILAR_FULL - SIMILAR_FROM), 0.0, 1.0))


def adjustments(vector: np.ndarray, examples: Iterable[Example]) -> dict[str, Adjustment]:
    """Penalties per folder (keyed as in the examples) for a file with ``vector``.

    Only folders that files were moved *away* from are lowered, by at most MAX_PENALTY.
    """
    totals: dict[str, float] = {}
    evidence: dict[str, list[Evidence]] = {}
    for example in examples:
        similarity = float(example.vector @ vector)
        strength = weight(similarity)
        if strength <= 0:
            continue
        key = example.from_folder
        totals[key] = totals.get(key, 0.0) + strength
        evidence.setdefault(key, []).append(
            Evidence(example.name, example.to_folder, round(similarity, 4))
        )
    result = {}
    for key, total in totals.items():
        amount = -min(MAX_PENALTY, PENALTY_PER_EXAMPLE * total)
        best = sorted(evidence[key], key=lambda item: item.similarity, reverse=True)
        result[key] = Adjustment(key, amount, tuple(best))
    return result
