from pathlib import Path

import pytest
from helpers import make

from autoshelf import __version__
from autoshelf.actions.mover import move_file
from autoshelf.cli import main


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: autoshelf" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_watch_without_root_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "--dry-run"]) == 2
    assert "--root" in capsys.readouterr().err


def test_watch_refuses_missing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["watch", "--root", str(tmp_path / "missing"), "--dry-run"]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_history_when_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["history"]) == 0
    assert "No history yet." in capsys.readouterr().out


def test_history_and_undo(conn, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "_Inbox").mkdir()
    moved = move_file(
        conn, make(root / "論文 draft.pdf"), root / "_Inbox", root=root, reason="test"
    )

    assert main(["history"]) == 0
    out = capsys.readouterr().out
    assert f"#{moved.history_id}" in out
    assert "論文 draft.pdf -> _Inbox" in out

    assert main(["undo", str(moved.history_id)]) == 0
    assert "Undid" in capsys.readouterr().out
    assert (root / "論文 draft.pdf").exists()

    assert main(["undo", str(moved.history_id)]) == 1
    assert "nothing to undo" in capsys.readouterr().err


def test_undo_last(conn, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (root / "_Inbox").mkdir()
    for name in ("a.txt", "b.txt"):
        move_file(conn, make(root / name), root / "_Inbox", root=root, reason="test")

    assert main(["undo", "--last", "5"]) == 0

    assert capsys.readouterr().out.count("Undid") == 2
    assert (root / "a.txt").exists()
    assert (root / "b.txt").exists()


def test_undo_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        main(["undo"])
