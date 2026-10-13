"""The Rules screen: rules that send matching files straight to a folder (SPEC section 4.4)."""

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf.actions.rules import RULE_KINDS, describe_condition
from autoshelf.app_service import AppService, RuleTest
from autoshelf.db.repository import RuleRecord
from autoshelf.ui.tasks import TaskRunner

HELP = (
    "Rules are checked from top to bottom before AutoShelf reads a file's meaning; the "
    "first rule that matches decides where the file goes. A rule never creates a folder: "
    "if its folder is gone, the file waits in the Inbox."
)
PATTERN_HINTS = {
    "name": "A regular expression, e.g. invoice|receipt (any case)",
    "extension": "One or more extensions, e.g. .ipynb or pdf, docx",
    "text": "Words the text must contain, e.g. Syllabus (any case)",
}
COLUMNS = ("On", "Name", "When", "Move to")


class RuleDialog(QDialog):
    """Create or edit one rule."""

    def __init__(self, folders: list[str], rule: RuleRecord | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit rule" if rule else "New rule")
        self.name = QLineEdit(rule.name if rule else "")
        self.kind = QComboBox()
        for kind, label in RULE_KINDS.items():
            self.kind.addItem(label, kind)
        self.pattern = QLineEdit(rule.pattern if rule else "")
        self.hint = QLabel()
        self.hint.setStyleSheet("color: palette(placeholder-text);")
        self.target = QComboBox()
        self.target.addItems(folders)
        self.enabled = QCheckBox("Rule is on")
        self.enabled.setChecked(rule.enabled if rule else True)
        if rule is not None:
            self.kind.setCurrentIndex(max(0, self.kind.findData(rule.kind)))
            self.target.setCurrentText(rule.target_folder)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Name", self.name)
        form.addRow("When the", self.kind)
        form.addRow("", self.pattern)
        form.addRow("", self.hint)
        form.addRow("Move it to", self.target)
        form.addRow("", self.enabled)
        form.addRow(buttons)
        self.resize(460, self.sizeHint().height())

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self.hint.setText(PATTERN_HINTS[kind])
        self.pattern.setPlaceholderText(PATTERN_HINTS[kind].split("e.g. ")[-1])

    def values(self) -> tuple[str, str, str, str, bool]:
        """(name, kind, pattern, target folder, enabled) as entered."""
        return (
            self.name.text(),
            self.kind.currentData(),
            self.pattern.text(),
            self.target.currentText(),
            self.enabled.isChecked(),
        )


class RulesView(QWidget):
    """Lists rules in checking order; add, edit, reorder, switch off, delete and test."""

    changed = Signal(str)  # a message after rules changed

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._rules: list[RuleRecord] = []

        help_text = QLabel(HELP)
        help_text.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._toggled)
        self.table.itemDoubleClicked.connect(lambda _: self.edit_selected())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self.add_button = QPushButton("Add...")
        self.add_button.clicked.connect(self.add_rule)
        self.edit_button = QPushButton("Edit...")
        self.edit_button.clicked.connect(self.edit_selected)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self.delete_selected)
        self.up_button = QPushButton("Move up")
        self.up_button.clicked.connect(lambda: self._move(-1))
        self.down_button = QPushButton("Move down")
        self.down_button.clicked.connect(lambda: self._move(1))
        test = QPushButton("Test on a file...")
        test.clicked.connect(self.test_file)
        buttons = QHBoxLayout()
        for button in (
            self.add_button,
            self.edit_button,
            self.delete_button,
            self.up_button,
            self.down_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(test)

        layout = QVBoxLayout(self)
        layout.addWidget(help_text)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        self._update_buttons()

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the rules in the background."""
        self.runner.run(self.service.rules, self._show)

    def _show(self, rules: list[RuleRecord]) -> None:
        selected = self._selected()
        self._rules = rules
        self.table.blockSignals(True)
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            on.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            cells = (
                on,
                QTableWidgetItem(rule.name),
                QTableWidgetItem(describe_condition(rule.kind, rule.pattern)),
                QTableWidgetItem(rule.target_folder),
            )
            for column, item in enumerate(cells):
                self.table.setItem(row, column, item)
        self.table.blockSignals(False)
        if selected is not None:
            ids = [rule.id for rule in rules]
            if selected.id in ids:
                self.table.selectRow(ids.index(selected.id))
        self.status.setText(
            "" if rules else "No rules yet. Example: file name matches invoice|receipt -> Finance."
        )
        self._update_buttons()

    def _selected(self) -> RuleRecord | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._rules):
            return None
        return self._rules[rows[0].row()]

    def _update_buttons(self) -> None:
        rule = self._selected()
        for button in (self.edit_button, self.delete_button, self.up_button, self.down_button):
            button.setEnabled(rule is not None)

    # --- changes ------------------------------------------------------------------

    def _after(self, message: str):
        def done(_=None) -> None:
            self.changed.emit(message)
            self.refresh()

        return done

    def _failed(self, exc: Exception) -> None:
        QMessageBox.warning(self, "Couldn't save the rule", str(exc))
        self.refresh()

    def add_rule(self) -> None:
        """Ask for a new rule and save it."""
        self.runner.run(self.service.folders, lambda folders: self._open_dialog(folders, None))

    def edit_selected(self) -> None:
        """Edit the selected rule."""
        rule = self._selected()
        if rule is not None:
            self.runner.run(self.service.folders, lambda folders: self._open_dialog(folders, rule))

    def _open_dialog(self, folders, rule: RuleRecord | None) -> None:
        keys = [folder.key for folder in folders]
        if not keys:
            QMessageBox.information(
                self, "No folders yet", "Create a folder first: rules move files into folders."
            )
            return
        dialog = RuleDialog(keys, rule, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        name, kind, pattern, target, enabled = dialog.values()
        if rule is None:
            self.runner.run(
                lambda: self.service.add_rule(name, kind, pattern, target, enabled=enabled),
                self._after(f"Added the rule {name!r}"),
                self._failed,
            )
        else:
            updated = replace(
                rule, name=name, kind=kind, pattern=pattern, target_folder=target, enabled=enabled
            )
            self.runner.run(
                lambda: self.service.update_rule(updated),
                self._after(f"Saved the rule {name!r}"),
                self._failed,
            )

    def delete_selected(self) -> None:
        """Delete the selected rule after asking."""
        rule = self._selected()
        if rule is None:
            return
        answer = QMessageBox.question(self, "Delete rule", f"Delete the rule {rule.name!r}?")
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.run(
                lambda: self.service.delete_rule(rule.id),
                self._after(f"Deleted the rule {rule.name!r}"),
            )

    def _move(self, offset: int) -> None:
        rule = self._selected()
        if rule is not None:
            self.runner.run(lambda: self.service.move_rule(rule.id, offset), self._after(""))

    def _toggled(self, item: QTableWidgetItem) -> None:
        if item.column() != 0 or item.row() >= len(self._rules):
            return
        rule = self._rules[item.row()]
        enabled = item.checkState() == Qt.CheckState.Checked
        state = "on" if enabled else "off"
        self.runner.run(
            lambda: self.service.set_rule_enabled(rule.id, enabled),
            self._after(f"The rule {rule.name!r} is {state}"),
        )

    # --- testing ------------------------------------------------------------------

    def test_file(self) -> None:
        """Pick any file and show which rule would handle it (the file is only read)."""
        path, _ = QFileDialog.getOpenFileName(self, "Test the rules on a file")
        if path:
            self.runner.run(lambda: self.service.test_rules(Path(path)), self._show_test)

    def _show_test(self, result: RuleTest) -> None:
        self.status.setText(f"{result.path.name}: {result.message}")
