"""End-to-end tests with a real watchdog observer on a temporary folder."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from helpers import make

from autoshelf.config import AppPaths
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
