"""Smoke tests of the desktop app's screens (drawn off screen, with the fake model)."""

import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make
from PySide6.QtWidgets import QFileDialog, QMessageBox

from autoshelf.app_service import AppService
from autoshelf.config import CONFIDENCE_LEVELS, AppPaths
from autoshelf.pipeline import Outcome, OutcomeKind
from autoshelf.ui import app as app_module
from autoshelf.ui.app import AutoShelfApp, notification_text
from autoshelf.ui.folders_view import DESCRIPTION_COLUMN, FoldersView
from autoshelf.ui.history_view import HistoryView
from autoshelf.ui.icons import app_icon
from autoshelf.ui.inbox_view import InboxView
from autoshelf.ui.main_window import MainWindow
from autoshelf.ui.settings_view import SettingsView
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.tray import Tray

WAIT_MS = 10_000


@pytest.fixture
def shelf(root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank")
    (root / "Recipes").mkdir()
    return root


@pytest.fixture
def service(paths: AppPaths, shelf: Path) -> Iterator[AppService]:
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.save_settings(replace(app.settings(), root=str(shelf), stable_seconds=0.3))
    app.describe("Finance", "invoice receipt tax bank")
    app.describe("Recipes", "recipe cooking baking oven")
    yield app
    app.release()


@pytest.fixture
def runner(qtbot) -> Iterator[TaskRunner]:
    tasks = TaskRunner()
    yield tasks
    tasks.wait()


def settle(qtbot, runner: TaskRunner) -> None:
    """Wait until every background task has delivered its result."""
    qtbot.waitUntil(lambda: not runner.busy, timeout=WAIT_MS)


@pytest.fixture(autouse=True)
def no_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Modal dialogs would block forever; record them instead."""
    shown: list[tuple] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: shown.append(args))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: shown.append(args))
    return shown


# --- background tasks -----------------------------------------------------------------


def test_task_results_and_errors_arrive_on_the_gui_thread(qtbot, runner: TaskRunner) -> None:
    results, errors = [], []
    runner.run(lambda: 41 + 1, results.append)
    runner.run(lambda: 1 / 0, None, errors.append)
    settle(qtbot, runner)
    assert results == [42]
    assert isinstance(errors[0], ZeroDivisionError)


# --- Inbox --------------------------------------------------------------------------------


def test_inbox_shows_files_and_suggestions(qtbot, service, runner, shelf: Path) -> None:
    make(shelf / "_Inbox" / "tax invoice.txt", "invoice tax")
    make(shelf / "_Inbox" / "cake.txt", "recipe baking oven")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    counts: list[int] = []
    view.count_changed.connect(counts.append)

    view.refresh()
    settle(qtbot, runner)

    assert view.list.count() == 2
    assert counts[-1] == 2
    assert view.summary.text() == "2 files waiting for you"
    view.select(shelf / "_Inbox" / "tax invoice.txt")
    assert view.name.text() == "tax invoice.txt"
    assert view.suggestion_buttons[0].text().startswith("Finance")
    assert "Why it's here" in view.why.text()
    assert view.preview.toPlainText() == "invoice tax"
    assert [view.other.itemText(i) for i in range(view.other.count())] == ["Finance", "Recipes"]


def test_choosing_a_suggestion_moves_the_file(qtbot, service, runner, shelf: Path) -> None:
    src = make(shelf / "_Inbox" / "tax invoice.txt", "invoice tax")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)

    with qtbot.waitSignal(view.moved, timeout=WAIT_MS) as moved:
        view.suggestion_buttons[0].click()
    settle(qtbot, runner)

    assert moved.args == ["Moved tax invoice.txt to Finance"]
    assert (shelf / "Finance" / "tax invoice.txt").exists()
    assert not src.exists()
    assert view.list.count() == 0
    assert view.empty.isVisibleTo(view)


def test_other_folder_moves_the_file(qtbot, service, runner, shelf: Path) -> None:
    make(shelf / "_Inbox" / "notes.txt", "call mom")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)

    view.other.setCurrentText("Recipes")
    view.move_other.click()
    settle(qtbot, runner)

    assert (shelf / "Recipes" / "notes.txt").exists()


def test_inbox_without_a_root_explains(qtbot, paths: AppPaths, runner) -> None:
    view = InboxView(AppService(paths), runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    assert "Choose the folder" in view.summary.text()


# --- History ------------------------------------------------------------------------------


def test_history_lists_moves_and_undoes_one(qtbot, service, runner, shelf: Path) -> None:
    src = make(shelf / "_Inbox" / "bread.txt", "flour")
    service.assign(src, "Recipes")
    view = HistoryView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)

    assert view.table.rowCount() == 1
    cells = [view.table.item(0, column).text() for column in range(view.table.columnCount())]
    assert cells[1:5] == ["bread.txt", "_Inbox", "Recipes", "You chose"]
    view.table.selectRow(0)
    assert view.undo_button.isEnabled()

    view.undo_button.click()
    settle(qtbot, runner)

    assert src.exists()
    assert view.table.rowCount() == 2
    assert view.table.item(0, 4).text().startswith("Undo of #")


def test_undo_of_a_changed_file_asks_first(
    qtbot, service, runner, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make(shelf / "_Inbox" / "bread.txt", "flour")
    moved = service.assign(src, "Recipes")
    moved.dst.write_text("flour and water", encoding="utf-8")
    questions = []

    def answer_yes(*args):
        questions.append(args)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", answer_yes)
    view = HistoryView(service, runner)
    qtbot.addWidget(view)
    view.undo_last()
    settle(qtbot, runner)

    assert "has changed since it was moved" in questions[0][2]
    assert "--force" not in questions[0][2]
    assert src.read_text(encoding="utf-8") == "flour and water"


# --- Folders --------------------------------------------------------------------------------


def test_folders_show_counts_and_save_descriptions(qtbot, service, runner) -> None:
    view = FoldersView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)

    assert view.table.rowCount() == 2
    assert view.table.item(0, 0).text() == "Finance"
    assert view.table.item(0, 1).text() == "1"
    with qtbot.waitSignal(view.saved, timeout=WAIT_MS):
        view.table.item(1, DESCRIPTION_COLUMN).setText("cakes and bread")
    assert {f.key: f.description for f in service.folders()}["Recipes"] == "cakes and bread"


# --- Settings -------------------------------------------------------------------------------


def test_settings_show_the_balanced_preset(qtbot, service, runner) -> None:
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    assert view.slider.value() == 2
    assert view.level_text.text().startswith("Balanced")
    assert view.root.text() == service.settings().root


def test_custom_values_are_kept_until_the_slider_moves(qtbot, service, runner) -> None:
    service.save_settings(replace(service.settings(), auto_threshold=0.42, auto_margin=0.11))
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    assert view.level_text.text().startswith("Custom: threshold 0.42")
    assert service.settings().auto_threshold == 0.42  # showing it changed nothing

    with qtbot.waitSignal(view.restart_needed, timeout=WAIT_MS):
        view.slider.setValue(1)
    settings = service.settings()
    assert (settings.auto_threshold, settings.auto_margin) == (
        CONFIDENCE_LEVELS[1].threshold,
        CONFIDENCE_LEVELS[1].margin,
    )


def test_settings_save_ignore_patterns(qtbot, service, runner) -> None:
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    view.patterns.setPlainText("*.tmp\n\n*.bak\n")
    with qtbot.waitSignal(view.restart_needed, timeout=WAIT_MS):
        view._apply_files()
    assert service.settings().ignore_patterns == ["*.tmp", "*.bak"]


# --- the app ---------------------------------------------------------------------------------


@pytest.fixture
def app(qtbot, service: AppService) -> Iterator[AutoShelfApp]:
    assert service.acquire()
    shelf_app = AutoShelfApp(service)
    qtbot.addWidget(shelf_app.window)
    yield shelf_app
    shelf_app.shutdown()


def test_app_sorts_and_reports_files_needing_a_decision(qtbot, app, shelf: Path) -> None:
    app._notifications = True
    app.start(show=False)
    assert app.service.watching

    make(shelf / "notes.txt", "call mom buy milk")

    status = app.window.statusBar()
    qtbot.waitUntil(
        lambda: "Where should notes.txt go?" in status.currentMessage(), timeout=WAIT_MS
    )
    qtbot.waitUntil(lambda: app.window.inbox.list.count() == 1, timeout=WAIT_MS)
    assert app.window.tabs.tabText(0) == "Inbox (1)"


def test_pausing_from_the_tray_updates_settings(qtbot, app) -> None:
    app.start(show=False)
    app.tray.pause_action.trigger()
    settle(qtbot, app.runner)
    assert app.service.settings().paused
    assert app.window.settings.paused.isChecked()
    assert app.window.state.text() == "Paused"


def test_first_run_asks_for_a_folder(
    qtbot, paths: AppPaths, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(shelf / "loose.pdf")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(shelf))
    monkeypatch.setattr(AutoShelfApp, "ask_about_loose_files", lambda self, root, n: "paused")
    service = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    assert service.acquire()
    shelf_app = AutoShelfApp(service)
    qtbot.addWidget(shelf_app.window)

    shelf_app.start(show=False)
    qtbot.waitUntil(lambda: service.watching, timeout=WAIT_MS)
    settle(qtbot, shelf_app.runner)

    assert service.root() == shelf
    assert service.settings().paused  # the user chose to look first
    assert (shelf / "loose.pdf").exists()
    shelf_app.shutdown()


def test_unusable_folder_is_refused(
    qtbot, app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_dialogs
) -> None:
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(Path.home()))
    app.choose_root()
    assert "home folder" in no_dialogs[0][2]


def test_notification_text() -> None:
    one = Outcome(OutcomeKind.MOVED, Path("scan.pdf"), "", dst=Path("_Inbox/scan.pdf"))
    assert notification_text([one]) == ("Where should scan.pdf go?", "Click to choose a folder.")
    many = [Outcome(OutcomeKind.WAITING, Path(f"{n}.pdf"), "") for n in "abcd"]
    title, message = notification_text(many)
    assert title == "4 files need your decision"
    assert message == "a.pdf, b.pdf, c.pdf and 1 more. Click to open your Inbox."


def test_closing_the_window_keeps_the_app_running(qtbot, service, runner) -> None:
    window = MainWindow(service, runner, keep_running_on_close=True)
    qtbot.addWidget(window)
    window.show()
    window.close()
    assert not window.isVisible()


def test_tray_tooltip_and_icon(qtbot) -> None:
    tray = Tray()
    tray.set_waiting(2)
    tray.set_paused(True)
    tray.set_status("Ready")
    assert tray.tooltip() == "AutoShelf (paused)\n2 files in the Inbox\nReady"
    assert tray.inbox_action.text() == "Inbox (2)"
    assert not app_icon().isNull()


def test_server_name_is_per_data_folder(paths: AppPaths, tmp_path: Path) -> None:
    other = AppPaths(tmp_path / "elsewhere")
    assert app_module.server_name(paths) != app_module.server_name(other)
    assert app_module.server_name(paths) == app_module.server_name(AppPaths(paths.home))


def test_later_launches_and_tools_can_show_or_quit_the_app(
    qtbot, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from PySide6.QtNetwork import QLocalServer

    shown, quits = [], []

    class FakeApp:
        def show_window(self) -> None:
            shown.append(True)

    monkeypatch.setattr(app_module.QApplication, "quit", lambda: quits.append(True))
    name = app_module.server_name(AppPaths(tmp_path))
    server = QLocalServer()
    QLocalServer.removeServer(name)
    assert server.listen(name)
    app_module._serve_commands(server, FakeApp())

    def send(command: bytes) -> bool:
        # A later launch is another process; send_command blocks until acknowledged.
        code = (
            "import sys; from autoshelf.ui.app import send_command; "
            f"sys.exit(0 if send_command({name!r}, {command!r}) else 1)"
        )
        sender = subprocess.Popen([sys.executable, "-c", code])
        qtbot.waitUntil(lambda: sender.poll() is not None, timeout=WAIT_MS * 3)
        return sender.returncode == 0

    assert send(app_module.SHOW_COMMAND)
    assert shown == [True]
    assert send(app_module.QUIT_COMMAND)
    qtbot.waitUntil(lambda: quits == [True], timeout=WAIT_MS)
    server.close()
    assert not send(app_module.SHOW_COMMAND)
