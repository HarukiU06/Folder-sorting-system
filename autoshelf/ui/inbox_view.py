"""The Inbox screen: files waiting for a decision, with the best folders as big buttons."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QListWidgetItem, QMessageBox, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    ListWidget,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
    TransparentPushButton,
    TransparentToolButton,
)

from autoshelf import wording
from autoshelf.actions.inbox import InboxItem
from autoshelf.app_service import AppService
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.why_panel import WhyPanel
from autoshelf.ui.widgets import plain, show_in_explorer, show_menu

MAX_SUGGESTIONS = 3
EMPTY_TEXT = "Nothing is waiting. Files AutoShelf isn't sure about will show up here."
SHOW_WHY = "Why is it waiting?"
HIDE_WHY = "Hide why it's waiting"
UNDO_MSECS = 10_000


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

        self.summary = plain(TitleLabel())
        refresh = TransparentToolButton(FluentIcon.SYNC)
        refresh.setToolTip("Look again")
        refresh.clicked.connect(self.refresh)
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(refresh)

        self.list = ListWidget()
        self.list.currentRowChanged.connect(self._show_current)
        self.list.setMaximumWidth(320)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)

        self.detail = SimpleCardWidget()
        self.name = plain(SubtitleLabel())
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title = plain(CaptionLabel())
        self.title.setWordWrap(True)
        self.why = plain(BodyLabel())
        self.why.setWordWrap(True)

        self.menu_button = TransparentToolButton(FluentIcon.MORE)
        self.menu_button.setToolTip("More actions")
        self.menu = RoundMenu(parent=self)
        self.rename_action = Action(FluentIcon.EDIT, "Rename")
        self.rename_action.triggered.connect(self._rename)
        self.menu.addAction(self.rename_action)
        open_action = Action(FluentIcon.DOCUMENT, "Open the file")
        open_action.triggered.connect(self._open_file)
        show_action = Action(FluentIcon.FOLDER, "Show in Explorer")
        show_action.triggered.connect(self._show_in_explorer)
        self.menu.addActions([open_action, show_action])
        self.menu_button.clicked.connect(lambda: show_menu(self.menu, self.menu_button))
        heading = QHBoxLayout()
        names = QVBoxLayout()
        names.setSpacing(2)
        names.addWidget(self.name)
        names.addWidget(self.title)
        names.addWidget(self.why)
        heading.addLayout(names, 1)
        heading.addWidget(self.menu_button, 0, Qt.AlignmentFlag.AlignTop)

        self.suggestions = QHBoxLayout()
        self.suggestion_buttons: list[PushButton] = []
        self.other = ComboBox()
        self.other.setMinimumWidth(220)
        self.move_other = PushButton("Move there")
        self.move_other.clicked.connect(self._move_to_other)
        other_row = QHBoxLayout()
        other_row.addWidget(BodyLabel("Another folder:"))
        other_row.addWidget(self.other, 1)
        other_row.addWidget(self.move_other)
        self.tag_row = QHBoxLayout()
        self.tag_label = BodyLabel("Also tag it:")
        self.tag_row.addWidget(self.tag_label)
        self.tag_boxes: list[CheckBox] = []
        self.tag_row.addStretch(1)

        self.preview = PlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("No text preview for this file.")
        self.why_button = TransparentPushButton(FluentIcon.INFO, SHOW_WHY)
        self.why_button.clicked.connect(self._toggle_why)
        self.why_panel = WhyPanel()
        self.why_panel.setVisible(False)

        detail = QVBoxLayout(self.detail)
        detail.setContentsMargins(20, 16, 20, 16)
        detail.setSpacing(10)
        detail.addLayout(heading)
        detail.addSpacing(4)
        detail.addWidget(StrongBodyLabel("Where should it go?"))
        detail.addLayout(self.suggestions)
        detail.addLayout(other_row)
        detail.addLayout(self.tag_row)
        detail.addWidget(self.preview, 1)
        detail.addWidget(self.why_button, 0, Qt.AlignmentFlag.AlignLeft)
        detail.addWidget(self.why_panel, 1)

        self.empty = BodyLabel(EMPTY_TEXT)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(self.list, 1)
        body.addWidget(self.detail, 3)
        body.addWidget(self.empty, 3)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(16)
        layout.addLayout(top)
        layout.addLayout(body, 1)
        self._show_items([], [])

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the waiting files and their suggestions in the background."""
        if self._loading:
            self._reload = True
            return
        self._loading = True
        if not self._items:
            self.summary.setText("Looking at your Inbox...")
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
            f"{count} file{'' if count == 1 else 's'} waiting for you" if count else "Inbox"
        )
        self.list.setVisible(bool(items))
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
        self.why.setText(wording.waiting_text(item.reason))
        self.preview.setPlainText(item.excerpt or "")
        root = item.path.parent.parent  # the file is in <root>/_Inbox
        self.why_panel.show_details(item.details, root)
        for button in self.suggestion_buttons:
            self.suggestions.removeWidget(button)
            button.deleteLater()
        self.suggestion_buttons = []
        top = item.classification.top(MAX_SUGGESTIONS) if item.classification else ()
        for index, score in enumerate(top):
            button = (PrimaryPushButton if index == 0 else PushButton)(score.folder)
            button.setMinimumHeight(48)
            button.setToolTip(
                ("Best guess: " if index == 0 else "") + f"move {item.path.name} to {score.folder}"
            )
            button.clicked.connect(lambda _=False, folder=score.folder: self.move_to(folder))
            self.suggestions.addWidget(button, 1)
            self.suggestion_buttons.append(button)
        if top:
            self.other.setCurrentText(top[0].folder)
        for box in self.tag_boxes:
            self.tag_row.removeWidget(box)
            box.deleteLater()
        self.tag_boxes = []
        for name in item.tag_suggestions:
            box = CheckBox(name)
            box.setToolTip("It fits this folder too: tag it, so it shows up under the tag")
            self.tag_row.insertWidget(self.tag_row.count() - 1, box)
            self.tag_boxes.append(box)
        self.tag_label.setVisible(bool(self.tag_boxes))
        self.rename_action.setVisible(item.rename is not None)
        if item.rename is not None:
            self.rename_action.setText(f"Rename to “{item.rename.new_name}”")

    def _toggle_why(self) -> None:
        shown = not self.why_panel.isVisible()
        self.why_panel.setVisible(shown)
        self.preview.setVisible(not shown)
        self.why_button.setText(HIDE_WHY if shown else SHOW_WHY)

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
            self._offer_undo(f"Moved {path.name} to {folder}. AutoShelf will remember.", result)
            self.refresh()
            self.runner.run(
                lambda: self.service.rule_suggestion_for(folder), self.offer_rule, lambda exc: None
            )

        def failed(exc: Exception) -> None:
            self.detail.setEnabled(True)
            QMessageBox.warning(self, "Couldn't move the file", str(exc))
            self.refresh()

        self.runner.run(lambda: self.service.assign(path, folder, tags), done, failed)

    def _offer_undo(self, text: str, result) -> None:
        history_id = result.history_id
        bar = InfoBar.success(
            "", text, duration=UNDO_MSECS, position=InfoBarPosition.TOP, parent=self
        )
        undo = PushButton("Undo")

        def undone(_) -> None:
            self.moved.emit(f"Put {result.dst.name} back in the Inbox")
            self.refresh()

        def undo_move() -> None:
            bar.close()
            self.runner.run(
                lambda: self.service.undo(history_id),
                undone,
                lambda exc: QMessageBox.warning(self, "Couldn't undo", str(exc)),
            )

        undo.clicked.connect(undo_move)
        bar.addWidget(undo)

    def offer_rule(self, suggestion) -> None:
        """After an answer: offer the rule the user's choices point to, if there is one."""
        if suggestion is None:
            return
        answer = QMessageBox.question(
            self,
            "Make this a rule?",
            wording.rule_suggestion(suggestion)
            + "\n\nA rule would send such files there by itself; you can change or remove it "
            "in Settings, under Rules. (No: don't suggest it again.)",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.run(
                lambda: self.service.add_suggested_rule(suggestion),
                lambda rule: self.moved.emit(f"Added the rule {rule.name!r}"),
                lambda exc: QMessageBox.warning(self, "Couldn't add the rule", str(exc)),
            )
        else:
            self.runner.run(lambda: self.service.dismiss_rule_suggestion(suggestion.key))

    def _rename(self) -> None:
        item = self._current_item()
        if item is None or item.rename is None:
            return
        new_name = item.rename.new_name
        self.runner.run(
            lambda: self.service.rename(item.path, new_name),
            lambda result: (self.moved.emit(f"Renamed to {result.dst.name}"), self.refresh()),
            lambda exc: QMessageBox.warning(self, "Couldn't rename", str(exc)),
        )

    def _open_file(self) -> None:
        item = self._current_item()
        if item is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.path)))

    def _show_in_explorer(self) -> None:
        item = self._current_item()
        if item is not None:
            show_in_explorer(item.path)
