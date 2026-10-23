"""End-to-end tests with a real watchdog observer on a temporary folder."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Outcome
from autoshelf.watcher.service import WatchService

TIMEOUT = 10.0


class Recorder:
    """Collects pipeline outcomes from the worker thread."""

    def __init__(self) -> None:
        self.outcomes: list[Outcome] = []
        self._changed = threading.Condition()

    def __call__(self, outcome: Outcome) -> None:
        with self._changed:
            self.outcomes.append(outcome)
            self._changed.notify_all()

    def wait_for(self, predicate: Callable[[list[Outcome]], bool]) -> list[Outcome]:
        with self._changed:
            assert self._changed.wait_for(lambda: predicate(self.outcomes), timeout=TIMEOUT), (
                f"timed out; outcomes so far: {self.outcomes}"
            )
            return list(self.outcomes)


@pytest.fixture
def run_service(root: Path, paths: AppPaths) -> Iterator[Callable[..., Recorder]]:
    services: list[WatchService] = []

    def start(**kwargs) -> Recorder:
        recorder = Recorder()
        service = WatchService(
            root,
            paths.db_path,
            stable_seconds=0.3,
            poll_interval=0.05,
            on_outcome=recorder,
            **kwargs,
        )
        service.start()
        services.append(service)
        recorder.service = service
        return recorder

    yield start
    for service in services:
        service.stop()


def outcome_for(name: str) -> Callable[[list[Outcome]], bool]:
    return lambda outcomes: any(o.path.name == name for o in outcomes)


def test_dropped_file_is_moved_to_inbox(root: Path, run_service) -> None:
    recorder = run_service()
    make(root / "Neural networks ニューラル.pdf", "backprop")
    make(root / "Finance" / "invoice.pdf")
    make(root / "~$lock.docx")

    recorder.wait_for(outcome_for("Neural networks ニューラル.pdf"))

    assert (root / "_Inbox" / "Neural networks ニューラル.pdf").exists()
    assert (root / "Finance" / "invoice.pdf").exists()
    assert (root / "~$lock.docx").exists()


def test_files_present_at_startup_are_processed(root: Path, run_service) -> None:
    make(root / "waiting since yesterday.txt")
    recorder = run_service()
    recorder.wait_for(outcome_for("waiting since yesterday.txt"))
    assert (root / "_Inbox" / "waiting since yesterday.txt").exists()


def test_own_move_into_inbox_is_not_processed_again(root: Path, run_service) -> None:
    recorder = run_service()
    make(root / "a.txt")
    recorder.wait_for(outcome_for("a.txt"))
    time.sleep(1.0)  # give the watcher time to (wrongly) pick up the moved file
    assert [o.path.name for o in recorder.outcomes] == ["a.txt"]


def test_new_file_at_a_just_vacated_path_is_still_processed(root: Path, run_service) -> None:
    recorder = run_service()
    make(root / "scan.pdf", "first scan")
    recorder.wait_for(outcome_for("scan.pdf"))
    make(root / "scan.pdf", "second scan")  # same name, seconds later

    recorder.wait_for(lambda outcomes: len(outcomes) >= 2)

    assert (root / "_Inbox" / "scan.pdf").read_text() == "first scan"
    assert (root / "_Inbox" / "scan (2).pdf").read_text() == "second scan"


def test_dry_run_moves_nothing(root: Path, run_service) -> None:
    recorder = run_service(dry_run=True)
    make(root / "report.pdf")
    outcomes = recorder.wait_for(outcome_for("report.pdf"))
    assert outcomes[0].kind == "would move"
    assert (root / "report.pdf").exists()
    assert not (root / "_Inbox").exists()


def test_clear_match_is_sorted_into_its_folder(root: Path, conn, run_service) -> None:
    make(root / "Recipes" / "curry.txt", "recipe curry onion simmer")
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking")
    (root / "Finance").mkdir()

    recorder = run_service(
        make_library=lambda c: LibraryIndex(c, root, FakeEmbedder()), threshold=0.3, margin=0.1
    )
    make(root / "banana bread recipe.txt", "recipe baking banana oven")
    make(root / "random.txt", "call mom")

    recorder.wait_for(lambda outcomes: len(outcomes) >= 2)

    assert (root / "Recipes" / "banana bread recipe.txt").exists()
    assert (root / "_Inbox" / "random.txt").exists()


def test_failing_library_means_every_file_waits_in_inbox(
    root: Path, run_service, caplog: pytest.LogCaptureFixture
) -> None:
    (root / "Recipes").mkdir()

    def broken(conn):
        raise RuntimeError("no model for you")

    recorder = run_service(make_library=broken)
    make(root / "banana bread recipe.txt", "recipe")
    recorder.wait_for(outcome_for("banana bread recipe.txt"))

    assert (root / "_Inbox" / "banana bread recipe.txt").exists()
    assert "Sorting by content is unavailable (no model for you)" in caplog.text


def test_paused_service_holds_files_until_resumed(root: Path, run_service) -> None:
    recorder = run_service(paused=True)
    service = recorder.service
    src = make(root / "report.pdf")
    time.sleep(1.0)
    assert src.exists()
    assert recorder.outcomes == []
    assert service.paused

    service.resume()

    recorder.wait_for(outcome_for("report.pdf"))
    assert (root / "_Inbox" / "report.pdf").exists()


def test_file_deleted_while_paused_is_forgotten(root: Path, run_service) -> None:
    recorder = run_service(paused=True)
    service = recorder.service
    src = make(root / "report.pdf")
    time.sleep(1.0)
    src.unlink()
    time.sleep(0.3)
    service.resume()
    time.sleep(1.0)
    assert recorder.outcomes == []


# --- the periodic sweep and rescanning only when needed (Phase 9) -------------------------------


def test_sweep_offers_loose_files_but_not_known_unchanged_ones(
    root: Path, conn, paths: AppPaths
) -> None:
    from autoshelf.pipeline import Pipeline

    service = WatchService(root, paths.db_path)
    waiting = Pipeline(conn, root).process(make(root / "_Inbox" / "from iPhone.pdf", "x"))
    assert waiting.kind == "waiting"  # recorded as waiting in _Inbox
    undone = make(root / "undone.pdf", "put back by an undo")
    record_id = repository.upsert_file(
        conn, undone, sha256=None, size=undone.stat().st_size, mtime=undone.stat().st_mtime
    )
    repository.set_file_status(conn, record_id, "ignored")
    missed = make(root / "missed.pdf", "its event was lost")

    assert service.offer_loose_files(conn) == 1
    assert service.tracker.poll() == []  # not stable yet, but tracked:
    assert len(service.tracker) == 1

    # A waiting file that changed is looked at again.
    (root / "_Inbox" / "from iPhone.pdf").write_text("edited on the phone", encoding="utf-8")
    assert service.offer_loose_files(conn) == 2
    assert missed.exists()


def test_dry_run_sweep_reports_each_file_once(root: Path, conn, paths: AppPaths) -> None:
    from autoshelf.pipeline import Outcome, OutcomeKind

    service = WatchService(root, paths.db_path, dry_run=True)
    report = make(root / "report.pdf", "quarterly report")
    service._report(Outcome(OutcomeKind.WOULD_MOVE, report, "would wait in _Inbox"))
    assert service.offer_loose_files(conn) == 0
    report.write_text("quarterly report, revised", encoding="utf-8")
    assert service.offer_loose_files(conn) == 1


def test_folder_events_mark_the_library_for_a_rescan(root: Path, paths: AppPaths) -> None:
    service = WatchService(root, paths.db_path)
    dirty = service._library_dirty

    for reserved in (root / "_Inbox", root / "_Duplicates"):
        service.folder_changed(reserved)
    service.forget(root / "_Inbox" / "a.pdf", removed=True)
    # AutoShelf's own move (Windows reports one between folders as deleted + created).
    service.self_moves.expect_move(root / "Finance" / "a.pdf", root / "Travel" / "a.pdf")
    service.forget(root / "Finance" / "a.pdf", removed=True)
    assert not dirty.is_set()

    service.folder_changed(root / "New folder")  # appeared empty: no file event follows
    assert dirty.is_set()
    dirty.clear()
    service.forget(root / "Finance" / "Old" / "a.pdf", removed=True)  # file or folder: rescan
    assert dirty.is_set()
    dirty.clear()
    service.forget(root / "Finance", removed=True)  # Windows says "deleted", not "folder"
    assert dirty.is_set()


def test_a_new_empty_folder_is_sorted_into_right_away(root: Path, run_service) -> None:
    (root / "Finance").mkdir()
    recorder = run_service(
        make_library=lambda c: LibraryIndex(c, root, FakeEmbedder()), threshold=0.3, margin=0.1
    )
    make(root / "invoice.txt", "invoice total tax due bank")
    recorder.wait_for(outcome_for("invoice.txt"))

    # Only a folder event says it exists: no file event, no description (no version bump).
    (root / "Recipe").mkdir()
    time.sleep(0.5)
    make(root / "banana bread recipe.txt", "recipe baking banana oven")
    recorder.wait_for(outcome_for("banana bread recipe.txt"))

    assert (root / "Recipe" / "banana bread recipe.txt").exists()


def test_the_sweep_finds_files_whose_events_were_lost(
    root: Path, run_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.watcher import service as service_module

    # Windows dropped every event on this busy folder.
    monkeypatch.setattr(service_module._EventHandler, "on_any_event", lambda self, event: None)
    recorder = run_service(sweep_interval=0.5)
    time.sleep(0.3)  # after the files present at the start were looked at
    make(root / "arrived quietly.pdf", "no event for this one")

    recorder.wait_for(outcome_for("arrived quietly.pdf"))
    assert (root / "_Inbox" / "arrived quietly.pdf").exists()


def test_sorting_dropped_files_doesnt_rescan_the_library(
    root: Path, conn, run_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ml import library as library_module

    make(root / "Recipes" / "curry.txt", "recipe curry onion simmer")
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking oven")
    (root / "Finance").mkdir()
    scans: list[int] = []
    real = library_module.scan_library
    monkeypatch.setattr(
        library_module, "scan_library", lambda *args, **kw: scans.append(1) or real(*args, **kw)
    )
    recorder = run_service(
        make_library=lambda c: LibraryIndex(c, root, FakeEmbedder()), threshold=0.3, margin=0.1
    )
    for name in ("banana bread recipe.txt", "apple pie recipe.txt", "plum cake recipe.txt"):
        make(root / name, "recipe baking oven sugar")
        recorder.wait_for(outcome_for(name))
    time.sleep(2.5)  # past the delay after which changed folders are rescanned

    assert len(list((root / "Recipes").iterdir())) == 4
    assert len(scans) == 1  # only when the watcher started
