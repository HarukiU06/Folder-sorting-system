"""The library index: folder discovery, file vectors, caching and profiles."""

import os
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeEmbedder
from helpers import make, set_windows_attributes, windows_only

from autoshelf.db import repository
from autoshelf.extract import Extraction, extract
from autoshelf.ml import library as library_module
from autoshelf.ml.library import (
    LibraryIndex,
    folder_label,
    profile_text,
    scan_library,
)
from autoshelf.ml.vectors import from_blob


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def shelf(root: Path) -> Path:
    """A small library: nested folders, Unicode names, and things that must be skipped."""
    make(root / "Finance" / "invoice 2025.txt", "invoice total tax")
    make(root / "Machine Learning" / "Unsupervised" / "kmeans.md", "k-means clustering")
    make(root / "Machine Learning" / "Deep Learning" / "CNN 畳み込み.md", "convolution layers")
    (root / "Travel 旅行").mkdir()
    make(root / "dropped.pdf", "waiting in the root")
    make(root / "_Inbox" / "waiting.pdf", "waiting in the inbox")
    make(root / "_Duplicates" / "copy.pdf", "reserved")
    make(root / ".git" / "config", "hidden folder")
    make(root / "Finance" / "~$lock.docx", "temp file")
    make(root / "Finance" / "_old" / "ancient.txt", "reserved subfolder")
    return root


def test_scan_finds_category_folders_and_their_files(shelf: Path) -> None:
    scan = scan_library(shelf)
    assert scan.folders == (
        "Finance",
        "Machine Learning",
        "Machine Learning/Deep Learning",
        "Machine Learning/Unsupervised",
        "Travel 旅行",
    )
    found = {(item.folder, item.path.name) for item in scan.files}
    assert found == {
        ("Finance", "invoice 2025.txt"),
        ("Machine Learning/Unsupervised", "kmeans.md"),
        ("Machine Learning/Deep Learning", "CNN 畳み込み.md"),
    }


@windows_only
def test_scan_skips_hidden_folders_and_files(root: Path) -> None:
    hidden_dir = root / "Private"
    make(hidden_dir / "secret.txt")
    make(root / "Finance" / "hidden.txt")
    set_windows_attributes(hidden_dir, 0x2)
    set_windows_attributes(root / "Finance" / "hidden.txt", 0x2)

    scan = scan_library(root)

    assert scan.folders == ("Finance",)
    assert scan.files == ()


def test_folder_label_and_profile_text() -> None:
    assert folder_label("MachineLearning/ml_basics") == "Machine Learning / ml basics"
    assert profile_text("Finance") == "Finance"
    assert profile_text("Finance", " invoices and taxes ") == "Finance: invoices and taxes"


def test_refresh_embeds_each_file_once(conn, shelf: Path, embedder: FakeEmbedder) -> None:
    index = LibraryIndex(conn, shelf, embedder)
    stats = index.refresh()
    assert (stats.folders, stats.files, stats.embedded) == (5, 3, 3)

    embedded = embedder.embedded
    again = index.refresh()
    assert again.embedded == 0
    assert embedder.embedded == embedded


def test_vectors_are_cached_in_the_database(conn, shelf: Path) -> None:
    LibraryIndex(conn, shelf, FakeEmbedder()).refresh()

    restarted = FakeEmbedder()
    stats = LibraryIndex(conn, shelf, restarted).refresh()

    assert stats.files == 3
    assert stats.embedded == 0
    assert restarted.embedded == 0


def test_indexed_files_are_recorded_as_sorted_with_content(conn, shelf: Path) -> None:
    LibraryIndex(conn, shelf, FakeEmbedder()).refresh()
    record = repository.get_file_by_path(conn, shelf / "Finance" / "invoice 2025.txt")
    assert record.status == "sorted"
    assert record.sha256
    assert repository.get_file_content(conn, record.id) == (None, "invoice total tax")
    assert repository.get_vector(conn, record.id).model == "fake-model"


def test_changed_file_is_embedded_again(conn, shelf: Path, embedder: FakeEmbedder) -> None:
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()
    path = shelf / "Finance" / "invoice 2025.txt"
    path.write_text("a completely different and longer text", encoding="utf-8")
    os.utime(path, (1_700_000_000, 1_700_000_000))

    assert index.refresh().embedded == 1


def test_switching_models_rebuilds_vectors(conn, shelf: Path) -> None:
    LibraryIndex(conn, shelf, FakeEmbedder("model-a")).refresh()
    stats = LibraryIndex(conn, shelf, FakeEmbedder("model-b")).refresh()
    assert stats.embedded == 3
    record = repository.get_file_by_path(conn, shelf / "Finance" / "invoice 2025.txt")
    assert repository.get_vector(conn, record.id).model == "model-b"


def test_removed_files_and_folders_leave_the_index(conn, shelf: Path) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    (shelf / "Finance" / "invoice 2025.txt").unlink()
    (shelf / "Travel 旅行").rmdir()

    index.refresh()

    assert index.files_in("Finance") == []
    assert "Travel 旅行" not in index.folders


def test_parent_pool_includes_subfolder_files(conn, shelf: Path) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    names = {entry.path.name for entry in index.files_in("Machine Learning")}
    assert names == {"kmeans.md", "CNN 畳み込み.md"}
    assert index.direct_file_counts()["Machine Learning"] == 0


def test_profiles_use_descriptions(conn, shelf: Path, embedder: FakeEmbedder) -> None:
    repository.set_folder_description(conn, "travel 旅行", "trips hotels flights")
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()

    profiles = {profile.key: profile for profile in index.profiles()}

    expected = embedder.embed(["Travel 旅行: trips hotels flights"])[0]
    assert np.allclose(profiles["Travel 旅行"].vector, expected)
    assert profiles["Travel 旅行"].pool.shape == (0, expected.shape[0])
    assert len(profiles["Machine Learning"].pool) == 2


def test_unreadable_file_is_skipped_and_retried(
    conn, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def locked(path: Path) -> str:
        raise PermissionError("in use")

    monkeypatch.setattr(library_module, "sha256_file", locked)
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    stats = index.refresh()
    assert (stats.files, stats.unreadable) == (0, 3)

    monkeypatch.undo()
    assert index.refresh().embedded == 3


def test_online_only_files_are_indexed_by_name_without_reading_them(
    conn, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = make(root / "Finance" / "tax return 2024.pdf", "not downloaded yet")
    monkeypatch.setattr(library_module, "is_online_only", lambda st: True)
    reads: list[Path] = []

    def extractor(p: Path) -> Extraction:
        reads.append(p)
        return extract(p)

    def no_hashing(p: Path) -> str:
        raise AssertionError("an online-only file must not be read")

    monkeypatch.setattr(library_module, "sha256_file", no_hashing)
    index = LibraryIndex(conn, root, FakeEmbedder(), extractor=extractor)
    assert index.refresh().files == 1
    assert reads == []
    record = repository.get_file_by_path(conn, path)
    assert repository.get_vector(conn, record.id).source == "name"

    # Once iCloud has downloaded it, the name-only vector is replaced with the content.
    monkeypatch.setattr(library_module, "is_online_only", lambda st: False)
    monkeypatch.setattr(library_module, "sha256_file", lambda p: "digest")
    assert index.refresh().embedded == 1
    assert reads == [path]
    assert repository.get_vector(conn, record.id).source == "content"


def test_stored_vector_is_used_only_while_the_file_is_unchanged(conn, shelf: Path) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    path = shelf / "Finance" / "invoice 2025.txt"
    record = repository.get_file_by_path(conn, path)
    stored = from_blob(repository.get_vector(conn, record.id).vector)

    assert np.array_equal(index.stored_vector(path), stored)
    os.utime(path, (1_700_000_000, 1_700_000_000))
    assert index.stored_vector(path) is None
    assert index.stored_vector(shelf / "missing.txt") is None


def test_classify_scores_folders_for_a_vector(conn, shelf: Path, embedder: FakeEmbedder) -> None:
    repository.set_folder_description(conn, "Finance", "invoice receipt tax")
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()

    vector = embedder.embed(["invoice tax total"])[0]
    classification = index.classify(vector, threshold=0.3, margin=0.1)

    assert classification.choice.folder == "Finance"
