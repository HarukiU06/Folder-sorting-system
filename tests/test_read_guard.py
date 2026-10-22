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


# --- listing and clearing skipped files (Phase 9) -------------------------------------------


def test_skipped_files_can_be_listed_and_read_again(tmp_path: Path) -> None:
    first = make(tmp_path / "power cut.txt", "an innocent file")
    second = make(tmp_path / "really broken.txt", "a crashing file")
    crash_while_reading(tmp_path, first)
    crash_while_reading(tmp_path, second)
    guard.start(tmp_path, record=True)

    listed = guard.skipped_files(tmp_path)
    assert [item.path for item in listed] == [first, second]
    assert guard.newly_skipped() == [second]  # the run that crashed on it found the first

    assert guard.clear(tmp_path, [first]) == 1
    assert extract(first).text == "an innocent file"
    assert extract(second).error == guard.CRASHED
    assert guard.clear(tmp_path) == 1  # all the rest
    assert guard.skipped_files(tmp_path) == []
    assert extract(second).text == "a crashing file"


def test_a_skipped_file_is_cleared_where_it_is_now(tmp_path: Path) -> None:
    doc = make(tmp_path / "scan.pdf.txt", "scanned text")
    crash_while_reading(tmp_path, doc)
    guard.start(tmp_path, record=True)
    moved = doc.rename(tmp_path / "sorted by name.txt")  # sorted by its name since

    assert guard.clear(tmp_path, [moved]) == 1  # matched by size and time, not the old name
    assert extract(moved).text == "scanned text"


def test_clearing_from_another_process_reaches_the_running_app(tmp_path: Path) -> None:
    doc = make(tmp_path / "agenda.txt", "agenda")
    other = make(tmp_path / "notes.txt", "notes")
    crash_while_reading(tmp_path, doc)
    running = guard.start(tmp_path, record=True)  # the app
    assert running.skips(doc)

    # `autoshelf skipped --clear` in a terminal: a process without the app's guard.
    state_file = tmp_path / guard.STATE_FILENAME
    before = state_file.stat().st_mtime_ns
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from autoshelf.extract import guard\n"
        "print(guard.clear(Path(sys.argv[1])))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=120
    )
    assert done.stdout.strip() == "1"
    assert state_file.stat().st_mtime_ns != before

    assert not running.skips(doc)
    with running.reading(other):  # the app's next save keeps the cleared list
        pass
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["skipped"] == []


def test_the_service_and_the_command_line_list_and_clear(
    paths: AppPaths, root: Path, conn, capsys
) -> None:
    from autoshelf.cli import main
    from autoshelf.db import repository

    doc = make(root / "agenda.txt", "agenda")
    crash_while_reading(paths.home, doc)
    service = AppService(paths)
    assert service.acquire()
    try:
        assert service.newly_skipped() == [doc]
        moved = doc.rename(root / "Agenda 2026.txt")
        st = moved.stat()
        repository.upsert_file(conn, moved, sha256=None, size=st.st_size, mtime=st.st_mtime)
        assert [item.path for item in service.skipped_files()] == [moved]  # where it is now

        assert main(["skipped"]) == 0
        assert "Agenda 2026.txt" in capsys.readouterr().out
        assert main(["skipped", str(moved)]) == 2  # files only with --clear
        assert main(["skipped", "--clear", str(moved)]) == 0
        assert "1 file(s) will be read again" in capsys.readouterr().out
        assert service.skipped_files() == []
        assert extract(moved).text == "agenda"
        assert main(["skipped"]) == 0
        assert "No skipped files" in capsys.readouterr().out
    finally:
        service.release()
