r"""Measure how well AutoShelf sorts a library, to check its hand-tuned constants.

Usage (from the repository root):
    .venv\Scripts\python.exe scripts\benchmark.py --model-cache MODEL_CACHE_DIR
    .venv\Scripts\python.exe scripts\benchmark.py --fake        (no model: checks the script)
    .venv\Scripts\python.exe scripts\benchmark.py --library "D:\a copy of my library" ...

By default the library is generated (scripts/sample_corpus.py) in a temporary folder.
With --library, files are only read: nothing there is moved or written. AutoShelf's data
(the database) always goes to a temporary folder, never to %APPDATA%\AutoShelf.

Sections (all by default; pick some with --only):
  levels     leave-one-out: every file sorted as if new, at each confidence level
  constants  the same at Balanced while NEIGHBOR_WEIGHT, FULL_POOL, NEIGHBORS and
             HEADER_WEIGHT vary (one at a time; * marks the value in use)
  grid       correct and wrong shares for thresholds x margins (the presets' trade-off)
  learning   files arriving one by one, the user correcting AutoShelf's mistakes, with
             different SIMILAR_FROM/SIMILAR_FULL values (and without learning)
"""

import argparse
import json
import os
import random
import re
import sys
import tempfile
import zlib
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import numpy as np

from autoshelf.config import CONFIDENCE_LEVELS, DEFAULT_EMBEDDING_MODEL, ConfidenceLevel
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.ml import calibrate, classifier, learned, vectors
from autoshelf.ml.classifier import choose, score_folders
from autoshelf.ml.learned import Example, adjustments
from autoshelf.ml.library import IndexedFile, LibraryIndex, build_profiles
from autoshelf.ml.vectors import from_blob, normalize

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sample_corpus import make_corpus

SECTIONS = ("levels", "constants", "grid", "learning")
BALANCED = next(level for level in CONFIDENCE_LEVELS if level.name == "Balanced")
CONSTANT_VALUES = {
    "NEIGHBOR_WEIGHT": (classifier, (0.4, 0.5, 0.6, 0.7, 0.8)),
    "FULL_POOL": (classifier, (1, 2, 3, 5, 8)),
    "NEIGHBORS": (classifier, (1, 2, 3, 5)),
    "HEADER_WEIGHT": (vectors, (0.2, 0.3, 0.4, 0.5, 0.6)),
}
THRESHOLDS = (0.30, 0.35, 0.40, 0.45, 0.50, 0.55)
MARGINS = (0.04, 0.06, 0.08, 0.10, 0.12, 0.15)
LEARNING_VARIANTS = ((None, None), (0.25, 0.50), (0.30, 0.60), (0.35, 0.70), (0.40, 0.80))
SEED_FILES = 3  # files per folder the user starts with in the learning simulation


class HashingEmbedder:
    """A stand-in for the language model (hashed bag of words), to try the script fast."""

    model_id = "hashing"
    _words = re.compile(r"\w+")

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        rows = np.zeros((len(texts), 256), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in self._words.findall(text.casefold()):
                rows[row, zlib.crc32(word.encode()) % 256] += 1.0
            length = np.linalg.norm(rows[row])
            if length:
                rows[row] /= length
        return rows

    def chunk(self, text: str) -> list[str]:
        return [text[start : start + 400] for start in range(0, len(text), 400) if text.strip()]


@contextmanager
def constant(module, name: str, value) -> Iterator[None]:
    """Use another value for a module constant while the block runs."""
    old = getattr(module, name)
    setattr(module, name, value)
    try:
        yield
    finally:
        setattr(module, name, old)


def share(count: int, total: int) -> str:
    return f"{count / total:5.0%}" if total else "    -"


# --- sections -------------------------------------------------------------------------------


def run_levels(library: LibraryIndex) -> dict:
    result = calibrate.calibrate(library)
    print(f"\n== Leave-one-out at each level ({result.files} files, {result.folders} folders)")
    print(f"{'level':16} thr   margin  correct  inbox  wrong")
    for item in result.results:
        level = item.level
        print(
            f"{level.name:16} {level.threshold:.2f}  {level.margin:.2f}   "
            f"{share(item.correct, item.total)}  {share(item.inbox, item.total)}"
            f"  {share(item.wrong, item.total)}"
        )
    print(f"Own folder scored best (any level): {share(result.ranked_first, result.files)}")
    if result.recommended is not None:
        print(f"Recommended level: {result.results[result.recommended].level.name}")
    return {
        "files": result.files,
        "ranked_first": result.ranked_first,
        "levels": {
            item.level.name: [item.correct, item.inbox, item.wrong] for item in result.results
        },
    }


def header_weighted_entries(conn, library: LibraryIndex, weight: float) -> list[IndexedFile]:
    """The library's files with vectors remade from their stored chunk vectors as if
    HEADER_WEIGHT were ``weight`` (no model needed)."""
    pieces: dict[str, list[tuple[int, np.ndarray]]] = {}
    for row in repository.searchable_chunks(conn, library.model):
        piece = (row["idx"], from_blob(row["embedding"]))
        pieces.setdefault(row["path"].casefold(), []).append(piece)
    remade = []
    for entry in library.entries():
        parts = sorted(pieces.get(str(entry.path).casefold(), []), key=lambda part: part[0])
        if not parts:
            remade.append(entry)
            continue
        header, body = parts[0][1], [vector for _, vector in parts[1:]]
        if body:
            vector = normalize(weight * header + (1 - weight) * normalize(np.mean(body, axis=0)))
        else:
            vector = normalize(header)
        remade.append(replace(entry, vector=vector))
    return remade


def run_constants(conn, library: LibraryIndex) -> dict:
    folder_vectors = {profile.key: profile.vector for profile in library.profiles()}
    results: dict[str, dict] = {}
    print("\n== Constants, one at a time (at Balanced; * = the value in use)")
    print(f"{'constant':16} value  best   correct  inbox  wrong")
    for name, (module, values) in CONSTANT_VALUES.items():
        current = getattr(module, name)
        results[name] = {}
        for value in values:
            if name == "HEADER_WEIGHT":
                entries = header_weighted_entries(conn, library, value)
                found = calibrate.trials(
                    entries,
                    build_profiles(folder_vectors, entries),
                    library.examples,
                    [BALANCED],
                )
            else:
                with constant(module, name, value):
                    found = calibrate.trials(
                        library.entries(), library.profiles(), library.examples, [BALANCED]
                    )
            outcome = calibrate.summarize(found, [BALANCED])
            item = outcome.results[0]
            mark = "*" if value == current else " "
            print(
                f"{name:16} {value!s:5}{mark} {share(outcome.ranked_first, outcome.files)}  "
                f"{share(item.correct, item.total)}  {share(item.inbox, item.total)}"
                f"  {share(item.wrong, item.total)}"
            )
            results[name][str(value)] = [outcome.ranked_first, item.correct, item.inbox, item.wrong]
    return results


def run_grid(library: LibraryIndex) -> dict:
    levels = [
        ConfidenceLevel(f"{threshold:.2f}/{margin:.2f}", threshold, margin)
        for threshold in THRESHOLDS
        for margin in MARGINS
    ]
    found = calibrate.trials(library.entries(), library.profiles(), library.examples, levels)
    outcome = calibrate.summarize(found, levels)
    by_name = {item.level.name: item for item in outcome.results}
    print("\n== Threshold x margin: correct% / wrong% (the rest waits in the Inbox)")
    print("thr \\ margin " + "".join(f"{margin:>11.2f}" for margin in MARGINS))
    for threshold in THRESHOLDS:
        cells = []
        for margin in MARGINS:
            item = by_name[f"{threshold:.2f}/{margin:.2f}"]
            cells.append(f"{item.correct_share:5.0%}/{item.wrong_share:<4.0%}")
        print(f"{threshold:<12.2f} " + " ".join(f"{cell:>10}" for cell in cells))
    presets = ", ".join(
        f"{level.name} {level.threshold:.2f}/{level.margin:.2f}" for level in CONFIDENCE_LEVELS
    )
    print(f"Presets: {presets}")
    return {name: [item.correct, item.inbox, item.wrong] for name, item in by_name.items()}


def simulate_stream(
    library: LibraryIndex, level: ConfidenceLevel, use_learning: bool, rng_seed: int
) -> tuple[int, int, int, int, int, int]:
    """Files arrive one by one into a library holding SEED_FILES files per folder. The user
    answers the Inbox and corrects wrong moves; both are learned from, as in the app.
    Returns (correct, inbox, wrong) over the whole stream and over its second half."""
    folder_vectors = {profile.key: profile.vector for profile in library.profiles()}
    by_folder: dict[str, list[IndexedFile]] = {}
    for entry in sorted(library.entries(), key=lambda item: str(item.path)):
        by_folder.setdefault(entry.folder, []).append(entry)
    pool = [entry for entries in by_folder.values() for entry in entries[:SEED_FILES]]
    stream = [entry for entries in by_folder.values() for entry in entries[SEED_FILES:]]
    random.Random(rng_seed).shuffle(stream)
    examples: list[Example] = []
    counts = [0, 0, 0, 0, 0, 0]
    for number, entry in enumerate(stream):
        found = adjustments(entry.vector, examples) if use_learning else {}
        scores = score_folders(
            entry.vector,
            build_profiles(folder_vectors, pool),
            adjustments={key.casefold(): item.amount for key, item in found.items()},
        )
        choice = choose(scores, level.threshold, level.margin).choice
        if choice is None:
            outcome = 1
            corrected_from = scores[0].folder if scores else None  # the top suggestion
        elif choice.folder == entry.folder:
            outcome, corrected_from = 0, None
        else:
            outcome, corrected_from = 2, choice.folder
        if corrected_from is not None and corrected_from != entry.folder:
            examples.append(Example(corrected_from, entry.folder, entry.vector, entry.path.name))
        counts[outcome] += 1
        if number >= len(stream) // 2:
            counts[3 + outcome] += 1
        pool.append(entry)  # it ends up in its folder either way
    return tuple(counts)


def run_learning(library: LibraryIndex, seed: int) -> dict:
    print(
        f"\n== Learning from corrections: files arriving one by one (at Balanced; the user "
        f"starts with {SEED_FILES} files per folder)"
    )
    print(f"{'SIMILAR_FROM/FULL':18} correct  inbox  wrong   (second half: correct  inbox  wrong)")
    results = {}
    for start, full in LEARNING_VARIANTS:
        label = "no learning" if start is None else f"{start:.2f}/{full:.2f}"
        if start == learned.SIMILAR_FROM and full == learned.SIMILAR_FULL:
            label += " *"
        if start is None:
            counts = simulate_stream(library, BALANCED, False, seed)
        else:
            with constant(learned, "SIMILAR_FROM", start), constant(learned, "SIMILAR_FULL", full):
                counts = simulate_stream(library, BALANCED, True, seed)
        total, half = sum(counts[:3]), sum(counts[3:])
        print(
            f"{label:18} {share(counts[0], total)}  {share(counts[1], total)}  "
            f"{share(counts[2], total)}            {share(counts[3], half)}  "
            f"{share(counts[4], half)}  {share(counts[5], half)}"
        )
        results[label] = list(counts)
    return results


# --- main -----------------------------------------------------------------------------------


def build_library(args: argparse.Namespace, temp: Path):
    home = temp / "data"
    home.mkdir()
    os.environ["AUTOSHELF_HOME"] = str(home)  # never the real data folder
    if args.library:
        root = Path(args.library).resolve(strict=True)
        descriptions = {}
    else:
        root = temp / "library"
        root.mkdir()
        descriptions = make_corpus(root, files_per_folder=args.files_per_folder, seed=args.seed)
    conn = open_database(home / "autoshelf.db")
    for folder, text in descriptions.items():
        repository.set_folder_description(conn, folder, text)
    if args.fake:
        embedder = HashingEmbedder()
    else:
        from autoshelf.ml.embedder import load_embedder

        if not args.model_cache:
            sys.exit("give --model-cache (the folder the model is downloaded to) or --fake")
        embedder = load_embedder(args.model, Path(args.model_cache))
    library = LibraryIndex(conn, root, embedder)

    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            print(f"Reading files: {done}/{total}", file=sys.stderr)

    library.refresh(progress=progress)
    return conn, library


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model-cache", help="folder the language model is downloaded to")
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--fake", action="store_true", help="a hashing stand-in for the model")
    parser.add_argument("--library", help="measure this folder instead (files are only read)")
    parser.add_argument("--files-per-folder", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--only", help=f"comma-separated sections: {', '.join(SECTIONS)}")
    parser.add_argument("--json", type=Path, help="also write the numbers to this new file")
    args = parser.parse_args(argv)
    wanted = args.only.split(",") if args.only else list(SECTIONS)
    unknown = set(wanted) - set(SECTIONS)
    if unknown:
        parser.error(f"unknown section(s): {', '.join(sorted(unknown))}")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    with tempfile.TemporaryDirectory(prefix="autoshelf-benchmark-") as temp:
        conn, library = build_library(args, Path(temp))
        try:
            report = {"model": library.model, "files": len(library.entries())}
            if "levels" in wanted:
                report["levels"] = run_levels(library)
            if "constants" in wanted:
                report["constants"] = run_constants(conn, library)
            if "grid" in wanted:
                report["grid"] = run_grid(library)
            if "learning" in wanted:
                report["learning"] = run_learning(library, args.seed)
        finally:
            conn.close()
    if args.json:
        with open(args.json, "x", encoding="utf-8") as handle:  # never overwrites
            json.dump(report, handle, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
