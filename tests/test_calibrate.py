"""How well would AutoShelf sort a library? (Phase 10: leave-one-out, the calibrate feature
and the benchmark script.)"""

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf import wording
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import CONFIDENCE_LEVELS, AppPaths, ConfidenceLevel
from autoshelf.db import repository
from autoshelf.ml import calibrate
from autoshelf.ml.classifier import FolderScore
from autoshelf.ml.learned import Example
from autoshelf.ml.library import CONTENT, NAME_ONLY, IndexedFile, LibraryIndex, build_profiles

REPO = Path(__file__).resolve().parent.parent


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


def entry(name: str, folder: str, *values: float, source: str = CONTENT) -> IndexedFile:
    return IndexedFile(Path(name), folder, 1, 1.0, source, unit(*values))


def test_each_file_is_left_out_of_its_own_folder() -> None:
    # A folder with a single file: without leaving it out, it would match itself perfectly.
    lonely = entry("lonely.txt", "Travel", 0, 0, 1)
    entries = [entry("a.txt", "Finance", 1, 0, 0), entry("b.txt", "Finance", 1, 0.1, 0), lonely]
    profiles = build_profiles(
        {"Finance": unit(1, 0, 0), "Travel": unit(0, 1, 0)}, entries
    )  # the Travel profile (its name) points elsewhere than its only file
    found = calibrate.trials(entries, profiles, [], [CONFIDENCE_LEVELS[2]])
    by_name = {trial.path.name: trial for trial in found}
    assert by_name["a.txt"].choices == ("Finance",)
    assert by_name["lonely.txt"].choices[0] != "Travel"


def test_a_files_own_correction_doesnt_help_it() -> None:
    # X is in Travel but reads a little more like Finance; the user once moved it there.
    x = entry("hotel invoice.txt", "Travel", 1, 0.9, 0)
    entries = [entry("tax.txt", "Finance", 1, 0, 0), entry("trip.txt", "Travel", 0, 1, 0), x]
    profiles = build_profiles({"Finance": unit(1, 0, 0), "Travel": unit(0, 1, 0)}, entries)
    own = Example("Finance", "Travel", x.vector, x.path.name)
    other = Example("Finance", "Travel", unit(1, 0.9, 0.05), "another hotel invoice.txt")
    level = ConfidenceLevel("loose", 0.0, 0.0)

    with_own = calibrate.trials(entries, profiles, [own], [level])
    with_other = calibrate.trials(entries, profiles, [other], [level])

    choice = {trial.path.name: trial.choices[0] for trial in with_own}["hotel invoice.txt"]
    assert choice == "Finance"  # its own correction was ignored
    helped = {trial.path.name: trial.choices[0] for trial in with_other}["hotel invoice.txt"]
    assert helped != "Finance"  # a correction of another, similar file does count


def test_files_indexed_by_name_only_are_not_tested() -> None:
    entries = [entry("a.txt", "Finance", 1, 0), entry("b.txt", "Finance", 1, 0, source=NAME_ONLY)]
    profiles = build_profiles({"Finance": unit(1, 0)}, entries)
    assert [trial.path.name for trial in calibrate.trials(entries, profiles, [])] == ["a.txt"]


def test_best_guess_is_the_deepest_folder_of_the_best_branch() -> None:
    scores = [
        FolderScore("Machine Learning", 0.62, 0.5),
        FolderScore("Machine Learning/Unsupervised", 0.58, 0.5),
        FolderScore("Machine Learning/Deep Learning", 0.41, 0.5),
        FolderScore("Recipes", 0.50, 0.5),
    ]
    assert calibrate.best_guess(scores) == "Machine Learning/Unsupervised"
    assert calibrate.best_guess([FolderScore("Recipes", 0.5, 0.5)]) == "Recipes"
    assert calibrate.best_guess([]) is None


def result(name: str, correct: int, inbox: int, wrong: int) -> calibrate.LevelResult:
    level = next(level for level in CONFIDENCE_LEVELS if level.name == name)
    return calibrate.LevelResult(level, correct, inbox, wrong)


def test_the_most_automatic_level_that_rarely_goes_wrong_is_recommended() -> None:
    results = [
        result("Very cautious", 70, 30, 0),
        result("Cautious", 80, 19, 1),
        result("Balanced", 87, 9, 4),
        result("Automatic", 89, 5, 6),  # too many wrong moves
        result("Very automatic", 92, 0, 8),
    ]
    assert calibrate.recommend(results) == 2
    assert calibrate.recommend([result("Balanced", 50, 0, 50)]) == 0  # none: most cautious
    assert wording.level_result(results[2]) == (
        "At Balanced: 87% correct, 9% to the Inbox, 4% wrong"
    )


def test_a_tiny_library_gets_no_recommendation() -> None:
    trials = [
        calibrate.Trial(Path(f"{number}.txt"), "Finance", "Finance", ("Finance",))
        for number in range(3)
    ]
    outcome = calibrate.summarize(trials, [CONFIDENCE_LEVELS[2]])
    assert not outcome.enough
    assert outcome.recommended is None
    assert "Not enough files" in wording.calibration(outcome)[0]


# --- the app and the command line -------------------------------------------------------------


@pytest.fixture
def shelf(root: Path) -> Path:
    for number in range(6):
        make(root / "Finance" / f"invoice {number}.txt", f"invoice total tax due bank {number}")
        make(root / "Recipes" / f"cake {number}.txt", f"recipe baking oven flour sugar {number}")
    return root


@pytest.fixture
def service(paths: AppPaths, shelf: Path) -> AppService:
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.save_settings(replace(app.settings(), root=str(shelf)))
    app.describe("Finance", "invoice receipt tax bank")
    app.describe("Recipes", "recipe cooking baking oven")
    return app


def test_the_service_tests_the_library_and_switches_level(service: AppService) -> None:
    progress: list[tuple[int, int]] = []
    outcome = service.calibrate(progress=lambda done, total: progress.append((done, total)))
    assert (outcome.files, outcome.folders) == (12, 2)
    assert progress[-1] == (12, 12)
    assert outcome.enough and outcome.recommended is not None
    assert len(outcome.results) == len(CONFIDENCE_LEVELS)
    assert service.library_file_count() == 12

    service.set_level(0)
    settings = service.settings()
    assert (settings.auto_threshold, settings.auto_margin) == (
        CONFIDENCE_LEVELS[0].threshold,
        CONFIDENCE_LEVELS[0].margin,
    )


def test_cli_calibrate(service: AppService, shelf: Path, fake_model, capsys) -> None:
    service.set_level(0)  # very cautious: the recommendation will differ
    assert main(["calibrate", "--root", str(shelf)]) == 0
    out = capsys.readouterr().out
    assert "Each of the 12 files in your 2 folders" in out
    assert "At Very cautious:" in out and "(now)" in out
    assert "Recommended:" in out
    assert "autoshelf calibrate --apply" in out

    assert main(["calibrate", "--root", str(shelf), "--apply"]) == 0
    assert "Now sorting at" in capsys.readouterr().out
    assert service.settings().auto_threshold != CONFIDENCE_LEVELS[0].threshold


def test_learning_examples_are_used_like_in_the_app(conn, service: AppService, shelf) -> None:
    library = LibraryIndex(conn, shelf, FakeEmbedder())
    library.refresh()
    record = repository.get_file_by_path(conn, shelf / "Recipes" / "cake 0.txt")
    repository.record_correction(
        conn, file_id=record.id, from_folder="Finance", to_folder="Recipes", source="inbox"
    )
    library.refresh()
    assert calibrate.calibrate(library).files == 12


# --- the benchmark script -----------------------------------------------------------------------


def test_the_benchmark_script_runs(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    done = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "benchmark.py"),
            "--fake",
            "--files-per-folder",
            "4",
            "--json",
            str(report),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        cwd=REPO,
    )
    assert done.returncode == 0, done.stderr
    for section in ("Leave-one-out", "Constants", "Threshold x margin", "Learning"):
        assert section in done.stdout
    assert report.exists()
