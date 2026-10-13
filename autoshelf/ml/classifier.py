"""Score category folders for a file and decide: move it, or ask (SPEC section 4.2).

A folder's score mixes two cosine similarities with the file's vector:

- its *profile*: the folder's path ("Machine Learning / Unsupervised") plus the
  description the user wrote for it;
- its *neighbours*: the mean of the ``NEIGHBORS`` most similar files already inside it
  (subfolders included).

Neighbours get more weight as a folder fills up, so an empty folder is judged by its
name and description alone. Corrections can lower a folder's score (``ml.learned``).
The decision rule is in ``choose``.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD
from autoshelf.ml.learned import Adjustment

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
    learned: float = 0.0  # added from past corrections (zero or negative)

    def to_details(self) -> dict:
        """A JSON-ready summary: the score and the parts it was made of."""
        details = {"folder": self.folder, "score": round(self.score, 4)}
        details["name"] = round(self.profile_similarity, 4)
        if self.neighbor_similarity is not None:
            details["files"] = round(self.neighbor_similarity, 4)
        if self.learned:
            details["learned"] = round(self.learned, 4)
        return details


@dataclass(frozen=True)
class Classification:
    """Every folder's score (best first) and the folder to move to, if any."""

    scores: tuple[FolderScore, ...]
    choice: FolderScore | None
    explanation: str
    threshold: float = DEFAULT_AUTO_THRESHOLD
    margin: float = DEFAULT_AUTO_MARGIN
    learned: Mapping[str, Adjustment] = field(default_factory=dict)  # penalties, by folder

    def top(self, count: int = 3) -> tuple[FolderScore, ...]:
        """The ``count`` best-scoring folders."""
        return self.scores[:count]

    def to_details(self) -> dict:
        """A JSON-ready summary for the history table."""
        return {
            "candidates": [item.to_details() for item in self.top()],
            "choice": self.choice.folder if self.choice else None,
            "explanation": self.explanation,
        }

    def score_of(self, folder: str) -> FolderScore | None:
        """The score of one folder (any case)."""
        wanted = folder.casefold()
        return next((item for item in self.scores if item.folder.casefold() == wanted), None)


def score_folders(
    vector: np.ndarray,
    profiles: Iterable[FolderProfile],
    *,
    adjustments: Mapping[str, float] | None = None,
    exclude: Path | None = None,
) -> list[FolderScore]:
    """Score every folder for ``vector``, best first.

    ``adjustments`` adds a per-folder amount, keyed by casefolded folder key (learned
    from corrections). ``exclude`` leaves one file out of the neighbour pools, so a file
    already in a folder can be classified without matching itself.
    """
    adjustments = adjustments or {}
    scores = [
        _score(vector, profile, adjustments.get(profile.key.casefold(), 0.0), exclude)
        for profile in profiles
    ]
    scores.sort(key=lambda item: item.score, reverse=True)
    return scores


def _score(
    vector: np.ndarray, profile: FolderProfile, learned: float, exclude: Path | None
) -> FolderScore:
    profile_similarity = float(profile.vector @ vector)
    pool, paths = profile.pool, profile.pool_paths
    if exclude is not None and exclude in paths:
        keep = [i for i, path in enumerate(paths) if path != exclude]
        pool, paths = pool[keep], tuple(paths[i] for i in keep)
    if len(pool) == 0:
        score = profile_similarity + learned
        return FolderScore(profile.key, score, profile_similarity, learned=learned)

    similarities = pool @ vector
    nearest = np.argsort(-similarities)[:NEIGHBORS]
    neighbor_similarity = float(similarities[nearest].mean())
    weight = NEIGHBOR_WEIGHT * min(len(pool), FULL_POOL) / FULL_POOL
    score = (1 - weight) * profile_similarity + weight * neighbor_similarity + learned
    neighbors = tuple(Neighbor(paths[i], float(similarities[i])) for i in nearest)
    return FolderScore(
        profile.key, score, profile_similarity, neighbor_similarity, neighbors, learned
    )


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
    clear match. If a subfolder is good enough but too close to another folder (e.g.
    two sibling subfolders fit equally), the user is asked rather than the file being
    put in the parent. A parent is chosen only when its subfolders all score too low.
    """
    ranked = tuple(sorted(scores, key=lambda item: item.score, reverse=True))
    if not ranked:
        return Classification(
            ranked, None, "there are no folders to sort into yet", threshold, margin
        )

    qualifying = [item for item in ranked if _qualifies(item, ranked, threshold, margin)]
    if qualifying:
        choice = max(qualifying, key=lambda item: (depth(item.folder), item.score))
        contested = _contested_subfolder(choice, ranked, threshold)
        if contested is None:
            rival = _best_rival(choice, ranked)
            versus = f", next {rival.folder!r} {rival.score:.2f}" if rival else ""
            explanation = f"best match {choice.folder!r} {choice.score:.2f}{versus}"
            return Classification(ranked, choice, explanation, threshold, margin)
        return Classification(ranked, None, _too_close(contested, ranked), threshold, margin)

    best = ranked[0]
    if best.score < threshold:
        explanation = (
            f"not sure: best match {best.folder!r} scored {best.score:.2f}, "
            f"below the {threshold:.2f} needed to move it automatically"
        )
    else:
        explanation = _too_close(best, ranked)
    return Classification(ranked, None, explanation, threshold, margin)


def _is_below(item: FolderScore, folder: str) -> bool:
    return item.folder.casefold().startswith(folder.casefold() + "/")


def _contested_subfolder(
    choice: FolderScore, ranked: Sequence[FolderScore], threshold: float
) -> FolderScore | None:
    """A subfolder of ``choice`` that is good enough but lost on the margin, if any.

    Such a file fits two folders about equally (usually two sibling subfolders), so
    the user is asked instead of the file going into the parent.
    """
    return next(
        (item for item in ranked if _is_below(item, choice.folder) and item.score >= threshold),
        None,
    )


def _too_close(item: FolderScore, ranked: Sequence[FolderScore]) -> str:
    rival = _best_rival(item, ranked)
    return (
        f"not sure: {item.folder!r} ({item.score:.2f}) is too close to "
        f"{rival.folder!r} ({rival.score:.2f})"
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
