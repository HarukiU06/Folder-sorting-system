"""The Folders screen: your folders, how many files they hold, and their descriptions."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService, FolderInfo
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import open_folder

HELP = (
    "These are the folders AutoShelf sorts into. It never creates folders itself: make new "
    "ones in Explorer or on your iPhone and they appear here. A short description (double-"
    "click it) helps AutoShelf recognise what belongs in a folder, especially while the "
    "folder is still empty."
)
DESCRIPTION_COLUMN = 2


class FoldersView(QWidget):
    """A table of category folders with editable descriptions."""

    saved = Signal(str)  # a message after a description was saved

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._folders: list[FolderInfo] = []

        help_text = QLabel(HELP)
        help_text.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Folder", "Files", "Description"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(DESCRIPTION_COLUMN, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._edited)
        self.status = QLabel()

        open_root = QPushButton("Open in Explorer")
        open_root.clicked.connect(self._open_root)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(open_root)
        buttons.addWidget(refresh)

        layout = QVBoxLayout(self)
        layout.addWidget(help_text)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        """Reload the folders in the background."""
        self.runner.run(self.service.folders, self._show, self._failed)

    def _failed(self, exc: Exception) -> None:
        self._show([])
        self.status.setText(str(exc))

    def _show(self, folders: list[FolderInfo]) -> None:
        self._folders = folders
        self.table.blockSignals(True)
        self.table.setRowCount(len(folders))
        for row, folder in enumerate(folders):
            name = QTableWidgetItem(folder.key.replace("/", " / "))
            count = QTableWidgetItem(str(folder.files))
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            for item in (name, count):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            description = QTableWidgetItem(folder.description)
            description.setToolTip("Double-click to describe what belongs in this folder")
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, count)
            self.table.setItem(row, DESCRIPTION_COLUMN, description)
        self.table.blockSignals(False)
        self.status.setText(
            "" if folders else "No folders yet: create some in your AutoShelf folder."
        )

    def _edited(self, item: QTableWidgetItem) -> None:
        if item.column() != DESCRIPTION_COLUMN or item.row() >= len(self._folders):
            return
        key = self._folders[item.row()].key
        text = item.text().strip()
        self.runner.run(
            lambda: self.service.describe(key, text),
            lambda _: self.saved.emit(f"Saved the description of {key}"),
            self._failed,
        )

    def _open_root(self) -> None:
        self.runner.run(self.service.root, lambda root: root and open_folder(root))
