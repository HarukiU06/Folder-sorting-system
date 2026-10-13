"""Tidying up: duplicates and versions, organizing a folder, stats, and watched folders
(SPEC 5.6, 5.7, 5.9, 5.12)."""

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.mover import MoveError, move_file
from autoshelf.actions.undo import UndoError, undo, undo_batch
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths, ConfigError, validate_outside_folder
from autoshelf.db import repository
from autoshelf.features import organize
from autoshelf.features.duplicates import (
    DuplicateError,
    base_name,
    exact_duplicates,
    keep_one,
    name_similarity,
    possible_versions,
)
from autoshelf.features.stats import library_stats
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import OutcomeKind, Pipeline
from autoshelf.watcher.service import WatchService


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank transfer")
    make(root / "Recipes" / "cake.txt", "recipe cooking baking oven cake flour sugar")
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank")
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking oven")
    return root


@pytest.fixture
def library(conn, shelf: Path) -> LibraryIndex:
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    return index


# --- duplicates ---------------------------------------------------------------------------


def test_exact_duplicates_are_found_and_extra_copies_set_aside(conn, shelf, library) -> None:
    copy = make(shelf / "Recipes" / "invoice copy.txt", "invoice total tax due bank transfer")
    make(shelf / "_Duplicates" / "old.txt", "invoice total tax due bank transfer")
    library.refresh()

    (group,) = exact_duplicates(conn, shelf)
    original = shelf / "Finance" / "invoice 2024.txt"
    assert set(group.paths) == {original, copy}  # _Duplicates itself isn't searched

    moved = keep_one(conn, shelf, original, [copy])

    assert original.exists()
    assert not copy.exists()
    assert (shelf / "_Duplicates" / "invoice copy (2).txt").exists() or (
        shelf / "_Duplicates" / "invoice copy.txt"
    ).exists()
    entry = repository.get_history(conn, moved[0].history_id)
    assert entry.reason == "duplicate of invoice 2024.txt"
    assert entry.batch_id
    undo_batch(conn, entry.batch_id)
    assert copy.exists()
    assert exact_duplicates(conn, shelf) != []


def test_keep_one_refuses_files_that_are_no_longer_identical(conn, shelf, library) -> None:
    copy = make(shelf / "Recipes" / "invoice copy.txt", "invoice total tax due bank transfer")
    copy.write_text("edited meanwhile", encoding="utf-8")
    with pytest.raises(DuplicateError, match="no longer an exact copy"):
        keep_one(conn, shelf, shelf / "Finance" / "invoice 2024.txt", [copy])
    assert copy.exists()
    assert not (shelf / "_Duplicates").exists()


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Report_v2.docx", "report final.docx"),
        ("notes 2.pdf", "notes.pdf"),  # an iCloud conflict copy
        ("Thesis (3).pdf", "thesis-draft.pdf"),
        ("企画書 コピー.docx", "企画書.docx"),
    ],
)
def test_version_markers_are_ignored_in_names(a: str, b: str) -> None:
    assert base_name(a) == base_name(b)


def test_name_similarity() -> None:
    assert name_similarity("Report_v2.docx", "report_v3.docx") == 1.0
    assert name_similarity("Report.docx", "Budget.xlsx") < 0.5


def test_possible_versions_group_similar_files_with_similar_names(conn, shelf, library) -> None:
    text = "quarterly sales report revenue grew costs flat outlook positive"
    make(shelf / "Finance" / "sales report v1.txt", text)
    make(shelf / "Finance" / "sales report final.txt", text + " q3")
    make(shelf / "Finance" / "unrelated.txt", text + " q3")  # same text, different name
    make(shelf / "Finance" / "exact copy.txt", "invoice total tax due bank transfer")
    library.refresh()

    (group,) = possible_versions(conn, shelf, "fake-model")

    assert {path.name for path in group.paths} == {"sales report final.txt", "sales report v1.txt"}
    assert group.similarity > 0.85


# --- the mover and folders outside the root ---------------------------------------------------


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads ダウンロード"
    folder.mkdir()
    return folder.resolve()


def test_files_outside_the_root_need_an_opted_in_folder(conn, shelf, downloads) -> None:
    src = make(downloads / "paper.pdf")
    with pytest.raises(MoveError, match="outside the root"):
        move_file(conn, src, shelf / "Finance", root=shelf, reason="auto")
    result = move_file(
        conn, src, shelf / "Finance", root=shelf, reason="auto", allowed_source=downloads
    )
    entry = repository.get_history(conn, result.history_id)
    assert entry.details["source_folder"] == str(downloads)

    undo(conn, result.history_id)  # back where it came from

    assert src.exists()
    other = make(downloads.parent / "Elsewhere" / "x.pdf")
    with pytest.raises(MoveError):
        move_file(
            conn, other, shelf / "Finance", root=shelf, reason="auto", allowed_source=downloads
        )


def test_outside_folders_are_validated(shelf: Path, downloads: Path, paths: AppPaths) -> None:
    assert validate_outside_folder(downloads, shelf, paths) == downloads
    for bad, message in (
        (shelf / "Finance", "inside the folder AutoShelf sorts"),
        (shelf.parent, "or contains it"),
        (paths.home, "AutoShelf's own data"),
        (Path.home(), "whole home folder"),
        (Path(shelf.anchor), "whole drive"),
        (downloads / "missing", "does not exist"),
    ):
        with pytest.raises(ConfigError, match=message):
            validate_outside_folder(bad, shelf, paths)


# --- organize ---------------------------------------------------------------------------------


def test_organize_previews_then_applies_one_undoable_batch(conn, shelf, library) -> None:
    make(shelf / "tax invoice.txt", "invoice total tax due bank")
    make(shelf / "banana bread.txt", "recipe baking oven flour banana")
    make(shelf / "notes.txt", "call mom buy milk")
    make(shelf / "~$lock.txt", "ignored")

    proposals = organize.plan(conn, shelf, library, shelf, threshold=0.3, margin=0.1)

    by_name = {item.path.name: item for item in proposals}
    assert set(by_name) == {"tax invoice.txt", "banana bread.txt", "notes.txt"}
    assert by_name["tax invoice.txt"].target == "Finance"
    assert by_name["tax invoice.txt"].selected
    assert not by_name["notes.txt"].selected  # unclear: a guess, not ticked
    assert (shelf / "tax invoice.txt").exists()  # the preview moved nothing

    by_name["banana bread.txt"].target = "Finance"  # the user overrides one row

    result = organize.apply(conn, shelf, proposals)

    assert len(result.moved) == 2
    assert (shelf / "Finance" / "tax invoice.txt").exists()
    assert (shelf / "Finance" / "banana bread.txt").exists()
    assert (shelf / "notes.txt").exists()
    entries = repository.list_batch(conn, result.batch_id)
    assert {entry.reason for entry in entries} == {"organize"}
    (correction,) = repository.list_corrections(conn)
    assert (correction["from_folder"], correction["to_folder"]) == ("Recipes", "Finance")

    undone = undo_batch(conn, result.batch_id)

    assert len(undone) == 2
    assert (shelf / "tax invoice.txt").exists()
    with pytest.raises(UndoError):
        undo_batch(conn, result.batch_id)


def test_organizing_a_folder_skips_files_already_in_place(conn, shelf, library) -> None:
    make(shelf / "Recipes" / "tax invoice.txt", "invoice total tax due bank")
    proposals = organize.plan(conn, shelf, library, shelf / "Recipes", threshold=0.3, margin=0.1)
    assert [item.path.name for item in proposals] == ["tax invoice.txt"]
    assert proposals[0].current == "Recipes"


def test_organizing_an_outside_folder(conn, shelf, library, downloads) -> None:
    make(downloads / "tax invoice.txt", "invoice total tax due bank")
    make(downloads / "sub" / "cake 2.txt", "recipe baking oven cake")

    flat = organize.plan(conn, shelf, library, downloads, threshold=0.3, margin=0.1)
    deep = organize.plan(conn, shelf, library, downloads, recursive=True, threshold=0.3, margin=0.1)
    assert [item.path.name for item in flat] == ["tax invoice.txt"]
    assert len(deep) == 2

    refused = organize.apply(conn, shelf, deep)  # without saying which folder: refused
    assert refused.moved == [] and len(refused.failed) == 2
    result = organize.apply(conn, shelf, deep, folder=downloads)
    assert len(result.moved) == 2
    undo_batch(conn, result.batch_id)
    assert (downloads / "sub" / "cake 2.txt").exists()


# --- stats -------------------------------------------------------------------------------------


def test_stats(conn, shelf, library) -> None:
    pipeline = Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)
    kept = pipeline.process(make(shelf / "tax invoice.txt", "invoice total tax due bank"))
    wrong = pipeline.process(make(shelf / "invoice 2.txt", "invoice total tax due bank"))
    undo(conn, wrong.history_id)
    pipeline.process(make(shelf / "notes.txt", "call mom buy milk"))  # to the Inbox
    assert kept.kind is OutcomeKind.MOVED

    stats = library_stats(conn, shelf, now=datetime.now(UTC))

    assert stats.total_files == 3
    assert dict(stats.files_per_folder) == {"Finance": 2, "Recipes": 1}
    assert stats.inbox == 1
    assert (stats.auto_moves, stats.auto_corrected) == (2, 1)
    assert stats.accuracy == 0.5
    assert stats.added_per_month[-1][1] == 3
    assert len(stats.added_per_month) == 12
    assert stats.stale == []

    later = library_stats(conn, shelf, now=datetime.now(UTC) + timedelta(days=400))
    assert later.stale_total == 3


# --- watched folders ------------------------------------------------------------------------


def test_new_file_in_a_watched_folder_is_moved_in_and_sorted(
    conn, shelf, library, downloads
) -> None:
    pipeline = Pipeline(
        conn,
        shelf,
        library=library,
        threshold=0.3,
        margin=0.1,
        watched=[downloads],
        watched_extensions=[".txt"],
    )
    src = make(downloads / "tax invoice.txt", "invoice total tax due bank")

    outcome = pipeline.process(src)

    assert outcome.dst == shelf / "Finance" / "tax invoice.txt"
    assert outcome.message.startswith("from Downloads ダウンロード:")
    undo(conn, outcome.history_id)
    assert src.exists()
    assert pipeline.process(src).kind is OutcomeKind.SKIPPED  # put back: left alone
    installer = make(downloads / "setup.exe")
    assert pipeline.process(installer).kind is OutcomeKind.SKIPPED
    nested = make(downloads / "sub" / "tax 2.txt", "invoice total tax")
    assert pipeline.process(nested).kind is OutcomeKind.SKIPPED


def test_watcher_takes_only_new_files_from_watched_folders(
    conn, shelf, downloads, paths: AppPaths
) -> None:
    old = make(downloads / "old invoice.txt", "invoice total tax due bank")
    arrived = threading.Event()
    outcomes = []

    def on_outcome(outcome) -> None:
        outcomes.append(outcome)
        arrived.set()

    service = WatchService(
        shelf,
        paths.db_path,
        stable_seconds=0.2,
        poll_interval=0.05,
        make_library=lambda c: LibraryIndex(c, shelf, FakeEmbedder()),
        threshold=0.3,
        margin=0.1,
        on_outcome=on_outcome,
        watched=[downloads],
        watched_extensions=[".txt"],
    )
    service.start()
    try:
        make(downloads / "new invoice.txt", "invoice total tax due bank")
        assert arrived.wait(10)
    finally:
        service.stop()
    assert outcomes[0].dst == shelf / "Finance" / "new invoice.txt"
    assert old.exists()  # was there before: not taken


def test_app_service_watched_folders(paths: AppPaths, shelf: Path, downloads: Path) -> None:
    service = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    service.set_root(shelf)
    assert service.add_watched(downloads) == downloads
    service.add_watched(downloads)
    assert service.settings().watched_folders == [str(downloads)]
    with pytest.raises(ConfigError):
        service.add_watched(shelf / "Finance")
    service.remove_watched(downloads)
    assert service.settings().watched_folders == []


# --- command line --------------------------------------------------------------------------


def test_cli_tidy_commands(
    paths: AppPaths, shelf: Path, fake_model, capsys: pytest.CaptureFixture[str]
) -> None:
    service = AppService(paths)
    service.set_root(shelf)
    copy = make(shelf / "Recipes" / "invoice copy.txt", "invoice total tax due bank transfer")
    make(shelf / "tax invoice.txt", "invoice total tax due bank")
    service.save_settings(replace(service.settings(), auto_threshold=0.3, auto_margin=0.1))

    assert main(["duplicates"]) == 0
    assert "2 identical files" in capsys.readouterr().out
    assert main(["duplicates", "--keep", str(shelf / "Finance" / "invoice 2024.txt")]) == 0
    output = capsys.readouterr().out
    assert "Undo with: autoshelf undo --batch" in output
    assert not copy.exists()
    batch = output.rsplit("--batch ", 1)[1].strip()
    assert main(["undo", "--batch", batch]) == 0
    assert copy.exists()

    assert main(["organize", str(shelf)]) == 0
    assert "[x] " in capsys.readouterr().out
    assert (shelf / "tax invoice.txt").exists()
    assert main(["organize", str(shelf), "--apply"]) == 0
    assert "Moved 1 file(s)." in capsys.readouterr().out
    assert (shelf / "Finance" / "tax invoice.txt").exists()

    assert main(["stats"]) == 0
    assert "Files in your folders: 4" in capsys.readouterr().out
    assert main(["versions"]) == 0
    assert main(["config", "--watch", str(shelf / "Finance")]) == 1
    assert "inside the folder AutoShelf sorts" in capsys.readouterr().err


def test_cli_watch_also_watches_opted_in_folders(
    paths: AppPaths, shelf: Path, downloads: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf import cli
    from autoshelf.watcher import service as service_module

    started: list[dict] = []

    class FakeWatchService:
        def __init__(self, root, db_path, **kwargs) -> None:
            started.append(kwargs)

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(service_module, "WatchService", FakeWatchService)
    monkeypatch.setattr(cli.time, "sleep", interrupt)
    service = AppService(paths)
    service.set_root(shelf)
    service.add_watched(downloads)

    assert main(["watch"]) == 0

    assert started[0]["watched"] == [downloads]
    assert ".pdf" in started[0]["watched_extensions"]
