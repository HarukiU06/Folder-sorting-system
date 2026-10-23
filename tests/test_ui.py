"""Smoke tests of the desktop app's screens (drawn off screen, with the fake model)."""

import html
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make
from PySide6.QtCore import Qt
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
from autoshelf.ui.widgets import plain_tooltip

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
    """Modal dialogs would block forever; record them instead (questions get "No")."""
    shown: list[tuple] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: shown.append(args))
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: shown.append(args))

    def no(*args):
        shown.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", no)
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


def test_inbox_shows_text_from_files_literally(qtbot, service, runner, shelf: Path) -> None:
    # Shown as HTML, the title would make Qt open \\attacker\share (leaking the sign-in hash).
    title = '<img src="file://attacker/share/x.png">Agenda'
    make(shelf / "_Inbox" / "agenda.html", f"<title>{html.escape(title)}</title><p>agenda</p>")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    assert view.title.text() == f"Title: {title}"
    for label in (view.name, view.title, view.why, view.rename_label):
        assert label.textFormat() == Qt.TextFormat.PlainText


def test_plain_tooltip_never_renders_html() -> None:
    assert plain_tooltip("invoice total") == "invoice total"
    shown = plain_tooltip('<img src="file://attacker/share/x.png"> notes')
    assert "<img" not in shown
    assert "&lt;img" in shown


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


# --- Phase 5: "Why here?", corrections and rules -------------------------------------------


def test_history_shows_why_and_corrects_a_move(
    qtbot, service, runner, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make(shelf / "_Inbox" / "bread.txt", "flour")
    service.assign(src, "Finance")  # the wrong folder
    view = HistoryView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    view.table.selectRow(0)
    assert "You chose" in view.why.toPlainText()
    assert view.correct_button.isEnabled()

    monkeypatch.setattr(
        "autoshelf.ui.history_view.QInputDialog.getItem", lambda *args: ("Recipes", True)
    )
    with qtbot.waitSignal(view.undone, timeout=WAIT_MS) as moved:
        view.correct_selected()
        settle(qtbot, runner)
    assert moved.args == ["Moved bread.txt to Recipes; AutoShelf will remember"]
    assert (shelf / "Recipes" / "bread.txt").exists()
    settle(qtbot, runner)
    assert view.table.item(0, 4).text() == "You corrected #1"
    assert service.corrections()[-1]["source"] == "undo"


def test_inbox_explains_its_suggestions(qtbot, service, runner, shelf: Path) -> None:
    make(shelf / "_Inbox" / "tax invoice.txt", "invoice tax bank")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    text = view.why_panel.toPlainText()
    assert "Best folders" in text
    assert "Words in common with Finance" in text
    assert "invoice 2024.txt" in text  # the most similar file


def test_rules_view_adds_toggles_tests_and_deletes(
    qtbot, service, runner, shelf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ui.rules_view import RuleDialog, RulesView

    view = RulesView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    assert "No rules yet" in view.status.text()

    monkeypatch.setattr(RuleDialog, "exec", lambda self: RuleDialog.DialogCode.Accepted)
    values = {
        "name": "Invoices",
        "conditions": [{"kind": "name", "pattern": "invoice"}],
        "match_any": False,
        "target": "Finance",
        "tags": ["Taxes"],
        "rename_template": "",
        "enabled": True,
    }
    monkeypatch.setattr(RuleDialog, "values", lambda self: dict(values))
    with qtbot.waitSignal(view.changed, timeout=WAIT_MS):
        view.add_rule()
        settle(qtbot, runner)
    settle(qtbot, runner)
    assert view.table.rowCount() == 1
    assert view.table.item(0, 2).text() == "file name matches /invoice/"
    assert view.table.item(0, 3).text() == "move to Finance; tag Taxes"

    view.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    settle(qtbot, runner)
    assert not service.rules()[0].enabled
    view.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    settle(qtbot, runner)

    sample = make(tmp_path / "invoice 9.txt", "x")
    monkeypatch.setattr(
        "autoshelf.ui.rules_view.QFileDialog.getOpenFileName", lambda *args: (str(sample), "")
    )
    view.test_file()
    settle(qtbot, runner)
    assert "Rule 'Invoices' matches" in view.status.text()

    view.table.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    view.delete_selected()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert view.table.rowCount() == 0
    assert service.rules() == []


def test_rule_dialog_takes_several_conditions_and_actions(qtbot) -> None:
    from autoshelf.ui.rules_view import DONT_MOVE, RuleDialog

    dialog = RuleDialog(["Finance", "Recipes"])
    qtbot.addWidget(dialog)
    dialog.name.setText("Big scans")
    first = dialog.rows[0]
    first.kind.setCurrentIndex(first.kind.findData("extension"))
    first.first.setText(".pdf")
    assert first.second.isHidden()
    second = dialog.add_condition(None)
    second.kind.setCurrentIndex(second.kind.findData("size"))
    assert not second.second.isHidden()
    second.first.setText("1MB")
    third = dialog.add_condition({"kind": "date", "from": "2025-01-01", "to": None})
    dialog._remove_condition(third)
    dialog.match.setCurrentIndex(1)
    assert dialog.target.itemText(0) == DONT_MOVE
    dialog.tags.setText("Scans, 2025 ")
    dialog.rename.setText("{date} {title}")

    assert dialog.values() == {
        "name": "Big scans",
        "conditions": [
            {"kind": "extension", "pattern": ".pdf"},
            {"kind": "size", "min": "1MB", "max": None},
        ],
        "match_any": True,
        "target": "",
        "tags": ["Scans", "2025"],
        "rename_template": "{date} {title}",
        "enabled": True,
    }


def test_rule_dialog_shows_a_saved_rule(qtbot) -> None:
    from autoshelf.db.repository import RuleRecord
    from autoshelf.ui.rules_view import RuleDialog

    rule = RuleRecord(
        1,
        "Old big files",
        1,
        True,
        (
            {"kind": "size", "min": 1536, "max": None},
            {"kind": "date", "from": None, "to": "2020-12-31"},
        ),
        "Recipes",
        tags=("Old",),
        rename_template="{year} {name}",
    )
    dialog = RuleDialog(["Finance", "Recipes"], rule)
    qtbot.addWidget(dialog)
    assert dialog.rows[0].first.text() == "1.5KB"
    assert dialog.rows[1].second.text() == "2020-12-31"
    assert dialog.target.currentText() == "Recipes"
    values = dialog.values()
    assert (values["tags"], values["rename_template"]) == (["Old"], "{year} {name}")


# --- Phase 6: Library, Tidy up, Dashboard, watched folders -----------------------------------


def test_library_searches_and_tags(qtbot, service, runner, shelf: Path) -> None:
    from autoshelf.ui.library_view import LibraryView

    service.folders()
    service.inbox()  # indexes the library
    view = LibraryView(service, runner)
    qtbot.addWidget(view)
    view.query.setText("invoice tax")
    view.search()
    settle(qtbot, runner)
    settle(qtbot, runner)

    assert view.results.item(0, 0).text() == "invoice 2024.txt"
    assert view.name.text() == "invoice 2024.txt"
    assert "In Finance" in view.facts.text()

    view.new_tag.setText("Taxes 2024")
    view._add_typed_tag()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert [view.file_tags.item(i).text() for i in range(view.file_tags.count())] == ["Taxes 2024"]
    assert view.tag_list.item(0).text() == "Taxes 2024  (1)"

    view.results.setRowCount(0)
    view._tag_clicked(view.tag_list.item(0))
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert view.results.rowCount() == 1
    view.file_tags.setCurrentRow(0)
    view._remove_tag()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert service.tags() == []


def test_tidy_organizes_and_undoes_a_batch(qtbot, service, runner, shelf: Path) -> None:
    from autoshelf.ui.tidy_view import ROOT_CHOICE, TidyView

    service.save_settings(replace(service.settings(), auto_threshold=0.3, auto_margin=0.1))
    make(shelf / "tax invoice.txt", "invoice tax bank receipt")
    view = TidyView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    page = view.organize
    page.source.setCurrentText(ROOT_CHOICE)
    page.preview()
    settle(qtbot, runner)
    assert page.table.rowCount() == 1
    assert page.table.cellWidget(0, 3).currentText() == "Finance"
    assert (shelf / "tax invoice.txt").exists()

    page.apply()
    settle(qtbot, runner)
    assert (shelf / "Finance" / "tax invoice.txt").exists()
    assert page.undo_button.isEnabled()
    page.undo_batch()
    settle(qtbot, runner)
    assert (shelf / "tax invoice.txt").exists()


def test_tidy_sets_duplicates_aside(
    qtbot, service, runner, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ui.tidy_view import TidyView

    copy = make(shelf / "Recipes" / "copy.txt", "invoice total tax due bank")
    view = TidyView(service, runner)
    qtbot.addWidget(view)
    page = view.duplicates
    page.refresh()
    settle(qtbot, runner)
    group = page.tree.topLevelItem(0)
    assert group.childCount() == 2
    keep = next(group.child(i) for i in range(2) if "invoice 2024" in group.child(i).text(0))
    page.tree.setCurrentItem(keep)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    page.keep_selected()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert not copy.exists()
    assert (shelf / "_Duplicates" / "copy.txt").exists()
    assert page.tree.topLevelItemCount() == 0


def test_dashboard_shows_numbers(qtbot, service, runner, shelf: Path) -> None:
    from autoshelf.ui.dashboard_view import DashboardView

    make(shelf / "_Inbox" / "waiting.txt", "x")
    view = DashboardView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    assert view.values["files"].text() == "1"
    assert view.values["inbox"].text() == "1"
    assert view.per_folder.items == [("Finance", 1), ("Recipes", 0)]
    view.resize(800, 600)
    view.grab()  # paints the charts


def test_settings_add_and_remove_a_watched_folder(
    qtbot, service, runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    monkeypatch.setattr(
        "autoshelf.ui.settings_view.QFileDialog.getExistingDirectory", lambda *a: str(downloads)
    )
    with qtbot.waitSignal(view.restart_needed, timeout=WAIT_MS):
        view.add_watched()
        settle(qtbot, runner)
    settle(qtbot, runner)
    assert view.watched.count() == 1
    view.watched.setCurrentRow(0)
    with qtbot.waitSignal(view.restart_needed, timeout=WAIT_MS):
        view.remove_watched()
        settle(qtbot, runner)
    assert service.settings().watched_folders == []
    view.extensions.setText("pdf, .MD")
    with qtbot.waitSignal(view.restart_needed, timeout=WAIT_MS):
        view._apply_extensions()
    assert service.settings().watched_extensions == [".md", ".pdf"]


def test_main_window_has_every_screen(qtbot, service, runner) -> None:
    window = MainWindow(service, runner, keep_running_on_close=False)
    qtbot.addWidget(window)
    names = [window.tabs.tabText(i) for i in range(window.tabs.count())]
    assert names == [
        "Inbox",
        "Library",
        "History",
        "Folders",
        "Rules",
        "Tidy up",
        "Dashboard",
        "Ask",
        "Settings",
    ]
    assert not window.tabs.isTabVisible(window.tabs.indexOf(window.ask))  # AI is off
    window.set_ai_enabled(True)
    assert window.tabs.isTabVisible(window.tabs.indexOf(window.ask))
    for index in range(window.tabs.count()):
        window.tabs.setCurrentIndex(index)
        settle(qtbot, runner)


# --- Phase 7: the optional AI -----------------------------------------------------------------


@pytest.fixture
def fake_ai(service, monkeypatch: pytest.MonkeyPatch):
    from fakes import FakeProvider

    provider = FakeProvider("A short summary.")
    monkeypatch.setattr(service, "provider", lambda bulk=False: provider)
    return provider


def test_settings_turn_the_ai_on_and_keep_the_key_safe(
    qtbot, service, runner, memory_keyring: dict
) -> None:
    from autoshelf.llm import secrets

    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    states: list[bool] = []
    view.ai_state.connect(states.append)
    view.load()
    settle(qtbot, runner)
    assert states[-1] is False
    assert view.key_status.text() == "Key: not saved"

    view.ai_provider.setCurrentIndex(view.ai_provider.findData("claude"))
    assert view.ai_model.currentText() == "claude-opus-5-5"
    view.ai_model.setCurrentText("claude-haiku-5-5")
    view._apply_ai()
    settle(qtbot, runner)
    assert states[-1] is True
    settings = service.settings()
    assert (settings.llm_provider, settings.llm_model) == ("claude", "claude-haiku-5-5")

    view.api_key.setText("sk-ant-secret-9876")
    view._save_key()
    settle(qtbot, runner)
    assert view.api_key.text() == ""
    assert "saved (...9876)" in view.key_status.text()
    assert memory_keyring[(secrets.SERVICE, secrets.USERNAME)] == "sk-ant-secret-9876"
    assert "sk-ant-secret" not in str(service.settings())  # never in the settings
    view._forget_key()
    settle(qtbot, runner)
    assert view.key_status.text() == "Key: not saved"


def test_ask_view_shows_answers_with_sources(qtbot, service, runner, fake_ai) -> None:
    from autoshelf.ui.ask_view import AskView

    service.inbox()  # indexes the library
    fake_ai.replies = ["Invoices go to Finance [1]."]
    view = AskView(service, runner)
    qtbot.addWidget(view)
    view.question.setText("Where are my invoices?")
    view.ask()
    settle(qtbot, runner)
    text = view.transcript.toPlainText()
    assert "Invoices go to Finance [1]." in text
    assert "invoice 2024.txt" in text
    assert view.history[0].cited == (1,)


def test_library_summarizes_and_renames(
    qtbot, service, runner, shelf: Path, fake_ai, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ui.library_view import LibraryView

    service.inbox()
    view = LibraryView(service, runner)
    qtbot.addWidget(view)
    view.set_ai_enabled(True)
    view.query.setText("invoice")
    view.search()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert view.summarize_button.isVisibleTo(view)
    view.summarize()
    settle(qtbot, runner)
    assert view.summary_text.text() == "Summary: A short summary."

    fake_ai.replies = ["Hosting Invoice 2024"]
    monkeypatch.setattr(
        "autoshelf.ui.library_view.QInputDialog.getText",
        lambda *args, text="", **kwargs: (text, True),
    )
    view.rename_file()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert (shelf / "Finance" / "Hosting Invoice 2024.txt").exists()


def test_inbox_offers_a_better_name(qtbot, service, runner, shelf: Path) -> None:
    make(shelf / "_Inbox" / "scan_0034.txt", "Quarterly Report\nrevenue grew")
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    assert view.rename_row.isVisibleTo(view)
    assert view.rename_label.text().endswith("Quarterly Report.txt")
    view.rename_button.click()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert (shelf / "_Inbox" / "Quarterly Report.txt").exists()


def test_tidy_renames_and_suggests_folders(
    qtbot, service, runner, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ui.tidy_view import TidyView

    make(shelf / "_Inbox" / "scan_0001.txt", "Meeting Minutes\nwe met")
    for number in range(3):
        make(shelf / "_Inbox" / f"pods {number}.txt", f"kubernetes pods deployment yaml {number}")
    service.inbox()
    view = TidyView(service, runner)
    qtbot.addWidget(view)

    view.renames.refresh()
    settle(qtbot, runner)
    assert view.renames.table.item(0, 1).text() == "Meeting Minutes.txt"
    view.renames.table.selectRow(0)
    view.renames.rename_selected()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert (shelf / "_Inbox" / "Meeting Minutes.txt").exists()

    view.new_folders.refresh()
    settle(qtbot, runner)
    assert view.new_folders.tree.topLevelItemCount() == 1
    view.new_folders.tree.setCurrentItem(view.new_folders.tree.topLevelItem(0))
    monkeypatch.setattr(
        "autoshelf.ui.tidy_view.QInputDialog.getText", lambda *args, **kwargs: ("Kubernetes", True)
    )
    view.new_folders.create_selected()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert len(list((shelf / "Kubernetes").iterdir())) == 3


def test_study_dialog_makes_and_reviews_cards(
    qtbot, service, runner, shelf: Path, fake_ai, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ui.study_dialog import StudyDialog

    service.inbox()
    fake_ai.replies = ['[{"q": "What is due?", "a": "Tax"}, {"q": "Bank?", "a": "Transfer"}]']
    dialog = StudyDialog(service, runner, "Finance")
    qtbot.addWidget(dialog)
    settle(qtbot, runner)
    dialog.make_cards()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert dialog.card_text.text() == "Q: What is due?"
    dialog.flip()
    assert dialog.card_text.text() == "A: Tax"
    dialog.next_card()
    assert dialog.counter.text() == "Card 2 of 2"

    target = tmp_path / "finance cards.csv"
    monkeypatch.setattr(
        "autoshelf.ui.study_dialog.QFileDialog.getSaveFileName", lambda *a: (str(target), "")
    )
    dialog.export()
    assert target.read_text(encoding="utf-8").splitlines()[0] == "What is due?,Tax"


# --- Phase 9: missed files, skipped files, the bulk AI model ------------------------------------

_unfinished_reads: list = []  # kept referenced: a collected context manager would clean up


def crash_while_reading(paths: AppPaths, path: Path) -> None:
    """What the sorting process leaves behind when it dies while reading ``path``."""
    from autoshelf.extract import guard

    reading = guard.ReadGuard(paths.home / guard.STATE_FILENAME, record=True).reading(path)
    reading.__enter__()
    _unfinished_reads.append(reading)


def test_settings_show_the_sweep_and_the_bulk_model(qtbot, service, runner) -> None:
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    assert view.sweep.value() == 5
    view.sweep.setValue(0)
    assert view.sweep.text() == "never"
    view._apply_files()
    settle(qtbot, runner)
    assert service.settings().sweep_minutes == 0

    view.ai_provider.setCurrentIndex(view.ai_provider.findData("claude"))
    assert view.ai_model.currentText() == "claude-opus-5-5"
    assert view.ai_bulk_model.currentText() == "claude-haiku-5-5"
    view._apply_ai()
    settle(qtbot, runner)
    assert service.settings().llm_bulk_model == ""  # follows the default
    view.ai_bulk_model.setCurrentText("claude-sonnet-5-5")
    view._apply_ai()
    settle(qtbot, runner)
    assert service.settings().llm_bulk_model == "claude-sonnet-5-5"


def test_settings_list_skipped_files_and_read_them_again(
    qtbot, service, runner, paths: AppPaths, shelf: Path
) -> None:
    from autoshelf.extract import guard

    doc = make(shelf / "_Inbox" / "agenda.txt", "agenda")
    crash_while_reading(paths, doc)
    guard.start(paths.home, record=True)
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    messages: list[str] = []
    view.message.connect(messages.append)
    view.load()
    settle(qtbot, runner)
    assert not view.skipped_box.isHidden()
    assert [view.skipped.item(i).text() for i in range(view.skipped.count())] == ["agenda.txt"]

    view.skipped.item(0).setSelected(True)
    view.clear_selected_skipped()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert messages == ["AutoShelf will read 1 file(s) again"]
    assert view.skipped.count() == 0
    assert view.skipped_box.isHidden()
    assert not guard.crashed_before(doc)


def test_the_app_says_which_files_stopped_its_last_run(
    qtbot, service, paths: AppPaths, shelf: Path
) -> None:
    doc = make(shelf / "broken.pdf", "%PDF")
    crash_while_reading(paths, doc)
    assert service.acquire()  # starts the guard: broken.pdf is skipped from now on
    app = AutoShelfApp(service, tray=Tray())
    qtbot.addWidget(app.window)
    app.start(show=False)
    assert "AutoShelf stopped while reading broken.pdf" in app.window.statusBar().currentMessage()
    app.runner.wait()


# --- Phase 10: how well would AutoShelf sort my library? -------------------------------------


@pytest.fixture
def full_shelf(shelf: Path) -> Path:
    for number in range(5):
        make(shelf / "Finance" / f"invoice {number}.txt", f"invoice total tax due bank {number}")
        make(shelf / "Recipes" / f"cake {number}.txt", f"recipe baking oven flour sugar {number}")
    return shelf


def test_settings_test_sorting_and_use_the_recommendation(
    qtbot, service, runner, full_shelf: Path
) -> None:
    service.set_level(0)  # very cautious
    view = SettingsView(service, runner)
    qtbot.addWidget(view)
    view.load()
    settle(qtbot, runner)
    assert view.calibration_text.isHidden()

    view.calibrate()
    settle(qtbot, runner)
    text = view.calibration_text.text()
    assert "Each of the 11 files in your 2 folders" in text
    assert "At Balanced:" in text and "Recommended:" in text
    assert not view.use_recommended.isHidden()

    view._use_recommended()
    settle(qtbot, runner)
    assert view.use_recommended.isHidden()
    assert service.settings().auto_threshold != CONFIDENCE_LEVELS[0].threshold


def test_the_app_offers_the_test_once(
    qtbot, service, full_shelf: Path, no_dialogs: list, monkeypatch
) -> None:
    assert service.acquire()
    app = AutoShelfApp(service, tray=Tray())
    qtbot.addWidget(app.window)
    app.start(show=True)
    qtbot.waitUntil(lambda: service.settings().calibration_offered, timeout=WAIT_MS)
    asked = [args for args in no_dialogs if len(args) > 1 and "sort your files" in str(args[1])]
    assert len(asked) == 1  # answered "No" by the test

    app.show_window()  # opened again: not asked again
    app.runner.wait()
    asked = [args for args in no_dialogs if len(args) > 1 and "sort your files" in str(args[1])]
    assert len(asked) == 1
    app.runner.wait()


def test_a_small_library_is_not_offered_the_test(qtbot, service, no_dialogs: list) -> None:
    assert service.acquire()
    app = AutoShelfApp(service, tray=Tray())
    qtbot.addWidget(app.window)
    app.start(show=True)
    app.runner.wait()
    qtbot.wait(100)
    assert not service.settings().calibration_offered
    assert not any("sort your files" in str(args[1]) for args in no_dialogs if len(args) > 1)


# --- Phase 11: files AutoShelf made ---------------------------------------------------------------


def test_history_offers_undo_for_a_file_autoshelf_made(
    qtbot, service, runner, shelf: Path, tmp_path: Path
) -> None:
    from autoshelf.actions.create import place_new_file
    from autoshelf.db.connection import open_database
    from autoshelf.ui.history_view import can_undo

    source = make(shelf / "Finance" / "report.txt", "report")
    made = make(tmp_path / "work" / "report.zip", "zip")
    conn = open_database(service.paths.db_path)
    try:
        created = place_new_file(
            conn, made, source.parent, "report.zip", root=shelf, operation="zip", sources=[source]
        )
    finally:
        conn.close()
    view = HistoryView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    row = [entry.id for entry in view._entries].index(created.history_id)
    assert view.table.item(row, 4).text() == "Made a ZIP of report.txt"
    assert can_undo(view._entries[row])
    view.table.selectRow(row)
    view.undo_selected()
    settle(qtbot, runner)
    settle(qtbot, runner)
    assert (shelf / "_Trash" / "report.zip").exists()
    assert source.exists()


# --- Phase 12: rules suggested after an Inbox answer -----------------------------------------


def test_an_inbox_answer_can_become_a_rule(
    qtbot, service, runner, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.db import repository
    from autoshelf.db.connection import open_database

    conn = open_database(service.paths.db_path)
    try:
        for number in range(2):
            path = make(shelf / "Finance" / f"receipt {number}.txt", "x")
            st = path.stat()
            file_id = repository.upsert_file(
                conn, path, sha256=None, size=st.st_size, mtime=st.st_mtime
            )
            repository.record_correction(
                conn, file_id=file_id, from_folder=None, to_folder="Finance", source="inbox"
            )
    finally:
        conn.close()
    make(shelf / "_Inbox" / "receipt 3.txt", "anything at all")
    asked: list[str] = []

    def yes(parent, title, text, *rest):
        asked.append(text)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", yes)
    view = InboxView(service, runner)
    qtbot.addWidget(view)
    view.refresh()
    settle(qtbot, runner)
    view.move_to("Finance")
    for _ in range(4):
        settle(qtbot, runner)
    assert asked and "into Finance. Make this a rule?" in asked[0]
    assert [rule.target_folder for rule in service.rules()] == ["Finance"]
