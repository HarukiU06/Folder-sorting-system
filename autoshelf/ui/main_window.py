"""The main window: Inbox, History, Folders and Settings tabs."""

from pathlib import Path

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QLabel, QMainWindow, QTabWidget

from autoshelf.app_service import AppService
from autoshelf.ui.folders_view import FoldersView
from autoshelf.ui.history_view import HistoryView
from autoshelf.ui.icons import app_icon
from autoshelf.ui.inbox_view import InboxView
from autoshelf.ui.settings_view import SettingsView
from autoshelf.ui.tasks import TaskRunner

MESSAGE_MSECS = 8000


class MainWindow(QMainWindow):
    """The app window. Closing it keeps AutoShelf running in the tray."""

    def __init__(
        self, service: AppService, runner: TaskRunner, *, keep_running_on_close: bool = True
    ) -> None:
        super().__init__()
        self.keep_running_on_close = keep_running_on_close
        self.setWindowTitle("AutoShelf")
        self.setWindowIcon(app_icon())
        self.resize(1000, 660)

        self.inbox = InboxView(service, runner)
        self.history = HistoryView(service, runner)
        self.folders = FoldersView(service, runner)
        self.settings = SettingsView(service, runner)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.inbox, "Inbox")
        self.tabs.addTab(self.history, "History")
        self.tabs.addTab(self.folders, "Folders")
        self.tabs.addTab(self.settings, "Settings")
        self.setCentralWidget(self.tabs)
        self.state = QLabel()
        self.statusBar().addPermanentWidget(self.state)

        self.inbox.moved.connect(self._after_move)
        self.inbox.count_changed.connect(self._show_count)
        self.history.undone.connect(self._after_move)
        self.folders.saved.connect(self._after_description)
        self.tabs.currentChanged.connect(self._tab_changed)

    def show_message(self, text: str) -> None:
        """A transient message in the status bar."""
        self.statusBar().showMessage(text, MESSAGE_MSECS)

    def set_state(self, text: str) -> None:
        """What AutoShelf is doing, shown permanently in the status bar."""
        self.state.setText(text)

    def show_tab(self, widget) -> None:
        """Switch to one of the tabs."""
        self.tabs.setCurrentWidget(widget)

    def show_inbox(self, path: Path | None = None) -> None:
        """Switch to the Inbox, optionally selecting one file."""
        self.tabs.setCurrentWidget(self.inbox)
        self.inbox.select(path)
        self.inbox.refresh()

    def refresh_all(self) -> None:
        """Reload every screen."""
        self.inbox.refresh()
        self.history.refresh()
        self.folders.refresh()
        self.settings.load()

    def _after_move(self, message: str) -> None:
        self.show_message(message)
        self.inbox.refresh()
        self.history.refresh()
        self.folders.refresh()

    def _after_description(self, message: str) -> None:
        self.show_message(message)
        self.inbox.refresh()  # suggestions depend on descriptions

    def _show_count(self, count: int) -> None:
        self.tabs.setTabText(
            self.tabs.indexOf(self.inbox), f"Inbox ({count})" if count else "Inbox"
        )

    def _tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.history:
            self.history.refresh()
        elif widget is self.folders:
            self.folders.refresh()
        elif widget is self.settings:
            self.settings.load()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.keep_running_on_close:
            event.ignore()
            self.hide()
        else:
            event.accept()
