"""How well would AutoShelf sort this library? A leave-one-out test.

Every file already in a folder is classified as if it had just arrived: it is left out of
its folder's examples, and out of the corrections learned from it, and the folder chosen
is compared with the folder it is actually in. Each file is scored once; the decision rule
(``classifier.choose``) is then applied at every confidence level, so the levels can be
compared by how many files they would move to the right folder, leave in the Inbox, or
move to a wrong folder. The result recommends the most automatic level that rarely moves a
file to a wrong folder.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from autoshelf.config import CONFIDENCE_LEVELS, ConfidenceLevel
from autoshelf.ml.classifier import (
    FolderProfile,
    FolderScore,
    choose,
    depth,
    is_related,
    score_folders,
)
from autoshelf.ml.learned import Example, adjustments
from autoshelf.ml.library import CONTENT, IndexedFile, LibraryIndex

MIN_FILES = 10  # fewer files than this say little about how sorting will go
MIN_FOLDERS = 2
MAX_WRONG = 0.05  # the recommended level moves at most this share to a wrong folder
_SAME_FILE = 0.999  # a learning example this similar to a file is that file itself

Progress = Callable[[int, int], None]


@dataclass(frozen=True)
class Trial:
    """One file classified as if new: its folder, AutoShelf's best guess (``best_guess``),
    and the folder chosen at each confidence level (None: it would wait in the Inbox)."""

    path: Path
    folder: str
    best: str | None
    choices: tuple[str | None, ...]


@dataclass(frozen=True)
class LevelResult:
    """How the files would have gone at one confidence level."""

    level: ConfidenceLevel
    correct: int
    inbox: int
    wrong: int

    @property
    def total(self) -> int:
        """Files tested."""
        return self.correct + self.inbox + self.wrong

    def _share(self, count: int) -> float:
        return count / self.total if self.total else 0.0

    @property
    def correct_share(self) -> float:
        """Share moved to the folder they are in."""
        return self._share(self.correct)

    @property
    def inbox_share(self) -> float:
        """Share left in the Inbox for the user to decide."""
        return self._share(self.inbox)

    @property
    def wrong_share(self) -> float:
        """Share moved to another folder than the one they are in."""
        return self._share(self.wrong)


@dataclass(frozen=True)
class Calibration:
    """The leave-one-out result at each confidence level, and the level to use."""

    files: int  # files tested
    folders: int  # folders holding them
    results: tuple[LevelResult, ...]  # in the order of the levels tested
    ranked_first: int  # files AutoShelf's best guess would put in their own folder
    recommended: int | None  # index of the recommended level; None without enough files

    @property
    def enough(self) -> bool:
        """True if the library is big enough for the result to mean something."""
        return self.files >= MIN_FILES and self.folders >= MIN_FOLDERS


def recommend(results: Sequence[LevelResult], max_wrong: float = MAX_WRONG) -> int:
    """Index of the most automatic level (levels go from cautious to automatic) that moves
    at most ``max_wrong`` of the files to a wrong folder; the most cautious one if none."""
    best = 0
    for index, result in enumerate(results):
        if result.wrong_share <= max_wrong:
            best = index
    return best


def best_guess(scores: Sequence[FolderScore]) -> str | None:
    """The folder AutoShelf would pick if it never asked: the deepest folder on the
    best-scoring branch that still beats every folder outside that branch.

    (A parent folder holds its subfolders' files, so it often scores highest itself.)
    """
    if not scores:
        return None
    top = max(scores, key=lambda item: item.score)
    rival = max(
        (item.score for item in scores if not is_related(item.folder, top.folder)),
        default=float("-inf"),
    )
    branch = [
        item for item in scores if is_related(item.folder, top.folder) and item.score >= rival
    ]
    return max(branch, key=lambda item: (depth(item.folder), item.score)).folder


def trials(
    entries: Sequence[IndexedFile],
    profiles: Sequence[FolderProfile],
    examples: Sequence[Example],
    levels: Sequence[ConfidenceLevel] = CONFIDENCE_LEVELS,
    progress: Progress | None = None,
) -> list[Trial]:
    """Classify each file read by its content as if it were new (leave-one-out).

    Files indexed by name only (iCloud online-only ones) stay in their folders' examples
    but aren't tested: a new file is read before it is sorted.
    """
    tested = [entry for entry in entries if entry.source == CONTENT]
    found = []
    for done, entry in enumerate(tested, start=1):
        own = [
            example
            for example in examples
            if not (
                example.name == entry.path.name
                and float(example.vector @ entry.vector) >= _SAME_FILE
            )
        ]
        learned = adjustments(entry.vector, own)
        scores = score_folders(
            entry.vector,
            profiles,
            adjustments={key.casefold(): item.amount for key, item in learned.items()},
            exclude=entry.path,
        )
        choices = []
        for level in levels:
            choice = choose(scores, level.threshold, level.margin).choice
            choices.append(choice.folder if choice else None)
        found.append(Trial(entry.path, entry.folder, best_guess(scores), tuple(choices)))
        if progress is not None:
            progress(done, len(tested))
    return found


def summarize(
    found: Sequence[Trial], levels: Sequence[ConfidenceLevel] = CONFIDENCE_LEVELS
) -> Calibration:
    """Count the trials at each level and recommend one."""
    results = []
    for index, level in enumerate(levels):
        correct = inbox = 0
        for trial in found:
            choice = trial.choices[index]
            if choice is None:
                inbox += 1
            elif choice.casefold() == trial.folder.casefold():
                correct += 1
        results.append(LevelResult(level, correct, inbox, len(found) - correct - inbox))
    ranked_first = sum(
        1 for trial in found if trial.best and trial.best.casefold() == trial.folder.casefold()
    )
    folders = len({trial.folder.casefold() for trial in found})
    calibration = Calibration(len(found), folders, tuple(results), ranked_first, None)
    if not calibration.enough:
        return calibration
    return Calibration(len(found), folders, tuple(results), ranked_first, recommend(results))


def calibrate(
    library: LibraryIndex,
    levels: Sequence[ConfidenceLevel] = CONFIDENCE_LEVELS,
    progress: Progress | None = None,
) -> Calibration:
    """The leave-one-out test over ``library`` (refresh it first). Reads no file."""
    found = trials(library.entries(), library.profiles(), library.examples, levels, progress)
    return summarize(found, levels)
