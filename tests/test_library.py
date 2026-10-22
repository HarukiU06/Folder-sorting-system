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


# --- rescanning only when needed (Phase 9) ----------------------------------------------


@pytest.fixture
def scans(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every folder rescan the index makes (scan_library lists the whole tree)."""
    calls: list[Path] = []
    real = library_module.scan_library

    def counting(root: Path, *args, **kwargs):
        calls.append(root)
        return real(root, *args, **kwargs)

    monkeypatch.setattr(library_module, "scan_library", counting)
    return calls


def test_ensure_current_rescans_only_when_something_changed(
    conn, shelf: Path, scans: list[Path]
) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    assert index.ensure_current() is not None  # never scanned yet
    assert index.ensure_current() is None
    assert len(scans) == 1

    index.mark_dirty()  # e.g. the watcher saw a folder appear
    assert index.ensure_current() is not None
    assert len(scans) == 2

    # A description edited anywhere (even by another process) changes the library version.
    repository.set_folder_description(conn, "Finance", "bills")
    assert index.ensure_current() is not None
    assert index.ensure_current() is None
    assert len(scans) == 3


def test_user_moves_and_corrections_make_every_index_rescan(conn, shelf: Path) -> None:
    from autoshelf.actions.mover import move_file

    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    path = shelf / "Finance" / "invoice 2025.txt"
    move_file(conn, path, shelf / "Travel 旅行", root=shelf, reason="user")
    assert index.ensure_current() is not None  # the watcher ignores the app's own events
    assert [entry.path.name for entry in index.files_in("Travel 旅行")] == ["invoice 2025.txt"]

    record = repository.get_file_by_path(conn, shelf / "Travel 旅行" / "invoice 2025.txt")
    repository.record_correction(
        conn, file_id=record.id, from_folder="Finance", to_folder="Travel 旅行", source="inbox"
    )
    assert index.ensure_current() is not None  # learning examples changed

    # A move that doesn't touch a category folder leaves the index as it is.
    inbox = shelf / "_Inbox"
    move_file(conn, shelf / "dropped.pdf", inbox, root=shelf, reason="ask")
    assert index.ensure_current() is None


def test_a_sorted_file_joins_its_folder_without_a_rescan(
    conn, shelf: Path, embedder: FakeEmbedder, scans: list[Path]
) -> None:
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()
    path = make(shelf / "Finance" / "receipt.txt", "receipt total")
    document = index.embed(extract(path), path.name)
    st = path.stat()
    index.add_file(path, document, size=st.st_size, mtime=st.st_mtime)

    assert {entry.path.name for entry in index.files_in("Finance")} == {
        "invoice 2025.txt",
        "receipt.txt",
    }
    assert index.ensure_current() is None
    assert len(scans) == 1

    # A folder the index doesn't know yet (created meanwhile) makes it rescan instead.
    new = make(shelf / "Taxes" / "return.txt", "tax return")
    index.add_file(new, index.embed(extract(new), new.name), size=1, mtime=1.0)
    assert index.dirty


def test_folder_documents_take_a_fixed_number_of_queries(conn, shelf: Path) -> None:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    for number in range(20):
        make(shelf / "Finance" / f"bill {number}.txt", f"bill number {number} total")
    index.refresh()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        documents = index.folder_documents()
    finally:
        conn.set_trace_callback(None)
    assert "bill number 7 total" in documents["Finance"]
    assert len(statements) <= 3  # not two per file, as before


def test_a_file_that_crashed_a_read_is_indexed_by_name_until_cleared(
    conn, shelf: Path, paths
) -> None:
    from autoshelf.extract import guard

    path = shelf / "Finance" / "invoice 2025.txt"
    unfinished = guard.ReadGuard(paths.home / guard.STATE_FILENAME, record=True).reading(path)
    unfinished.__enter__()  # the app died while reading it
    guard.start(paths.home, record=True)
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    record = repository.get_file_by_path(conn, path)
    assert repository.get_vector(conn, record.id).source == "name"
    assert index.refresh().embedded == 0  # not retried at every rescan
    assert index.stored_vector(path) is not None

    guard.clear(paths.home)
    assert index.stored_vector(path) is None  # the Inbox reads it again
    assert index.refresh().embedded == 1
    assert repository.get_vector(conn, record.id).source == "content"
    assert repository.get_file_content(conn, record.id) == (None, "invoice total tax")
