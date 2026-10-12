"""Score category folders for a file and decide: move it, or ask (SPEC section 4.2).

A folder's score mixes two cosine similarities with the file's vector:

- its *profile*: the folder's path ("Machine Learning / Unsupervised") plus the
  description the user wrote for it;
- its *neighbours*: the mean of the ``NEIGHBORS`` most similar files already inside it
  (subfolders included).

Neighbours get more weight as a folder fills up, so an empty folder is judged by its
name and description alone. The decision rule is in ``choose``.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD

NEIGHBORS = 3  # k in the top-k mean of neighbour similarities
FULL_POOL = 3  # files a folder needs before its neighbours get their full weight
NEIGHBOR_WEIGHT = 0.6  # weight of neighbour similarity in a folder with FULL_POOL files


@dataclass(frozen=True)
class FolderProfile:
    """What a folder is compared on: its profile vector and the files inside it."""

    key: str  # relative path with "/" separators, e.g. "Machine Learning/Unsupervised"
    vector: np.ndarray
    pool: np.ndarray  # shape (n, dim): vectors of the files in the folder and below it
    pool_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Neighbor:
    """A file inside a folder and how similar it is to the file being classified."""

    path: Path
    similarity: float


@dataclass(frozen=True)
class FolderScore:
    """How well a file fits one folder."""

    folder: str
    score: float
    profile_similarity: float
    neighbor_similarity: float | None = None
    neighbors: tuple[Neighbor, ...] = ()


@dataclass(frozen=True)
class Classification:
    """Every folder's score (best first) and the folder to move to, if any."""

    scores: tuple[FolderScore, ...]
    choice: FolderScore | None
    explanation: str
    threshold: float = DEFAULT_AUTO_THRESHOLD
    margin: float = DEFAULT_AUTO_MARGIN

    def top(self, count: int = 3) -> tuple[FolderScore, ...]:
        """The ``count`` best-scoring folders."""
        return self.scores[:count]

    def to_details(self) -> dict:
        """A JSON-ready summary for the history table."""
        return {
            "candidates": [
                {"folder": item.folder, "score": round(item.score, 4)} for item in self.top()
            ],
            "explanation": self.explanation,
        }


def score_folders(
    vector: np.ndarray,
    profiles: Iterable[FolderProfile],
    *,
    boosts: Mapping[str, float] | None = None,
    exclude: Path | None = None,
) -> list[FolderScore]:
    """Score every folder for ``vector``, best first.

    ``boosts`` adds a per-folder amount (learned from corrections in Phase 5).
    ``exclude`` leaves one file out of the neighbour pools, so a file already in a
    folder can be classified without matching itself.
    """
    boosts = boosts or {}
    scores = [
        _score(vector, profile, boosts.get(profile.key, 0.0), exclude) for profile in profiles
    ]
    scores.sort(key=lambda item: item.score, reverse=True)
    return scores


def _score(
    vector: np.ndarray, profile: FolderProfile, boost: float, exclude: Path | None
) -> FolderScore:
    profile_similarity = float(profile.vector @ vector)
    pool, paths = profile.pool, profile.pool_paths
    if exclude is not None and exclude in paths:
        keep = [i for i, path in enumerate(paths) if path != exclude]
        pool, paths = pool[keep], tuple(paths[i] for i in keep)
    if len(pool) == 0:
        return FolderScore(profile.key, profile_similarity + boost, profile_similarity)

    similarities = pool @ vector
    nearest = np.argsort(-similarities)[:NEIGHBORS]
    neighbor_similarity = float(similarities[nearest].mean())
    weight = NEIGHBOR_WEIGHT * min(len(pool), FULL_POOL) / FULL_POOL
    score = (1 - weight) * profile_similarity + weight * neighbor_similarity + boost
    neighbors = tuple(Neighbor(paths[i], float(similarities[i])) for i in nearest)
    return FolderScore(profile.key, score, profile_similarity, neighbor_similarity, neighbors)


def is_related(a: str, b: str) -> bool:
    """True if folder ``a`` is ``b``, or one contains the other."""
    a, b = a.casefold(), b.casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def depth(folder: str) -> int:
    """Nesting depth of a folder key: "A" is 1, "A/B" is 2."""
    return folder.count("/") + 1


def choose(
    scores: Sequence[FolderScore],
    threshold: float = DEFAULT_AUTO_THRESHOLD,
    margin: float = DEFAULT_AUTO_MARGIN,
) -> Classification:
    """Pick the folder to move to, or None to ask the user.

    A folder qualifies when its score reaches ``threshold`` and beats every *unrelated*
    folder by at least ``margin``. A folder's own parents and subfolders are not
    rivals: they naturally score alike. Qualifying folders always lie on one branch,
    and the deepest one wins, so a file goes to the most specific folder that is a
    clear match, or to its parent when the subfolders are too close to call.
    """
    ranked = tuple(sorted(scores, key=lambda item: item.score, reverse=True))
    if not ranked:
        return Classification(
            ranked, None, "there are no folders to sort into yet", threshold, margin
        )

    qualifying = [item for item in ranked if _qualifies(item, ranked, threshold, margin)]
    if qualifying:
        choice = max(qualifying, key=lambda item: (depth(item.folder), item.score))
        rival = _best_rival(choice, ranked)
        versus = f", next {rival.folder!r} {rival.score:.2f}" if rival else ""
        explanation = f"best match {choice.folder!r} {choice.score:.2f}{versus}"
        explanation += _why_not_deeper(choice, ranked, threshold)
        return Classification(ranked, choice, explanation, threshold, margin)

    best = ranked[0]
    if best.score < threshold:
        explanation = (
            f"not sure: best match {best.folder!r} scored {best.score:.2f}, "
            f"below the {threshold:.2f} needed to move it automatically"
        )
    else:
        rival = _best_rival(best, ranked)
        explanation = (
            f"not sure: {best.folder!r} ({best.score:.2f}) is too close to "
            f"{rival.folder!r} ({rival.score:.2f})"
        )
    return Classification(ranked, None, explanation, threshold, margin)


def _why_not_deeper(choice: FolderScore, ranked: Sequence[FolderScore], threshold: float) -> str:
    """Explain why a subfolder that scored higher than ``choice`` wasn't picked."""
    best = ranked[0]
    if best is choice or not best.folder.casefold().startswith(choice.folder.casefold() + "/"):
        return ""
    if best.score < threshold:
        return f"; its subfolder {best.folder!r} only scored {best.score:.2f}"
    rival = _best_rival(best, ranked)
    return (
        f"; its subfolders {best.folder!r} ({best.score:.2f}) and {rival.folder!r} "
        f"({rival.score:.2f}) are too close to choose between"
    )


def _qualifies(
    item: FolderScore, ranked: Sequence[FolderScore], threshold: float, margin: float
) -> bool:
    if item.score < threshold:
        return False
    rival = _best_rival(item, ranked)
    return rival is None or item.score - rival.score >= margin


def _best_rival(item: FolderScore, ranked: Sequence[FolderScore]) -> FolderScore | None:
    """The best-scoring folder that is neither ``item`` nor its parent or subfolder."""
    return next((other for other in ranked if not is_related(other.folder, item.folder)), None)
