"""The History screen: what AutoShelf did, in sentences, with Undo, corrections and "Why?"."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    FluentIcon,
    PushButton,
    RoundMenu,
    TableWidget,
    TitleLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from autoshelf import wording
from autoshelf.actions.undo import UndoError
from autoshelf.app_service import AppService
from autoshelf.db.repository import UNDOABLE_ACTIONS, HistoryEntry
from autoshelf.layout import INBOX_DIRNAME, RESERVED_PREFIX
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.why_panel import WhyPanel
from autoshelf.ui.widgets import plain_tooltip, show_menu

COLUMNS = ("What happened", "How", "When")
FORCE_HINT = "; use --force to undo anyway"  # CLI advice; the app asks instead
SHOW_WHY = "Why?"
HIDE_WHY = "Hide why"
UNDO_LAST_MAX = 100


def describe_reason(entry: HistoryEntry) -> str:
    """A readable version of a history row's reason."""
    return wording.history_reason(entry)


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


def can_correct(entry: HistoryEntry) -> bool:
    """True if the row put a file into one of the user's folders (so it can be corrected)."""
    if entry.state != "done" or entry.action != "move":
        return False
    folder = folder_of(entry.dst, entry.root)
    return not (
        folder in ("?", "(root)", INBOX_DIRNAME)
        or folder.startswith(RESERVED_PREFIX)
        or Path(folder).is_absolute()
    )


def sentence(entry: HistoryEntry) -> str:
    """The row's "What happened" text ("... (undone)" once it has been undone)."""
    text = wording.history_sentence(
        entry, folder_of(entry.src, entry.root), folder_of(entry.dst, entry.root)
    )
    if entry.state == "undone":
        return f"{text} (undone)"
    if entry.state == "failed":
        return f"{text} (failed)"
    return text


class HistoryView(QWidget):
    """What AutoShelf did, newest first, with Undo and corrections for the selected row."""

    undone = Signal(str)  # a message after something was undone

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._entries: list[HistoryEntry] = []

        refresh = TransparentToolButton(FluentIcon.SYNC)
        refresh.setToolTip("Look again")
        refresh.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(TitleLabel("History"), 1)
        top.addWidget(refresh)

        self.table = TableWidget()
        self.table.setColumnCount(len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        self.undo_button = PushButton(FluentIcon.RETURN, "Undo")
        self.undo_button.clicked.connect(self.undo_selected)
        self.correct_button = PushButton(FluentIcon.FOLDER, "Move to another folder…")
        self.correct_button.setToolTip(
            "Put the file where it belongs; AutoShelf learns from the correction"
        )
        self.correct_button.clicked.connect(self.correct_selected)
        self.why_button = TransparentPushButton(FluentIcon.INFO, SHOW_WHY)
        self.why_button.clicked.connect(self._toggle_why)

        self.menu = RoundMenu(parent=self)
        self.batch_action = Action("Undo everything done together with it")
        self.batch_action.triggered.connect(self.undo_selected_batch)
        undo_last = Action("Undo the last few moves…")
        undo_last.triggered.connect(self.ask_undo_last)
        self.menu.addActions([self.batch_action, undo_last])
        self.menu_button = TransparentToolButton(FluentIcon.MORE)
        self.menu_button.setToolTip("More ways to undo")
        self.menu_button.clicked.connect(lambda: show_menu(self.menu, self.menu_button))

        buttons = QHBoxLayout()
        buttons.addWidget(self.undo_button)
        buttons.addWidget(self.correct_button)
        buttons.addWidget(self.why_button)
        buttons.addStretch(1)
        buttons.addWidget(self.menu_button)

        self.why = WhyPanel()
        self.why.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        layout.addLayout(top)
        layout.addWidget(self.table, 3)
        layout.addLayout(buttons)
        layout.addWidget(self.why, 2)
        self._update_buttons()

    def refresh(self) -> None:
        """Reload the history in the background."""
        self.runner.run(self.service.history, self._show)

    def _show(self, entries: list[HistoryEntry]) -> None:
        selected = self._selected()
        self._entries = entries
        self.table.setRowCount(len(entries))
        faded = QBrush(self.palette().color(QPalette.ColorRole.PlaceholderText))
        for row, entry in enumerate(entries):
            when = datetime.fromisoformat(entry.ts).astimezone().strftime("%Y-%m-%d %H:%M")
            cells = (sentence(entry), wording.history_how(entry), when)
            tip = wording.explanation(entry.details) or entry.details.get("note") or ""
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                if tip:
                    item.setToolTip(plain_tooltip(f"#{entry.id}: {tip}"))
                if entry.state != "done" or column:
                    item.setForeground(faded)
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
        self.correct_button.setEnabled(entry is not None and can_correct(entry))
        self.why_button.setEnabled(entry is not None)
        self.batch_action.setEnabled(entry is not None and bool(entry.batch_id))
        if entry is None:
            self.why.clear_details()
            return
        name = Path(entry.dst or entry.src or "?").name
        headline = (
            f"#{entry.id} {describe_reason(entry)}: {name} -> {folder_of(entry.dst, entry.root)}"
        )
        root = Path(entry.root) if entry.root else None
        self.why.show_details(entry.details, root, headline)

    def _toggle_why(self) -> None:
        shown = not self.why.isVisible()
        self.why.setVisible(shown)
        self.why_button.setText(HIDE_WHY if shown else SHOW_WHY)

    def correct_selected(self) -> None:
        """Ask for the right folder and move the selected file there (AutoShelf learns)."""
        entry = self._selected()
        if entry is None or not can_correct(entry):
            return
        current = folder_of(entry.dst, entry.root)

        def ask(folders) -> None:
            choices = [folder.key for folder in folders if folder.key != current]
            if not choices:
                QMessageBox.information(self, "No other folder", "There is no other folder yet.")
                return
            name = Path(entry.dst).name
            folder, ok = QInputDialog.getItem(
                self, "Move to another folder", f"Where does {name} belong?", choices, 0, False
            )
            if ok and folder:
                self.runner.run(
                    lambda: self.service.correct(entry.id, folder),
                    lambda result: self._corrected(name, folder),
                    lambda exc: QMessageBox.warning(self, "Couldn't move the file", str(exc)),
                )

        self.runner.run(self.service.folders, ask)

    def _corrected(self, name: str, folder: str) -> None:
        self.undone.emit(f"Moved {name} to {folder}; AutoShelf will remember")
        self.refresh()

    def undo_selected(self) -> None:
        """Undo the selected row."""
        entry = self._selected()
        if entry is None or not can_undo(entry):
            return
        self._run_undo(lambda force: [self.service.undo(entry.id, force=force)])

    def undo_selected_batch(self) -> None:
        """Undo every move of the selected row's batch (e.g. one organize run)."""
        entry = self._selected()
        if entry is None or not entry.batch_id:
            return
        batch = entry.batch_id
        self._run_undo(lambda force: self.service.undo_batch(batch, force=force))

    def ask_undo_last(self) -> None:
        """Ask how many of the latest moves to undo, then undo them."""
        count, ok = QInputDialog.getInt(
            self, "Undo the last few moves", "How many moves?", 5, 1, UNDO_LAST_MAX
        )
        if ok:
            self.undo_last(count)

    def undo_last(self, count: int = 5) -> None:
        """Undo the last ``count`` moves, newest first."""
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
