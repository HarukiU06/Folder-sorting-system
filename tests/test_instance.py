"""Only one process may sort a library at a time."""

import os
from pathlib import Path

import pytest

from autoshelf.instance import WatchLock


def test_second_holder_is_refused_until_released(tmp_path: Path) -> None:
    path = tmp_path / "data" / "watcher.lock"
    first, second = WatchLock(path), WatchLock(path)

    assert first.acquire()
    assert first.held
    assert not second.acquire()
    assert not second.held

    first.release()
    assert second.acquire()
    second.release()


def test_acquire_is_idempotent_for_the_holder(tmp_path: Path) -> None:
    lock = WatchLock(tmp_path / "watcher.lock")
    assert lock.acquire()
    assert lock.acquire()
    lock.release()
    lock.release()  # releasing twice is harmless
    assert not lock.held


def test_holder_pid_is_readable_by_others(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    with WatchLock(path):
        assert WatchLock(path).holder_pid() == os.getpid()


def test_context_manager_raises_when_taken(tmp_path: Path) -> None:
    path = tmp_path / "watcher.lock"
    with WatchLock(path), pytest.raises(RuntimeError, match="locked"), WatchLock(path):
        pass
