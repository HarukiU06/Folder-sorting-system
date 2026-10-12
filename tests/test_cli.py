from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf import __version__
from autoshelf.actions.mover import move_file
from autoshelf.cli import format_history_entry, main
from autoshelf.db import repository
from autoshelf.ml import embedder as embedder_module
from autoshelf.ui import app as ui_app


def test_no_command_starts_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(ui_app, "run_app", lambda **kwargs: calls.append(kwargs) or 0)
    assert main(["-v"]) == 0
    assert calls == [{"verbose": True}]


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
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


def test_extract_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = make(tmp_path / "PCA_notes.md", "Principal components explain variance.")
    assert main(["extract", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Extractor: text" in out
    assert "Filename:  PCA, notes" in out
    assert "Principal components explain variance." in out


def test_extract_command_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["extract", str(tmp_path / "missing.pdf")]) == 1
    assert "not a file" in capsys.readouterr().err


# --- Phase 3: folders, classification and the Inbox ---------------------------------


@pytest.fixture
def fake_model(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    """Replace the language model with the fake one for commands that load it."""
    embedder = FakeEmbedder()
    monkeypatch.setattr(embedder_module, "load_embedder", lambda name, cache_dir: embedder)
    return embedder


@pytest.fixture
def shelf(root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due")
    (root / "Recipes" / "Baking").mkdir(parents=True)
    return root


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize("command", ["folders", "inbox", "index", "classify"])
def test_commands_need_a_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    extra = [str(make(tmp_path / "a.txt"))] if command == "classify" else []
    code, _, err = run(capsys, command, *extra)
    assert code == 2
    assert "--root" in err


def test_config_sets_and_shows_settings(shelf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(
        capsys, "config", "--root", str(shelf), "--threshold", "0.5", "--margin", "0.05"
    )
    assert code == 0
    assert f"Root folder:      {shelf}" in out
    assert "Auto threshold:   0.50" in out

    code, out, _ = run(capsys, "folders")  # uses the configured root
    assert code == 0
    assert "Finance" in out


def test_config_rejects_out_of_range_values(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "config", "--threshold", "1.5")
    assert code == 1
    assert "between 0 and 1" in err


def test_folders_lists_counts_and_descriptions(
    shelf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "describe", "--root", str(shelf), "recipes/baking", "bread and cakes")
    code, out, _ = run(capsys, "folders", "--root", str(shelf))
    assert code == 0
    assert "Finance" in out and "1 file" in out
    assert "Recipes/Baking" in out and "bread and cakes" in out


def test_folders_when_there_are_none(root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "folders", "--root", str(root))
    assert code == 0
    assert "No folders yet" in out


def test_describe_shows_sets_and_clears(shelf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert "no description yet" in run(capsys, "describe", "--root", str(shelf), "Finance")[1]
    assert (
        "Described Finance"
        in run(capsys, "describe", "--root", str(shelf), "Finance", "invoices and taxes")[1]
    )
    assert "invoices and taxes" in run(capsys, "describe", "--root", str(shelf), "finance")[1]
    assert "Cleared" in run(capsys, "describe", "--root", str(shelf), "Finance", "")[1]


def test_describe_unknown_folder_is_an_error(
    shelf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, "describe", "--root", str(shelf), "Taxes", "tax stuff")
    assert code == 1
    assert "never creates folders" in err
    assert not (shelf / "Taxes").exists()


def test_classify_shows_the_decision_without_moving(
    shelf: Path, fake_model: FakeEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    run(capsys, "config", "--threshold", "0.3", "--margin", "0.1")
    run(capsys, "describe", "--root", str(shelf), "Finance", "invoice tax")
    path = make(shelf / "invoice 2025.txt", "invoice total tax due")

    code, out, _ = run(capsys, "classify", "--root", str(shelf), str(path))

    assert code == 0
    assert "Decision:  move to 'Finance' automatically" in out
    assert "closest: invoice 2024.txt" in out
    assert path.exists()


def test_index_reports_counts(
    shelf: Path, fake_model: FakeEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "index", "--root", str(shelf))
    assert code == 0
    assert "Indexed 1 files in 3 folders" in out


def test_inbox_lists_waiting_files_with_suggestions(
    shelf: Path, fake_model: FakeEmbedder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "Nothing is waiting" in run(capsys, "inbox", "--root", str(shelf))[1]
    make(shelf / "_Inbox" / "tax notes.txt", "tax invoice")

    code, out, _ = run(capsys, "inbox", "--root", str(shelf))

    assert code == 0
    assert "tax notes.txt" in out
    assert "suggestions: Finance (" in out


def test_assign_by_name_moves_the_file(
    conn, shelf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make(shelf / "_Inbox" / "bread 食パン.txt", "flour yeast")

    code, out, _ = run(capsys, "assign", "--root", str(shelf), "bread 食パン.txt", "Recipes/Baking")

    assert code == 0
    assert (shelf / "Recipes" / "Baking" / "bread 食パン.txt").exists()
    assert "autoshelf undo" in out
    assert "[user]" in run(capsys, "history")[1]


def test_assign_missing_file_is_an_error(shelf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "assign", "--root", str(shelf), "nope.pdf", "Finance")
    assert code == 1
    assert "no file" in err


def test_history_shows_reason_and_confidence(conn, root: Path) -> None:
    (root / "Finance").mkdir()
    move_file(
        conn, make(root / "a.pdf"), root / "Finance", root=root, reason="auto", confidence=0.61
    )
    assert "[auto 0.61]" in format_history_entry(repository.list_history(conn)[0])
