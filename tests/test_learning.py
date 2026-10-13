"""Learning from corrections (SPEC 4.6): recording them, using them, and noticing moves
made in Explorer or on the iPhone."""

import os
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.inbox import assign
from autoshelf.actions.learning import (
    CorrectionError,
    adopt_moved_file,
    correct,
    folder_key_of,
)
from autoshelf.actions.undo import undo
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.ml import library as library_module
from autoshelf.ml.learned import (
    MAX_PENALTY,
    PENALTY_PER_EXAMPLE,
    SIMILAR_FROM,
    SIMILAR_FULL,
    Example,
    adjustments,
    weight,
)
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import OutcomeKind, Pipeline
from autoshelf.watcher.service import WatchService

THRESHOLD = 0.3
MARGIN = 0.1


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / np.linalg.norm(vector)


# --- the penalty ------------------------------------------------------------------------


def test_weight_grows_with_similarity() -> None:
    assert weight(SIMILAR_FROM) == 0.0
    assert weight(SIMILAR_FULL) == 1.0
    assert weight(1.0) == 1.0
    assert 0.0 < weight((SIMILAR_FROM + SIMILAR_FULL) / 2) < 1.0


def test_only_the_folder_files_were_moved_away_from_is_lowered() -> None:
    examples = [Example("Finance", "Travel", unit(1, 0), "hotel.pdf")]

    found = adjustments(unit(1, 0), examples)

    assert set(found) == {"Finance"}  # not Travel, where the file went
    assert found["Finance"].amount == pytest.approx(-PENALTY_PER_EXAMPLE)
    assert found["Finance"].evidence[0].name == "hotel.pdf"
    assert adjustments(unit(0, 1), examples) == {}  # unrelated content: no effect


def test_penalty_adds_up_but_is_capped() -> None:
    examples = [Example("Finance", "Travel", unit(1, 0)) for _ in range(10)]
    assert adjustments(unit(1, 0), examples)["Finance"].amount == pytest.approx(-MAX_PENALTY)


# --- a small library ------------------------------------------------------------------------


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank transfer")
    make(root / "Travel" / "flight.txt", "flight boarding pass airport gate")
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank payment total")
    repository.set_folder_description(conn, "Travel", "travel flight hotel booking trip")
    return root


@pytest.fixture
def library(conn, shelf: Path, embedder: FakeEmbedder) -> LibraryIndex:
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()
    return index


@pytest.fixture
def smart(conn, shelf: Path, library: LibraryIndex) -> Pipeline:
    return Pipeline(conn, shelf, library=library, threshold=THRESHOLD, margin=MARGIN)


# Says "Finance" to the classifier, but the user keeps hotel bills under Travel.
HOTEL = "hotel invoice total tax due bank payment"


def test_corrections_lower_the_folder_for_similar_files(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex
) -> None:
    first = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    assert first.dst.parent == shelf / "Finance"  # wrong: the user files these under Travel

    correct(conn, first.history_id, "Travel", root=shelf)
    library.refresh()
    probe = smart.extractor(make(shelf.parent / "probe.txt", HOTEL))
    classification = library.classify(
        library.vectorize(probe, "probe.txt"), threshold=THRESHOLD, margin=MARGIN
    )
    assert classification.score_of("Finance").learned == pytest.approx(-PENALTY_PER_EXAMPLE)
    assert classification.score_of("Travel").learned == 0
    assert "Finance" in classification.learned

    # Files keep being moved from Finance to Travel: similar files stop going to Finance.
    for number in (2, 3):
        moved = smart.process(make(shelf / f"hotel bill {number}.txt", HOTEL))
        if moved.dst.parent == shelf / "Finance":
            correct(conn, moved.history_id, "Travel", root=shelf)
    last = smart.process(make(shelf / "hotel bill 4.txt", HOTEL))
    assert last.dst.parent != shelf / "Finance"
    entry = repository.get_history(conn, last.history_id)
    learned = {item["folder"]: item for item in entry.details["learned"]}
    assert learned["Finance"]["amount"] < 0
    assert learned["Finance"]["examples"][0]["to"] == "Travel"


# --- recording corrections ------------------------------------------------------------------


def test_inbox_choice_keeps_the_suggestion_and_the_vector(
    conn, shelf: Path, smart: Pipeline
) -> None:
    waiting = smart.process(make(shelf / "notes.txt", "call mom buy milk"))
    assert waiting.dst.parent == shelf / "_Inbox"
    suggested = repository.get_file_by_path(conn, waiting.dst).suggestion
    assert suggested in ("Finance", "Travel")

    assign(conn, waiting.dst, "Travel", root=shelf)

    (row,) = repository.list_corrections(conn)
    assert (row["from_folder"], row["to_folder"], row["source"]) == (suggested, "Travel", "inbox")
    assert row["model"] == "fake-model"
    assert row["vector"] is not None
    assert row["history_id"] == waiting.history_id


def test_choosing_a_folder_after_undoing_an_automatic_move_is_an_undo_correction(
    conn, shelf: Path, smart: Pipeline
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    undo(conn, moved.history_id)

    assign(conn, shelf / "hotel bill.txt", "Travel", root=shelf)

    (row,) = repository.list_corrections(conn)
    assert (row["from_folder"], row["to_folder"], row["source"]) == ("Finance", "Travel", "undo")
    assert row["history_id"] == moved.history_id


def test_correct_moves_the_file_and_learns(conn, shelf: Path, smart: Pipeline) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))

    result = correct(conn, moved.history_id, "Travel", root=shelf)

    assert result.dst == shelf / "Travel" / "hotel bill.txt"
    entry = repository.get_history(conn, result.history_id)
    assert (entry.reason, entry.details["corrects"]) == ("user", moved.history_id)
    (row,) = repository.list_corrections(conn)
    assert (row["from_folder"], row["to_folder"], row["source"]) == ("Finance", "Travel", "undo")
    undo(conn, result.history_id)  # a correction can itself be undone
    assert (shelf / "Finance" / "hotel bill.txt").exists()


def test_correct_refuses_what_it_cant_do(conn, shelf: Path, smart: Pipeline) -> None:
    waiting = smart.process(make(shelf / "notes.txt", "call mom buy milk"))
    with pytest.raises(CorrectionError, match="Inbox"):
        correct(conn, waiting.history_id, "Travel", root=shelf)
    moved = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    with pytest.raises(CorrectionError, match="already in"):
        correct(conn, moved.history_id, "Finance", root=shelf)
    moved.dst.unlink()
    with pytest.raises(CorrectionError, match="no longer"):
        correct(conn, moved.history_id, "Travel", root=shelf)
    with pytest.raises(CorrectionError, match="no history entry"):
        correct(conn, 999, "Travel", root=shelf)


def test_folder_key_of(root: Path) -> None:
    assert folder_key_of(root / "A" / "B" / "x.pdf", root) == "A/B"
    assert folder_key_of(root / "_Inbox" / "x.pdf", root) is None
    assert folder_key_of(root / "x.pdf", root) is None


# --- moves made outside AutoShelf ----------------------------------------------------------------


def test_move_between_folders_in_explorer_is_learned(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex, embedder: FakeEmbedder
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    record = repository.get_file_by_path(conn, moved.dst)
    target = shelf / "Travel" / "hotel bill.txt"
    os.rename(moved.dst, target)  # what Explorer does
    embedded = embedder.embedded

    stats = library.refresh()

    (relocation,) = stats.relocations
    assert (relocation.from_folder, relocation.to_folder) == ("Finance", "Travel")
    assert relocation.file_id == record.id
    assert repository.get_file_by_path(conn, target).id == record.id  # same record, moved
    assert embedder.embedded == embedded  # its vector was reused, not recomputed
    (row,) = repository.list_corrections(conn)
    assert (row["from_folder"], row["to_folder"], row["source"]) == (
        "Finance",
        "Travel",
        "external",
    )
    assert row["history_id"] == moved.history_id
    assert library.refresh().relocations == ()  # learned once


def test_delete_plus_create_is_recognised_by_content(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    copy = shutil.copy2(moved.dst, shelf / "Travel" / "Hotel (renamed).txt")
    moved.dst.unlink()  # iCloud: a create in one folder and a delete in the other

    (relocation,) = library.refresh().relocations

    assert relocation.new_path == Path(copy)
    assert relocation.to_folder == "Travel"


def test_answering_the_inbox_on_the_iphone_is_learned(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex
) -> None:
    waiting = smart.process(make(shelf / "notes.txt", "call mom buy milk"))
    suggested = repository.get_file_by_path(conn, waiting.dst).suggestion
    shutil.move(waiting.dst, shelf / "Travel" / "notes.txt")

    library.refresh()

    (row,) = repository.list_corrections(conn)
    assert (row["from_folder"], row["to_folder"], row["source"]) == (
        suggested,
        "Travel",
        "external",
    )


def test_online_only_file_is_matched_by_name_and_size(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    target = shelf / "Travel" / "hotel bill.txt"
    os.rename(moved.dst, target)
    monkeypatch.setattr(library_module, "is_online_only", lambda st: True)
    read: list[Path] = []
    monkeypatch.setattr(library_module, "sha256_file", lambda path: read.append(path))

    (relocation,) = library.refresh().relocations

    assert relocation.new_path == target
    assert read == []  # never read: that would download it from iCloud


def test_rename_in_place_moves_the_record_without_learning(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex
) -> None:
    moved = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    os.rename(moved.dst, shelf / "Finance" / "Invoice March.txt")

    (relocation,) = library.refresh().relocations

    assert relocation.correction_id is None
    assert repository.list_corrections(conn) == []


def test_autoshelfs_own_moves_are_not_learned(
    conn, shelf: Path, smart: Pipeline, library: LibraryIndex
) -> None:
    smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    smart.process(make(shelf / "flight.txt", "flight boarding pass airport gate"))
    assert library.refresh().relocations == ()
    assert repository.list_corrections(conn) == []


def test_a_move_in_progress_is_not_mistaken_for_the_users(
    conn, shelf: Path, smart: Pipeline
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    record = repository.get_file_by_path(conn, moved.dst)
    target = shelf / "Travel" / "hotel bill.txt"
    os.rename(moved.dst, target)
    repository.insert_history(
        conn, action="move", src=moved.dst, dst=target, reason="user", root=shelf, file_id=record.id
    )
    found = adopt_moved_file(
        conn, shelf, target, size=target.stat().st_size, online_only=False, digest=record.sha256
    )
    assert found is None
    assert repository.get_file_by_path(conn, moved.dst).id == record.id


def test_two_processes_noticing_the_same_move_learn_it_once(
    conn, shelf: Path, smart: Pipeline, paths: AppPaths
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    os.rename(moved.dst, shelf / "Travel" / "hotel bill.txt")
    barrier = threading.Barrier(2)
    found: list = []

    def refresh() -> None:
        # Each process has its own connection and its own index.
        own_conn = open_database(paths.db_path)
        try:
            index = LibraryIndex(own_conn, shelf, FakeEmbedder())
            barrier.wait()
            found.extend(index.refresh().relocations)
        finally:
            own_conn.close()

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(found) == 1
    assert len(repository.list_corrections(conn)) == 1


# --- while watching -----------------------------------------------------------------------------


def test_watcher_learns_from_a_move_made_while_it_runs(
    conn, shelf: Path, smart: Pipeline, paths: AppPaths
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    learned = threading.Event()
    reported: list = []

    def on_learned(relocations) -> None:
        reported.extend(relocations)
        learned.set()

    service = WatchService(
        shelf,
        paths.db_path,
        stable_seconds=0.2,
        poll_interval=0.05,
        make_library=lambda c: LibraryIndex(c, shelf, FakeEmbedder()),
        on_learned=on_learned,
        learn_delay=0.2,
    )
    service.start()
    try:
        assert not learned.wait(1.0)  # nothing to learn at start
        os.rename(moved.dst, shelf / "Travel" / "hotel bill.txt")
        assert learned.wait(10)
    finally:
        service.stop()
    assert reported[0].to_folder == "Travel"
    assert repository.list_corrections(conn)[0]["source"] == "external"


# --- command line -----------------------------------------------------------------------------


def test_cli_correct_and_learned(
    conn, shelf: Path, smart: Pipeline, capsys: pytest.CaptureFixture[str]
) -> None:
    moved = smart.process(make(shelf / "hotel bill.txt", HOTEL))
    assert main(["learned"]) == 0
    assert "Nothing learned yet" in capsys.readouterr().out

    assert main(["correct", str(moved.history_id), "travel", "--root", str(shelf)]) == 0
    assert "and learned from it" in capsys.readouterr().out
    assert (shelf / "Travel" / "hotel bill.txt").exists()

    assert main(["learned"]) == 0
    assert "hotel bill.txt: Finance -> Travel  [corrected]" in capsys.readouterr().out
    assert main(["correct", str(moved.history_id), "Travel", "--root", str(shelf)]) == 1


def test_outcome_kinds_are_unchanged_by_learning(shelf: Path, smart: Pipeline) -> None:
    outcome = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    assert outcome.kind is OutcomeKind.MOVED
