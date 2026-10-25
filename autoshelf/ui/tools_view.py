"""More tools: one card per occasional tool (organize a folder, duplicates, versions,
better names, new folders, the overview); each card opens the tool's own page."""

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
    TitleLabel,
    TransparentPushButton,
)

from autoshelf.app_service import AppService
from autoshelf.ui.dashboard_view import DashboardView
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.tidy_view import (
    DuplicatesPage,
    NewFoldersPage,
    OrganizePage,
    RenamePage,
    VersionsPage,
)

CARD_COLUMNS = 3


@dataclass(frozen=True)
class Tool:
    """A card on the More tools page."""

    name: str
    text: str
    icon: FluentIcon


TOOLS = {
    "organize": Tool(
        "Organize a folder",
        "Sort a messy folder in one go: see where each file would go first, then undo it "
        "all at once if you like.",
        FluentIcon.BROOM,
    ),
    "duplicates": Tool(
        "Duplicates",
        "Identical files. Keep one and set the copies aside in _Duplicates; nothing is deleted.",
        FluentIcon.COPY,
    ),
    "versions": Tool(
        "Possible versions",
        "Files that look like versions of each other, such as report_v2 and report final.",
        FluentIcon.HISTORY,
    ),
    "renames": Tool(
        "Better names",
        "Files called scan_0034 or document(3) get a name from their title or first heading.",
        FluentIcon.EDIT,
    ),
    "new_folders": Tool(
        "New folders",
        "Topics waiting in your Inbox that none of your folders covers yet.",
        FluentIcon.FOLDER_ADD,
    ),
    "dashboard": Tool(
        "Overview",
        "Files per folder, how often AutoShelf was right, and files you haven't touched in months.",
        FluentIcon.PIE_SINGLE,
    ),
}


class ToolCard(CardWidget):
    """A clickable card naming one tool."""

    def __init__(self, tool: Tool, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(120)
        icon = IconWidget(tool.icon)
        icon.setFixedSize(24, 24)
        self.title = StrongBodyLabel(tool.name)
        text = BodyLabel(tool.text)
        text.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(icon)
        layout.addWidget(self.title)
        layout.addWidget(text, 1, Qt.AlignmentFlag.AlignTop)


class ToolsView(QWidget):
    """The cards, or the page of the tool chosen from them."""

    changed = Signal(str)  # a message after a tool moved, renamed or created something

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.organize = OrganizePage(service, runner)
        self.duplicates = DuplicatesPage(service, runner)
        self.versions = VersionsPage(service, runner)
        self.renames = RenamePage(service, runner)
        self.new_folders = NewFoldersPage(service, runner)
        self.dashboard = DashboardView(service, runner)
        for page in (self.duplicates, self.organize, self.renames, self.new_folders):
            page.changed.connect(self.changed)

        self.stack = QStackedWidget()
        self.cards: dict[str, ToolCard] = {}
        self._pages: dict[QWidget, QWidget] = {}  # tool -> its page (with the way back)
        cards_page = QWidget()
        grid = QGridLayout()
        grid.setSpacing(12)
        for index, (key, tool) in enumerate(TOOLS.items()):
            widget = getattr(self, key)
            card = ToolCard(tool)
            card.clicked.connect(lambda widget=widget: self.open(widget))
            self.cards[key] = card
            grid.addWidget(card, index // CARD_COLUMNS, index % CARD_COLUMNS)
            page = self._tool_page(tool, widget)
            self._pages[widget] = page
            self.stack.addWidget(page)
        cards_layout = QVBoxLayout(cards_page)
        cards_layout.setContentsMargins(28, 20, 28, 20)
        cards_layout.setSpacing(16)
        cards_layout.addWidget(TitleLabel("More tools"))
        cards_layout.addLayout(grid)
        cards_layout.addStretch(1)
        self.cards_page = cards_page
        self.stack.insertWidget(0, cards_page)
        self.stack.setCurrentWidget(cards_page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)

    def _tool_page(self, tool: Tool, widget: QWidget) -> QWidget:
        page = QWidget()
        back = TransparentPushButton(FluentIcon.LEFT_ARROW, "More tools")
        back.clicked.connect(self.show_cards)
        heading = QHBoxLayout()
        heading.addWidget(back)
        heading.addWidget(TitleLabel(tool.name))
        heading.addStretch(1)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.addLayout(heading)
        layout.addWidget(widget, 1)
        return page

    @property
    def tools(self) -> list[QWidget]:
        """Every tool's widget, in the order of the cards."""
        return list(self._pages)

    def current_tool(self) -> QWidget | None:
        """The tool on show (None while the cards are)."""
        page = self.stack.currentWidget()
        return next((tool for tool, shown in self._pages.items() if shown is page), None)

    def show_cards(self) -> None:
        """Back to the cards."""
        self.stack.setCurrentWidget(self.cards_page)

    def open(self, tool: QWidget) -> None:
        """Show one tool's page (and load what is cheap to load)."""
        self.stack.setCurrentWidget(self._pages[tool])
        if tool is self.organize:
            self.organize.refresh()
        elif tool is self.dashboard:
            self.dashboard.refresh()

    def refresh(self) -> None:
        """Reload what is cheap to reload (scans run when asked)."""
        self.organize.refresh()
        if self.current_tool() is self.dashboard:
            self.dashboard.refresh()

    def sort_files(self, paths: list[Path]) -> None:
        """Show the Organize preview for files chosen in Explorer."""
        self.open(self.organize)
        self.organize.preview_files(paths)

    def set_ai_enabled(self, enabled: bool) -> None:
        """Offer AI-made names only when an AI provider is on."""
        self.renames.ai_button.setVisible(enabled)
