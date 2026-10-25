"""The main window: a sidebar on the left, one page per screen."""

from pathlib import Path

from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QStackedWidget, QWidget
from qfluentwidgets import FluentIcon, NavigationInterface, NavigationItemPosition

from autoshelf.app_service import AppService
from autoshelf.ui.ask_view import AskView
from autoshelf.ui.dashboard_view import DashboardView
from autoshelf.ui.folders_view import FoldersView
from autoshelf.ui.history_view import HistoryView
from autoshelf.ui.icons import app_icon
from autoshelf.ui.inbox_view import InboxView
from autoshelf.ui.library_view import LibraryView
from autoshelf.ui.rules_view import RulesView
from autoshelf.ui.settings_view import SettingsView
from autoshelf.ui.style import apply_theme
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.tidy_view import TidyView

MESSAGE_MSECS = 8000
SIDEBAR_WIDTH = 220


class MainWindow(QMainWindow):
    """The app window. Closing it keeps AutoShelf running in the tray."""

    def __init__(
        self, service: AppService, runner: TaskRunner, *, keep_running_on_close: bool = True
    ) -> None:
        super().__init__()
        apply_theme()
        self.keep_running_on_close = keep_running_on_close
        self.setWindowTitle("AutoShelf")
        self.setWindowIcon(app_icon())
        self.resize(1100, 720)

        self.inbox = InboxView(service, runner)
        self.library = LibraryView(service, runner)
        self.history = HistoryView(service, runner)
        self.folders = FoldersView(service, runner)
        self.rules = RulesView(service, runner)
        self.tidy = TidyView(service, runner)
        self.dashboard = DashboardView(service, runner)
        self.ask = AskView(service, runner)
        self.settings = SettingsView(service, runner)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.navigation = NavigationInterface(
            central, showMenuButton=False, showReturnButton=False, collapsible=False
        )
        self.navigation.setExpandWidth(SIDEBAR_WIDTH)
        self.navigation.setMinimumExpandWidth(SIDEBAR_WIDTH)
        self.navigation.expand(useAni=False)  # it opened at its default width
        self.pages = QStackedWidget()
        layout.addWidget(self.navigation)
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        top, bottom = NavigationItemPosition.TOP, NavigationItemPosition.BOTTOM
        self._titles: dict[QWidget, str] = {}
        self._add(self.inbox, "inbox", FluentIcon.MAIL, "Inbox", top)
        self._add(self.library, "library", FluentIcon.LIBRARY, "Library", top)
        self._add(self.history, "history", FluentIcon.HISTORY, "History", top)
        self._add(self.folders, "folders", FluentIcon.FOLDER, "Folders", top)
        self._add(self.rules, "rules", FluentIcon.EDIT, "Rules", top)
        self._add(self.tidy, "tidy", FluentIcon.BROOM, "Tidy up", top)
        self._add(self.dashboard, "dashboard", FluentIcon.PIE_SINGLE, "Dashboard", top)
        self._add(self.ask, "ask", FluentIcon.CHAT, "Ask", top)
        self._add(self.settings, "settings", FluentIcon.SETTING, "Settings", bottom)
        self.set_page_visible(self.ask, False)  # only with AI on
        self.navigation.setCurrentItem("inbox")

        self.settings.ai_state.connect(self.set_ai_enabled)
        self.settings.message.connect(self.show_message)
        self.state = QLabel()
        self.statusBar().addPermanentWidget(self.state)

        self.inbox.moved.connect(self._after_move)
        self.inbox.count_changed.connect(self._show_count)
        self.history.undone.connect(self._after_move)
        self.folders.saved.connect(self._after_description)
        self.rules.changed.connect(self._after_rules)
        self.library.changed.connect(self._after_rules)
        self.tidy.changed.connect(self._after_move)
        self.pages.currentChanged.connect(self._page_changed)

    def _add(self, page: QWidget, key: str, icon, title: str, position) -> None:
        page.setObjectName(key)
        self.pages.addWidget(page)
        self._titles[page] = title
        self.navigation.addItem(
            key, icon, title, lambda *_, page=page: self.show_tab(page), position=position
        )

    # --- pages ------------------------------------------------------------------------

    def current_page(self) -> QWidget:
        """The page on show."""
        return self.pages.currentWidget()

    def page_title(self, page: QWidget) -> str:
        """The page's name in the sidebar (the Inbox's includes its count)."""
        return self.navigation.widget(page.objectName()).text()

    def page_titles(self) -> list[str]:
        """The names of the pages the sidebar shows, top to bottom."""
        return [
            self.page_title(page)
            for page in self._titles
            if self.navigation.widget(page.objectName()).isVisibleTo(self.navigation)
        ]

    def set_page_visible(self, page: QWidget, visible: bool) -> None:
        """Show or hide a page's sidebar entry."""
        self.navigation.widget(page.objectName()).setVisible(visible)

    def is_page_visible(self, page: QWidget) -> bool:
        """True if the sidebar offers the page."""
        return self.navigation.widget(page.objectName()).isVisibleTo(self.navigation)

    def set_ai_enabled(self, enabled: bool) -> None:
        """Show the AI features only when an AI provider is chosen in Settings."""
        self.set_page_visible(self.ask, enabled)
        self.library.set_ai_enabled(enabled)
        self.folders.set_ai_enabled(enabled)
        self.tidy.set_ai_enabled(enabled)

    def show_message(self, text: str) -> None:
        """A transient message in the status bar."""
        self.statusBar().showMessage(text, MESSAGE_MSECS)

    def set_state(self, text: str) -> None:
        """What AutoShelf is doing, shown permanently in the status bar."""
        self.state.setText(text)

    def show_tab(self, widget) -> None:
        """Switch to one of the pages."""
        self.pages.setCurrentWidget(widget)
        self.navigation.setCurrentItem(widget.objectName())

    def show_inbox(self, path: Path | None = None) -> None:
        """Switch to the Inbox, optionally selecting one file."""
        self.show_tab(self.inbox)
        self.inbox.select(path)
        self.inbox.refresh()

    def refresh_all(self) -> None:
        """Reload every screen."""
        self.inbox.refresh()
        self.history.refresh()
        self.folders.refresh()
        self.rules.refresh()
        self.settings.load()

    def after_learning(self) -> None:
        """The user moved files in their folders (e.g. answered the Inbox on the iPhone)."""
        self.inbox.refresh()
        self.history.refresh()
        self.folders.refresh()

    def _after_move(self, message: str) -> None:
        self.show_message(message)
        self.inbox.refresh()
        self.history.refresh()
        self.folders.refresh()

    def _after_description(self, message: str) -> None:
        self.show_message(message)
        self.inbox.refresh()  # suggestions depend on descriptions

    def _after_rules(self, message: str) -> None:
        if message:
            self.show_message(message)

    def _show_count(self, count: int) -> None:
        self.navigation.widget("inbox").setText(f"Inbox ({count})" if count else "Inbox")

    def _page_changed(self, index: int) -> None:
        widget = self.pages.widget(index)
        if widget is self.history:
            self.history.refresh()
        elif widget is self.folders:
            self.folders.refresh()
        elif widget is self.rules:
            self.rules.refresh()
        elif widget is self.library:
            self.library.refresh()
        elif widget is self.tidy:
            self.tidy.refresh()
        elif widget is self.dashboard:
            self.dashboard.refresh()
        elif widget is self.settings:
            self.settings.load()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.keep_running_on_close:
            event.ignore()
            self.hide()
        else:
            event.accept()
