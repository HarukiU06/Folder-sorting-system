"""Files that crashed AutoShelf while being read are only sorted by name afterwards."""

import json
import subprocess
import sys
from pathlib import Path

from helpers import make

from autoshelf.app_service import AppService
from autoshelf.config import AppPaths
from autoshelf.extract import extract, guard

# Reads that never finish. Kept referenced: a context manager that is garbage-collected
# runs its cleanup, which a process that died never does.
_unfinished = []


def start_reading(context) -> None:
    context.__enter__()
    _unfinished.append(context)


def crash_while_reading(home: Path, path: Path) -> None:
    """What a sorting process leaves behind when it dies while reading ``path``."""
    start_reading(guard.ReadGuard(home / guard.STATE_FILENAME, record=True).reading(path))


def test_a_file_that_crashed_a_read_is_only_sorted_by_name(tmp_path: Path) -> None:
    doc = make(tmp_path / "plans.txt", "budget plans")
    crash_while_reading(tmp_path, doc)

    guard.start(tmp_path, record=True)  # the next start
    result = extract(doc)
    assert result.error == guard.CRASHED
    assert result.text == ""
    assert result.filename_tokens == ("plans",)
    state = json.loads((tmp_path / guard.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["reading"] == []
    assert [entry["path"] for entry in state["skipped"]] == [str(doc)]

    # It stays skipped after a move (same content), until its content changes.
    moved = doc.rename(tmp_path / "moved.txt")
    assert extract(moved).error == guard.CRASHED
    moved.write_text("budget plans for 2026", encoding="utf-8")
    assert extract(moved).text == "budget plans for 2026"


def test_a_real_crash_while_reading_is_remembered(tmp_path: Path) -> None:
    doc = make(tmp_path / "evil.txt", "boom")
    script = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "from autoshelf.extract import extract, guard, plaintext\n"
        "plaintext.extract = lambda file, max_chars: os._exit(3)  # a parser takes it down\n"
        "guard.start(Path(sys.argv[1]), record=True)\n"
        "extract(Path(sys.argv[2]))\n"
    )
    done = subprocess.run([sys.executable, "-c", script, str(tmp_path), str(doc)], timeout=120)
    assert done.returncode == 3

    guard.start(tmp_path, record=True)
    assert extract(doc).error == guard.CRASHED


def test_files_are_listed_only_while_being_read(tmp_path: Path) -> None:
    doc = make(tmp_path / "notes.txt", "notes")
    current = guard.start(tmp_path, record=True)
    state_file = tmp_path / guard.STATE_FILENAME
    with current.reading(doc):
        listed = json.loads(state_file.read_text(encoding="utf-8"))["reading"]
        assert [entry["path"] for entry in listed] == [str(doc)]
    assert json.loads(state_file.read_text(encoding="utf-8"))["reading"] == []
    assert extract(doc).text == "notes"
    assert json.loads(state_file.read_text(encoding="utf-8"))["reading"] == []


def test_a_clean_exit_during_a_read_is_not_a_crash(tmp_path: Path) -> None:
    doc = make(tmp_path / "notes.txt", "notes")
    guard.start(tmp_path, record=True)
    start_reading(guard.reading(doc))  # still being read when the app quits
    guard.stop()
    guard.start(tmp_path, record=True)
    assert not guard.crashed_before(doc)


def test_only_the_sorting_process_treats_leftovers_as_crashes(tmp_path: Path) -> None:
    doc = make(tmp_path / "notes.txt", "notes")
    crash_while_reading(tmp_path, doc)  # or: the app is reading it right now
    guard.start(tmp_path, record=False)  # e.g. a command line tool
    assert extract(doc).text == "notes"
    state = json.loads((tmp_path / guard.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["skipped"] == []
    assert [entry["path"] for entry in state["reading"]] == [str(doc)]


def test_a_damaged_state_file_is_ignored(tmp_path: Path) -> None:
    doc = make(tmp_path / "notes.txt", "notes")
    (tmp_path / guard.STATE_FILENAME).write_text("{not json", encoding="utf-8")
    guard.start(tmp_path, record=True)
    assert extract(doc).text == "notes"
    (tmp_path / guard.STATE_FILENAME).write_text(
        json.dumps({"reading": [{"path": 1}], "skipped": "x"}), encoding="utf-8"
    )
    guard.start(tmp_path, record=True)
    assert not guard.crashed_before(doc)


def test_the_app_skips_what_crashed_its_last_run(paths: AppPaths, root: Path) -> None:
    doc = make(root / "agenda.txt", "agenda")
    crash_while_reading(paths.home, doc)
    service = AppService(paths)
    assert service.acquire()
    try:
        assert extract(doc).error == guard.CRASHED
    finally:
        service.release()
    assert extract(doc).text == "agenda"  # released: the guard is no longer used
