"""The Rules screen: rules that move, tag or rename matching files (SPEC 4.4, Phase 12),
and rules suggested from the user's own choices."""

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
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf import wording
from autoshelf.actions.rules import RULE_KINDS, TEMPLATE_FIELDS
from autoshelf.app_service import AppService, RuleTest
from autoshelf.db.repository import RuleRecord
from autoshelf.features.rule_suggestions import RuleSuggestion
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import plain_label, plain_tooltip

HELP = (
    "Rules are checked from top to bottom before AutoShelf reads a file's meaning; the "
    "first rule whose conditions hold applies. A rule can move the file to a folder, tag "
    "it, and rename it; a rename by your own rule is done without asking and can be undone "
    "in History. A rule never creates a folder: if its folder is gone, the file waits in "
    "the Inbox."
)
PATTERN_HINTS = {
    "name": "A regular expression, e.g. invoice|receipt (any case)",
    "extension": "One or more extensions, e.g. .ipynb or pdf, docx",
    "text": "Words the text must contain, e.g. Syllabus (any case)",
    "size": "Sizes like 500KB or 10MB; leave one empty for no limit",
    "date": "Dates like 2025-01-31 (when the file was last changed); one may be empty",
}
DONT_MOVE = "(let AutoShelf decide)"
COLUMNS = ("On", "Name", "When", "Then")


class ConditionRow(QWidget):
    """One condition: a kind and its value (two values for a size or date range)."""

    def __init__(self, condition: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.kind = QComboBox()
        for kind, label in RULE_KINDS.items():
            self.kind.addItem(label, kind)
        self.first = QLineEdit()
        self.second = QLineEdit()
        self.remove = QPushButton("Remove")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.kind)
        layout.addWidget(self.first, 1)
        layout.addWidget(self.second, 1)
        layout.addWidget(self.remove)
        if condition:
            kind = condition.get("kind")
            self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
            if kind == "size":
                self.first.setText(_size_value(condition.get("min")))
                self.second.setText(_size_value(condition.get("max")))
            elif kind == "date":
                self.first.setText(condition.get("from") or "")
                self.second.setText(condition.get("to") or "")
            else:
                self.first.setText(str(condition.get("pattern", "")))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._kind_changed()

    def _kind_changed(self) -> None:
        kind = self.kind.currentData()
        self.second.setVisible(kind in ("size", "date"))
        for field in (self.first, self.second):
            field.setToolTip(PATTERN_HINTS[kind])
        if kind == "size":
            self.first.setPlaceholderText("at least, e.g. 1MB")
            self.second.setPlaceholderText("at most, e.g. 50MB")
        elif kind == "date":
            self.first.setPlaceholderText("from, e.g. 2025-01-01")
            self.second.setPlaceholderText("to, e.g. 2025-12-31")
        else:
            self.first.setPlaceholderText(PATTERN_HINTS[kind].split("e.g. ")[-1])

    def value(self) -> dict:
        """The condition as entered (checked when the rule is saved)."""
        kind = self.kind.currentData()
        first, second = self.first.text().strip(), self.second.text().strip()
        if kind == "size":
            return {"kind": kind, "min": first or None, "max": second or None}
        if kind == "date":
            return {"kind": kind, "from": first or None, "to": second or None}
        return {"kind": kind, "pattern": first}


def _size_value(size) -> str:
    """A stored size in bytes as a value the field accepts again ("1.5MB")."""
    if size is None:
        return ""
    value, unit = float(size), "B"
    for next_unit in ("KB", "MB", "GB"):
        if value < 1024:
            break
        value, unit = value / 1024, next_unit
    return f"{value:g}{unit}"


class RuleDialog(QDialog):
    """Create or edit one rule: its conditions and what it does."""

    def __init__(self, folders: list[str], rule: RuleRecord | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit rule" if rule else "New rule")
        self.name = QLineEdit(rule.name if rule else "")
        self.match = QComboBox()
        self.match.addItem("all of these are true", False)
        self.match.addItem("any of these is true", True)
        if rule is not None and rule.match_any:
            self.match.setCurrentIndex(1)
        self.rows: list[ConditionRow] = []
        self.rows_layout = QVBoxLayout()
        add_condition = QPushButton("Add a condition")
        add_condition.clicked.connect(lambda: self.add_condition(None))
        for condition in rule.conditions if rule else [{"kind": "name", "pattern": ""}]:
            self.add_condition(condition)

        self.target = QComboBox()
        self.target.addItem(DONT_MOVE, "")
        for folder in folders:
            self.target.addItem(folder, folder)
        if rule is not None and rule.target_folder:
            self.target.setCurrentIndex(max(0, self.target.findData(rule.target_folder)))
        self.tags = QLineEdit(", ".join(rule.tags) if rule else "")
        self.tags.setPlaceholderText("e.g. Taxes, 2025")
        self.rename = QLineEdit(rule.rename_template if rule else "")
        self.rename.setPlaceholderText("e.g. {date} {title}")
        fields = " ".join("{" + field + "}" for field in TEMPLATE_FIELDS)
        self.rename.setToolTip(f"Leave empty to keep the name. Fields: {fields}")
        self.enabled = QCheckBox("Rule is on")
        self.enabled.setChecked(rule.enabled if rule else True)

        when_box = QGroupBox("When")
        when = QVBoxLayout(when_box)
        when.addWidget(self.match)
        when.addLayout(self.rows_layout)
        when.addWidget(add_condition, 0, Qt.AlignmentFlag.AlignLeft)
        then_box = QGroupBox("Then")
        then = QFormLayout(then_box)
        then.addRow("Move it to", self.target)
        then.addRow("Add tags", self.tags)
        then.addRow("Rename it to", self.rename)
        note = QLabel(f"Fields for the new name: {fields}. The extension is kept.")
        note.setStyleSheet("color: palette(placeholder-text);")
        note.setWordWrap(True)
        then.addRow("", note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout()
        form.addRow("Name", self.name)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(when_box)
        layout.addWidget(then_box)
        layout.addWidget(self.enabled)
        layout.addWidget(buttons)
        self.resize(560, self.sizeHint().height())

    def add_condition(self, condition: dict | None) -> ConditionRow:
        """Add a condition row (one can be removed while more than one is left)."""
        row = ConditionRow(condition)
        row.remove.clicked.connect(lambda: self._remove_condition(row))
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        return row

    def _remove_condition(self, row: ConditionRow) -> None:
        if len(self.rows) <= 1:
            return
        self.rows.remove(row)
        self.rows_layout.removeWidget(row)
        row.deleteLater()

    def values(self) -> dict:
        """What was entered, as ``AppService.add_rule`` keyword arguments plus the name."""
        tags = [tag.strip() for tag in self.tags.text().split(",") if tag.strip()]
        return {
            "name": self.name.text(),
            "conditions": [row.value() for row in self.rows],
            "match_any": bool(self.match.currentData()),
            "target": self.target.currentData() or "",
            "tags": tags,
            "rename_template": self.rename.text(),
            "enabled": self.enabled.isChecked(),
        }


class RulesView(QWidget):
    """Lists rules in checking order; add, edit, reorder, switch off, delete and test.
    Below them: rules suggested from the user's choices."""

    changed = Signal(str)  # a message after rules changed

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._rules: list[RuleRecord] = []
        self._suggestions: list[RuleSuggestion] = []

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
        self.status = plain_label()
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

        self.suggestions = QListWidget()
        self.suggestions.setMaximumHeight(100)
        self.make_button = QPushButton("Make this a rule")
        self.make_button.clicked.connect(self.make_selected_suggestion)
        self.dismiss_button = QPushButton("Don't suggest it")
        self.dismiss_button.clicked.connect(self.dismiss_selected_suggestion)
        suggestion_buttons = QVBoxLayout()
        suggestion_buttons.addWidget(self.make_button)
        suggestion_buttons.addWidget(self.dismiss_button)
        suggestion_buttons.addStretch(1)
        suggestion_row = QHBoxLayout()
        suggestion_row.addWidget(self.suggestions, 1)
        suggestion_row.addLayout(suggestion_buttons)
        self.suggestions_box = QGroupBox("Suggested from your choices")
        self.suggestions_box.setLayout(suggestion_row)
        self.suggestions_box.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(help_text)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.status)
        layout.addLayout(buttons)
        layout.addWidget(self.suggestions_box)
        self._update_buttons()

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the rules and the suggestions in the background."""
        self.runner.run(self.service.rules, self._show)
        self.runner.run(self.service.rule_suggestions, self._show_suggestions, lambda exc: None)

    def _show(self, rules: list[RuleRecord]) -> None:
        selected = self._selected()
        self._rules = rules
        self.table.blockSignals(True)
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            on.setCheckState(Qt.CheckState.Checked if rule.enabled else Qt.CheckState.Unchecked)
            when = wording.conditions(rule)
            then = wording.rule_actions(rule.target_folder, rule.tags, rule.rename_template)
            cells = (on, *(QTableWidgetItem(text) for text in (rule.name, when, then)))
            cells[2].setToolTip(plain_tooltip(when))
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

    def _show_suggestions(self, suggestions: list[RuleSuggestion]) -> None:
        self._suggestions = list(suggestions)
        self.suggestions.clear()
        for item in self._suggestions:
            entry = QListWidgetItem(wording.rule_suggestion(item))
            entry.setToolTip(plain_tooltip("\n".join(item.files)))
            self.suggestions.addItem(entry)
        if self._suggestions:
            self.suggestions.setCurrentRow(0)
        self.suggestions_box.setVisible(bool(self._suggestions))

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
        dialog = RuleDialog([folder.key for folder in folders], rule, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        name = values.pop("name")
        if rule is None:
            self.runner.run(
                lambda: self.service.add_rule(name, **values),
                self._after(f"Added the rule {name!r}"),
                self._failed,
            )
            return
        updated = replace(
            rule,
            name=name,
            conditions=tuple(values["conditions"]),
            match_any=values["match_any"],
            target_folder=values["target"],
            tags=tuple(values["tags"]),
            rename_template=values["rename_template"],
            enabled=values["enabled"],
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

    # --- suggestions ----------------------------------------------------------------

    def _selected_suggestion(self) -> RuleSuggestion | None:
        row = self.suggestions.currentRow()
        return self._suggestions[row] if 0 <= row < len(self._suggestions) else None

    def make_selected_suggestion(self) -> None:
        """Turn the selected suggestion into a rule."""
        item = self._selected_suggestion()
        if item is not None:
            self.runner.run(
                lambda: self.service.add_suggested_rule(item),
                self._after(f"Added the rule {item.name!r}"),
                self._failed,
            )

    def dismiss_selected_suggestion(self) -> None:
        """Don't suggest the selected rule again."""
        item = self._selected_suggestion()
        if item is not None:
            self.runner.run(lambda: self.service.dismiss_rule_suggestion(item.key), self._after(""))

    # --- testing ------------------------------------------------------------------

    def test_file(self) -> None:
        """Pick any file and show which rule would handle it (the file is only read)."""
        path, _ = QFileDialog.getOpenFileName(self, "Test the rules on a file")
        if path:
            self.runner.run(lambda: self.service.test_rules(Path(path)), self._show_test)

    def _show_test(self, result: RuleTest) -> None:
        self.status.setText(f"{result.path.name}: {wording.rule_test(result)}")
