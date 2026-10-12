from pathlib import Path

import pytest
from helpers import make, windows_only

from autoshelf.fsutil import is_locked
from autoshelf.watcher.selfmoves import SelfMoveRegistry
from autoshelf.watcher.stability import StabilityTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def tracker_for(clock: FakeClock, locked: set[Path] | None = None, **kw) -> StabilityTracker:
    locked = locked if locked is not None else set()
    return StabilityTracker(3.0, clock=clock, is_locked=lambda p: p in locked, **kw)


def test_file_is_ready_after_stable_window(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    tracker = tracker_for(clock)
    tracker.touch(path)

    assert tracker.poll() == []  # first look records size and mtime
    clock.now += 2.9
    assert tracker.poll() == []
    clock.now += 0.2
    assert tracker.poll() == [path]
    assert len(tracker) == 0


def test_growing_file_restarts_the_window(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf", "part")
    tracker = tracker_for(clock)
    tracker.touch(path)
    tracker.poll()

    clock.now += 2.0
    path.write_text("part and more")
    assert tracker.poll() == []
    clock.now += 2.0
    assert tracker.poll() == []
    clock.now += 1.1
    assert tracker.poll() == [path]


def test_repeated_events_are_debounced(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    tracker = tracker_for(clock)
    for _ in range(5):
        tracker.touch(path)
    tracker.poll()
    clock.now += 3.0
    assert tracker.poll() == [path]
    assert tracker.poll() == []


def test_locked_file_waits_until_released(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    locked = {path}
    tracker = tracker_for(clock, locked)
    tracker.touch(path)
    tracker.poll()
    clock.now += 10
    assert tracker.poll() == []
    locked.clear()
    assert tracker.poll() == [path]


def test_vanished_file_is_dropped(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    tracker = tracker_for(clock)
    tracker.touch(path)
    path.unlink()
    assert tracker.poll() == []
    assert len(tracker) == 0


def test_gives_up_after_max_wait(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    tracker = tracker_for(clock, {path}, max_wait=60)
    tracker.touch(path)
    tracker.poll()
    clock.now += 61
    assert tracker.poll() == []
    assert len(tracker) == 0


def test_discard_stops_tracking(tmp_path: Path, clock: FakeClock) -> None:
    path = make(tmp_path / "a.pdf")
    tracker = tracker_for(clock)
    tracker.touch(path)
    tracker.discard(path)
    clock.now += 10
    assert tracker.poll() == []


@windows_only
def test_is_locked_detects_a_writer(tmp_path: Path) -> None:
    path = make(tmp_path / "downloading.pdf")
    assert not is_locked(path)
    with open(path, "ab"):
        assert is_locked(path)
    assert not is_locked(path)


def test_is_locked_ignores_readers(tmp_path: Path) -> None:
    path = make(tmp_path / "preview.pdf")
    with open(path, "rb"):
        assert not is_locked(path)


def test_self_move_registry_expires(clock: FakeClock, root: Path) -> None:
    registry = SelfMoveRegistry(ttl=30, clock=clock)
    registry.expect(root / "a.pdf", root / "_Inbox" / "a.pdf")
    assert registry.is_expected(root / "_Inbox" / "a.pdf")
    assert not registry.is_expected(root / "b.pdf")
    clock.now += 31
    assert not registry.is_expected(root / "a.pdf")


@windows_only
def test_self_move_registry_ignores_case_on_windows(clock: FakeClock, root: Path) -> None:
    registry = SelfMoveRegistry(clock=clock)
    registry.expect(root / "_Inbox" / "a.pdf")
    assert registry.is_expected(root / "_INBOX" / "A.PDF")
