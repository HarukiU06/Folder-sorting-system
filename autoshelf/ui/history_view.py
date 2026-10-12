"""The History screen: every move, why it happened, and Undo."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf.actions.undo import UndoError
from autoshelf.app_service import AppService
from autoshelf.db.repository import UNDOABLE_ACTIONS, HistoryEntry
from autoshelf.ui.tasks import TaskRunner

COLUMNS = ("When", "File", "From", "To", "Why", "Score", "State")
_REASONS = {"auto": "Sorted automatically", "ask": "Not sure: sent to Inbox", "user": "You chose"}
FORCE_HINT = "; use --force to undo anyway"  # CLI advice; the app asks instead
_STATES = {"done": "Done", "undone": "Undone", "failed": "Failed", "pending": "In progress"}


def describe_reason(entry: HistoryEntry) -> str:
    """A readable version of a history row's reason."""
    if entry.action == "undo":
        return f"Undo of #{entry.undo_of}" if entry.undo_of else "Undo"
    return _REASONS.get(entry.reason, entry.reason)


def folder_of(path: str | None, root: str | None) -> str:
    """The folder part of a path, relative to the root ("(root)" for the root itself)."""
    if not path:
        return "?"
    parent = Path(path).parent
    if root:
        try:
            relative = parent.relative_to(root)
        except ValueError:
            return str(parent)
        return "(root)" if str(relative) == "." else relative.as_posix()
    return str(parent)


def can_undo(entry: HistoryEntry) -> bool:
    """True if Undo is offered for this row."""
    return entry.state == "done" and entry.action in UNDOABLE_ACTIONS


class HistoryView(QWidget):
    """A table of recent history with Undo buttons."""

    undone = Signal(str)  # a message after something was undone

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._entries: list[HistoryEntry] = []

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.undo_button = QPushButton("Undo selected")
        self.undo_button.clicked.connect(self.undo_selected)
        self.count = QSpinBox()
        self.count.setRange(1, 100)
        self.count.setValue(5)
        undo_last = QPushButton("Undo the last")
        undo_last.clicked.connect(self.undo_last)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)

        buttons = QHBoxLayout()
        buttons.addWidget(self.undo_button)
        buttons.addSpacing(20)
        buttons.addWidget(undo_last)
        buttons.addWidget(self.count)
        buttons.addWidget(QLabel("moves"))
        buttons.addStretch(1)
        buttons.addWidget(refresh)

        layout = QVBoxLayout(self)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self._update_buttons()

    def refresh(self) -> None:
        """Reload the history in the background."""
        self.runner.run(self.service.history, self._show)

    def _show(self, entries: list[HistoryEntry]) -> None:
        selected = self._selected()
        self._entries = entries
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            when = datetime.fromisoformat(entry.ts).astimezone().strftime("%Y-%m-%d %H:%M")
            cells = (
                when,
                Path(entry.dst or entry.src or "?").name,
                folder_of(entry.src, entry.root),
                folder_of(entry.dst, entry.root),
                describe_reason(entry),
                f"{entry.confidence:.2f}" if entry.confidence is not None else "",
                _STATES.get(entry.state, entry.state),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                tip = entry.details.get("explanation") or entry.details.get("note") or ""
                if tip:
                    item.setToolTip(f"#{entry.id}: {tip}")
                self.table.setItem(row, column, item)
        if selected is not None:
            ids = [entry.id for entry in entries]
            if selected.id in ids:
                self.table.selectRow(ids.index(selected.id))
        self._update_buttons()

    def _selected(self) -> HistoryEntry | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._entries):
            return None
        return self._entries[rows[0].row()]

    def _update_buttons(self) -> None:
        entry = self._selected()
        self.undo_button.setEnabled(entry is not None and can_undo(entry))

    def undo_selected(self) -> None:
        """Undo the selected row."""
        entry = self._selected()
        if entry is None or not can_undo(entry):
            return
        self._run_undo(lambda force: [self.service.undo(entry.id, force=force)])

    def undo_last(self) -> None:
        """Undo the last N moves, newest first."""
        count = self.count.value()
        self._run_undo(lambda force: self.service.undo_last(count, force=force))

    def _run_undo(self, action, force: bool = False) -> None:
        def done(results) -> None:
            names = ", ".join(result.src.name for result in results) or "nothing"
            self.undone.emit(f"Undid: {names}")
            self.refresh()

        def failed(exc: Exception) -> None:
            self.refresh()
            for result in getattr(exc, "completed", []):
                self.undone.emit(f"Undid: {result.src.name}")
            message = str(exc).replace(FORCE_HINT, "")
            if isinstance(exc, UndoError) and "has changed" in message and not force:
                answer = QMessageBox.question(
                    self,
                    "The file has changed",
                    message + ". Undo anyway? The edited file is moved back as it is now.",
                )
                if answer == QMessageBox.StandardButton.Yes:
                    self._run_undo(action, force=True)
                return
            QMessageBox.warning(self, "Couldn't undo", message)

        self.runner.run(lambda: action(force), done, failed)
