"""How long sorting will take: the estimate before starting, and the time left."""

from pathlib import Path

import pytest
from helpers import make

from autoshelf import wording
from autoshelf.db.connection import open_database
from autoshelf.features import estimate as estimates
from autoshelf.features.estimate import Eta, estimate
from autoshelf.ml.library import LibraryFile, LibraryScan

MODEL = "org/some-model"


def weights(model_dir: Path) -> None:
    snapshot = model_dir / "models--org--some-model" / "snapshots" / "abc"
    snapshot.mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"w")


def test_files_already_known_cost_nothing(tmp_path: Path) -> None:
    root = tmp_path / "shelf"
    known = make(root / "Finance" / "invoice.txt", "invoice")
    make(root / "Finance" / "receipt.txt", "receipt")
    models = tmp_path / "models"
    weights(models)
    conn = open_database(tmp_path / "a.db")
    try:
        st = known.stat()
        conn.execute(
            "INSERT INTO files (path, size, mtime, status, added_at)"
            " VALUES (?, ?, ?, 'sorted', '')",
            (str(known), st.st_size, st.st_mtime),
        )
        result = estimate(root, MODEL, models, loose_files=2, conn=conn)
    finally:
        conn.close()
    assert (result.library_files, result.to_read, result.to_sort) == (2, 1, 2)
    assert result.download_bytes == 0
    assert 0 < result.low_seconds < result.seconds < result.high_seconds < 60


def test_online_only_files_are_cheap_and_a_missing_model_is_a_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = [
        LibraryFile(tmp_path / "Docs" / f"{n}.pdf", "Docs", 5_000_000, 0.0, online_only=n > 0)
        for n in range(4001)
    ]
    monkeypatch.setattr(
        estimates, "scan_library", lambda root, patterns: LibraryScan(("Docs",), tuple(files))
    )
    result = estimate(tmp_path, MODEL, tmp_path / "models", loose_files=16)
    assert (result.library_files, result.name_only, result.to_read) == (4001, 4000, 4001)
    assert result.download_bytes == estimates.MODEL_BYTES
    assert 60 < result.low_seconds < result.high_seconds < 20 * 60  # minutes, not hours
    assert estimates.read_seconds(5_000_000, online_only=True) < estimates.read_seconds(
        5_000_000, online_only=False
    )


def test_the_estimate_in_words() -> None:
    result = estimates.Estimate(4244, 4226, 4244, 16, estimates.MODEL_BYTES, 190, 310)
    assert wording.estimate_lines(result) == [
        "4,244 files in your folders (4,226 online-only: names only, nothing downloaded)",
        "16 new files to sort now",
        "A one-time download of the language model (470 MB)",
    ]
    known = estimates.Estimate(13, 0, 0, 1, 0, 5, 9)
    assert wording.estimate_lines(known) == [
        "13 files in your folders, already known",
        "1 new file to sort now",
    ]
    assert wording.duration_range(190, 310) == "About 3 to 5 minutes"
    assert wording.duration_range(5, 9) == "Less than a minute"
    assert wording.duration_range(100, 110) == "About 2 minutes"
    assert wording.duration_range(3000, 7200) == "About 1 to 2 hours"


def test_durations() -> None:
    assert wording.duration(12) == "less than a minute"
    assert wording.duration(70) == "about 1 minute"
    assert wording.duration(150) == "about 2 minutes"
    assert wording.duration(5400) == "about 2 hours"


def test_time_left_starts_from_the_estimate_then_follows_the_speed() -> None:
    eta = Eta(total=100, expected=50, now=0.0)
    assert eta.update(10, now=1.0) == pytest.approx(45)  # too early: 90% of the estimate
    for second in range(2, 10):
        left = eta.update(10 + 20 * (second - 1), now=float(second))
    # it really does 20 files a second: 10 + 160 = 170 > 100, so all done
    assert left == 0.0
    steady = Eta(total=1000, expected=10, now=0.0)
    for second in range(1, 6):
        left = steady.update(100 * second, now=float(second))
    assert left == pytest.approx(5.0)  # 500 left at 100 a second


def test_time_left_without_an_estimate_waits_for_the_speed() -> None:
    eta = Eta(total=10, now=0.0)
    assert eta.update(1, now=0.5) is None
    assert eta.update(5, now=4.0) == pytest.approx(5 / ((0.3 * (4 / 3.5)) + 0.7 * 2), rel=0.01)
