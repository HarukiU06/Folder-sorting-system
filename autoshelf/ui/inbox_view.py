"""The Inbox screen: files waiting for a decision, with suggestions and a preview."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from autoshelf.actions.inbox import InboxItem
from autoshelf.app_service import AppService
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.why_panel import WhyPanel
from autoshelf.ui.widgets import show_in_explorer

MAX_SUGGESTIONS = 3
EMPTY_TEXT = (
    "Nothing is waiting.\n\nFiles that AutoShelf isn't sure about will appear here, "
    "with its best guesses."
)


class InboxView(QWidget):
    """Lists waiting files; choosing a folder moves the file there."""

    moved = Signal(str)  # a message after a file was moved
    count_changed = Signal(int)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._items: list[InboxItem] = []
        self._loading = False
        self._reload = False
        self._wanted: Path | None = None

        self.summary = QLabel()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(refresh)

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._show_current)

        self.detail = QWidget()
        self.name = QLabel()
        font = QFont(self.name.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.2)
        self.name.setFont(font)
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.why = QLabel()
        self.why.setWordWrap(True)
        self.why.setStyleSheet("color: palette(placeholder-text);")

        self.suggestions = QHBoxLayout()
        self.suggestion_buttons: list[QPushButton] = []
        self.other = QComboBox()
        self.other.setMinimumContentsLength(18)
        self.move_other = QPushButton("Move")
        self.move_other.clicked.connect(self._move_to_other)
        other_row = QHBoxLayout()
        other_row.addWidget(QLabel("Other folder:"))
        other_row.addWidget(self.other, 1)
        other_row.addWidget(self.move_other)
        self.tag_row = QHBoxLayout()
        self.tag_label = QLabel("Also tag it:")
        self.tag_row.addWidget(self.tag_label)
        self.tag_boxes: list[QCheckBox] = []
        self.tag_row.addStretch(1)

        open_file = QPushButton("Open file")
        open_file.clicked.connect(self._open_file)
        show = QPushButton("Show in Explorer")
        show.clicked.connect(self._show_in_explorer)
        file_row = QHBoxLayout()
        file_row.addWidget(open_file)
        file_row.addWidget(show)
        file_row.addStretch(1)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("No text preview for this file.")
        self.why_panel = WhyPanel()
        self.pages = QTabWidget()
        self.pages.addTab(self.preview, "Preview")
        self.pages.addTab(self.why_panel, "Why?")

        detail = QVBoxLayout(self.detail)
        detail.addWidget(self.name)
        detail.addWidget(self.title)
        detail.addWidget(self.why)
        detail.addSpacing(6)
        detail.addWidget(QLabel("Move it to:"))
        detail.addLayout(self.suggestions)
        detail.addLayout(other_row)
        detail.addLayout(self.tag_row)
        detail.addLayout(file_row)
        detail.addWidget(self.pages, 1)

        self.empty = QLabel(EMPTY_TEXT)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        splitter = QSplitter()
        splitter.addWidget(self.list)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self.detail)
        right_layout.addWidget(self.empty)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([260, 560])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(splitter, 1)
        self._show_items([], [])

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the waiting files and their suggestions in the background."""
        if self._loading:
            self._reload = True
            return
        self._loading = True
        if not self._items:
            self.summary.setText("Looking at your Inbox... (the first time loads the model)")
        self.runner.run(self._load, self._loaded, self._load_failed)

    def select(self, path: Path | None) -> None:
        """Show this file once it is listed."""
        self._wanted = path
        self._select_wanted()

    def _load(self) -> tuple[list[InboxItem], list[str]]:
        items = self.service.inbox()
        folders = [folder.key for folder in self.service.folders()]
        return items, folders

    def _loaded(self, result: tuple[list[InboxItem], list[str]]) -> None:
        self._finish_loading()
        self._show_items(*result)

    def _load_failed(self, exc: Exception) -> None:
        self._finish_loading()
        self._show_items([], [])
        self.summary.setText(str(exc))

    def _finish_loading(self) -> None:
        self._loading = False
        if self._reload:
            self._reload = False
            self.refresh()

    def _show_items(self, items: list[InboxItem], folders: list[str]) -> None:
        current = self._current_item()
        keep = current.path if current else None
        self._items = items
        self.other.clear()
        self.other.addItems(folders)
        self.list.blockSignals(True)
        self.list.clear()
        for item in items:
            entry = QListWidgetItem(item.path.name)
            entry.setToolTip(str(item.path))
            self.list.addItem(entry)
        self.list.blockSignals(False)
        count = len(items)
        self.summary.setText(
            f"{count} file{'' if count == 1 else 's'} waiting for you" if count else ""
        )
        self.detail.setVisible(bool(items))
        self.empty.setVisible(not items)
        self.count_changed.emit(count)
        if items:
            paths = [item.path for item in items]
            self.list.setCurrentRow(paths.index(keep) if keep in paths else 0)
            self._show_current(self.list.currentRow())
        self._select_wanted()

    def _select_wanted(self) -> None:
        if self._wanted is None:
            return
        for row, item in enumerate(self._items):
            if item.path == self._wanted:
                self.list.setCurrentRow(row)
                self._wanted = None
                return

    # --- the selected file --------------------------------------------------------

    def _current_item(self) -> InboxItem | None:
        row = self.list.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _show_current(self, row: int) -> None:
        item = self._current_item()
        if item is None:
            return
        self.name.setText(item.path.name)
        self.title.setText(f"Title: {item.title}" if item.title else "")
        self.title.setVisible(bool(item.title))
        self.why.setText(f"Why it's here: {item.explanation}")
        self.preview.setPlainText(item.excerpt or "")
        root = item.path.parent.parent  # the file is in <root>/_Inbox
        self.why_panel.show_details(item.details, root)
        for button in self.suggestion_buttons:
            self.suggestions.removeWidget(button)
            button.deleteLater()
        self.suggestion_buttons = []
        top = item.classification.top(MAX_SUGGESTIONS) if item.classification else ()
        for score in top:
            button = QPushButton(f"{score.folder}   {score.score:.2f}")
            button.setToolTip(f"Move {item.path.name} to {score.folder}")
            button.clicked.connect(lambda _=False, folder=score.folder: self.move_to(folder))
            self.suggestions.addWidget(button)
            self.suggestion_buttons.append(button)
        if top:
            self.other.setCurrentText(top[0].folder)
        for box in self.tag_boxes:
            self.tag_row.removeWidget(box)
            box.deleteLater()
        self.tag_boxes = []
        for name in item.tag_suggestions:
            box = QCheckBox(name)
            box.setToolTip("It fits this folder too: tag it, so it shows up under the tag")
            self.tag_row.insertWidget(self.tag_row.count() - 1, box)
            self.tag_boxes.append(box)
        self.tag_label.setVisible(bool(self.tag_boxes))

    def _move_to_other(self) -> None:
        if self.other.currentText():
            self.move_to(self.other.currentText())

    def move_to(self, folder: str) -> None:
        """Move the selected file into ``folder`` (in the background)."""
        item = self._current_item()
        if item is None:
            return
        self.detail.setEnabled(False)
        path = item.path
        leaf = folder.rsplit("/", 1)[-1]
        tags = [box.text() for box in self.tag_boxes if box.isChecked() and box.text() != leaf]

        def done(result) -> None:
            self.detail.setEnabled(True)
            self.moved.emit(f"Moved {path.name} to {folder}")
            self.refresh()

        def failed(exc: Exception) -> None:
            self.detail.setEnabled(True)
            QMessageBox.warning(self, "Couldn't move the file", str(exc))
            self.refresh()

        self.runner.run(lambda: self.service.assign(path, folder, tags), done, failed)

    def _open_file(self) -> None:
        item = self._current_item()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.path)))

    def _show_in_explorer(self) -> None:
        item = self._current_item()
        if item is not None:
            show_in_explorer(item.path)
