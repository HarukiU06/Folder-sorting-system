"""The "Tidy up" screen: exact duplicates, possible versions, and organizing a folder
(SPEC sections 5.6 and 5.7). Nothing is ever deleted."""

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf import wording
from autoshelf.app_service import AppService
from autoshelf.features.duplicates import DuplicateGroup, VersionGroup
from autoshelf.features.organize import OrganizeResult, Proposal
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import plain_tooltip, show_in_explorer

PATH_ROLE = Qt.ItemDataRole.UserRole
ROOT_CHOICE = "The root (files not sorted yet)"
OTHER_CHOICE = "Another folder..."
LEAVE = "(leave it)"


def _path_item(path: Path, root: Path | None) -> QTreeWidgetItem:
    try:
        shown = path.relative_to(root).as_posix() if root else str(path)
    except ValueError:
        shown = str(path)
    item = QTreeWidgetItem([shown])
    item.setData(0, PATH_ROLE, str(path))
    item.setToolTip(0, str(path))
    return item


class DuplicatesPage(QWidget):
    """Groups of identical files; keep one, set the others aside in _Duplicates."""

    changed = Signal(str)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.status = QLabel(
            "Identical files (same content). Select the copy to keep: the others are moved "
            "to _Duplicates, where you can delete them yourself. Nothing is deleted."
        )
        self.status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        scan = QPushButton("Look for duplicates")
        scan.clicked.connect(self.refresh)
        self.keep_button = QPushButton("Keep the selected copy, set the others aside")
        self.keep_button.clicked.connect(self.keep_selected)
        show = QPushButton("Show in Explorer")
        show.clicked.connect(self._show)
        buttons = QHBoxLayout()
        buttons.addWidget(scan)
        buttons.addStretch(1)
        buttons.addWidget(show)
        buttons.addWidget(self.keep_button)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)
        self._groups: list[DuplicateGroup] = []

    def refresh(self) -> None:
        """Scan for duplicates in the background."""
        self.status.setText("Looking for identical files...")
        self.runner.run(self.service.duplicates, self._show_groups, self._failed)

    def _failed(self, exc: Exception) -> None:
        self.status.setText(str(exc))

    def _show_groups(self, groups: list[DuplicateGroup]) -> None:
        self._groups = groups
        self.tree.clear()
        root = self.service.root()
        for group in groups:
            parent = QTreeWidgetItem([f"{len(group.paths)} identical files ({group.size:,} bytes)"])
            for path in group.paths:
                parent.addChild(_path_item(path, root))
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.status.setText(
            f"{len(groups)} group(s) of identical files. Select the copy to keep."
            if groups
            else "No identical files found."
        )

    def _selected_path(self) -> tuple[Path, list[Path]] | None:
        item = self.tree.currentItem()
        if item is None or item.parent() is None:
            return None
        keep = Path(item.data(0, PATH_ROLE))
        siblings = [
            Path(item.parent().child(i).data(0, PATH_ROLE))
            for i in range(item.parent().childCount())
        ]
        return keep, [path for path in siblings if path != keep]

    def keep_selected(self) -> None:
        """Keep the selected copy and move its identical copies to _Duplicates."""
        selected = self._selected_path()
        if selected is None:
            QMessageBox.information(self, "Choose a copy", "Select the copy you want to keep.")
            return
        keep, others = selected
        names = "\n".join(path.name for path in others)
        answer = QMessageBox.question(
            self,
            "Set copies aside",
            f"Keep {keep.name} and move these to _Duplicates?\n\n{names}\n\n"
            "You can undo this in History.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.runner.run(
            lambda: self.service.keep_one(keep, others),
            lambda moved: (
                self.changed.emit(f"Moved {len(moved)} copies to _Duplicates"),
                self.refresh(),
            ),
            lambda exc: QMessageBox.warning(self, "Couldn't move the copies", str(exc)),
        )

    def _show(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, PATH_ROLE):
            show_in_explorer(Path(item.data(0, PATH_ROLE)))


class VersionsPage(QWidget):
    """Files that look like versions of one document (only shown)."""

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.status = QLabel(
            "Files with very similar content and similar names, e.g. report_v2 and report "
            'final, or iCloud conflict copies like "notes 2.pdf". Only shown; nothing moves.'
        )
        self.status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        scan = QPushButton("Look for versions")
        scan.clicked.connect(self.refresh)
        show = QPushButton("Show in Explorer")
        show.clicked.connect(self._show)
        buttons = QHBoxLayout()
        buttons.addWidget(scan)
        buttons.addStretch(1)
        buttons.addWidget(show)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        """Look for versions in the background."""
        self.status.setText("Comparing your files...")
        self.runner.run(
            self.service.versions, self._show_groups, lambda exc: self.status.setText(str(exc))
        )

    def _show_groups(self, groups: list[VersionGroup]) -> None:
        self.tree.clear()
        root = self.service.root()
        for group in groups:
            parent = QTreeWidgetItem(
                [f"{len(group.paths)} files, similarity {group.similarity:.2f}"]
            )
            for path in group.paths:
                parent.addChild(_path_item(path, root))
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.status.setText(
            f"{len(groups)} group(s) of possible versions."
            if groups
            else "No possible versions found."
        )

    def _show(self) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, PATH_ROLE):
            show_in_explorer(Path(item.data(0, PATH_ROLE)))


FILES_CHOICE = "Files chosen in Explorer"


def sort_plan_note(plan) -> str:
    """What else to say about files chosen in Explorer: already in place, or refused."""
    lines = []
    if plan.in_place:
        names = ", ".join(path.name for path in plan.in_place[:3])
        more = f" and {len(plan.in_place) - 3} more" if len(plan.in_place) > 3 else ""
        lines.append(f"Already where it belongs: {names}{more}.")
    lines.extend(reason for _, reason in plan.refused)
    return "\n".join(lines)


class OrganizePage(QWidget):
    """Preview where every file of a folder would go, then apply as one undoable batch."""

    changed = Signal(str)
    COLUMNS = ("", "File", "Now in", "Move to", "Score", "Why")

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._proposals: list[Proposal] = []
        self._folder: Path | None = None
        self._files: list[Path] | None = None  # files chosen in Explorer, instead of a folder
        self._folders: list[str] = []
        self._last_batch: str | None = None

        self.source = QComboBox()
        self.source.setMinimumContentsLength(28)
        self.recursive = QCheckBox("Include subfolders")
        preview = QPushButton("Preview")
        preview.clicked.connect(self.preview)
        top = QHBoxLayout()
        top.addWidget(QLabel("Organize:"))
        top.addWidget(self.source, 1)
        top.addWidget(self.recursive)
        top.addWidget(preview)
        self.status = QLabel(
            "Choose a folder and click Preview. Nothing moves until you click Apply; confident "
            "proposals are ticked, guesses are not. You can change any target."
        )
        self.status.setWordWrap(True)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        self.apply_button = QPushButton("Apply ticked moves")
        self.apply_button.clicked.connect(self.apply)
        self.undo_button = QPushButton("Undo this batch")
        self.undo_button.clicked.connect(self.undo_batch)
        self.undo_button.setEnabled(False)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.undo_button)
        buttons.addWidget(self.apply_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        self._update_buttons()

    def refresh(self) -> None:
        """Reload the folder choices."""
        self.runner.run(self.service.folders, self._fill_sources, lambda exc: None)

    def _fill_sources(self, folders) -> None:
        self._folders = [folder.key for folder in folders]
        current = self.source.currentText()
        self.source.clear()
        self.source.addItem(ROOT_CHOICE)
        self.source.addItem("_Inbox")
        self.source.addItems(self._folders)
        self.source.addItem(OTHER_CHOICE)
        if self._files:
            self.source.insertItem(0, FILES_CHOICE)
        if current and self.source.findText(current) >= 0:
            self.source.setCurrentText(current)

    def _chosen_folder(self) -> Path | None:
        root = self.service.root()
        choice = self.source.currentText()
        if root is None:
            return None
        if choice == ROOT_CHOICE:
            return root
        if choice == OTHER_CHOICE:
            picked = QFileDialog.getExistingDirectory(self, "Choose a folder to organize")
            return Path(picked) if picked else None
        return root.joinpath(*choice.split("/"))

    def preview(self) -> None:
        """Propose a folder for every file (in the background)."""
        if self.source.currentText() == FILES_CHOICE and self._files:
            self.preview_files(self._files)
            return
        folder = self._chosen_folder()
        if folder is None:
            return
        recursive = self.recursive.isChecked()
        self.status.setText(f"Reading the files in {folder}...")
        self.table.setRowCount(0)

        def planned(proposals: list[Proposal]) -> None:
            self._folder, self._files = folder, None
            self._show(proposals)

        self.runner.run(
            lambda: self.service.plan_organize(folder, recursive=recursive),
            planned,
            lambda exc: self.status.setText(str(exc)),
        )

    def preview_files(self, paths: list[Path]) -> None:
        """Propose a folder for files chosen in Explorer ("Sort with AutoShelf")."""
        self.status.setText(f"Reading {len(paths)} file(s)...")
        self.table.setRowCount(0)
        self._proposals = []
        self._update_buttons()

        self._folder, self._files = None, list(paths)
        if self.source.findText(FILES_CHOICE) < 0:
            self.source.insertItem(0, FILES_CHOICE)
        self.source.setCurrentText(FILES_CHOICE)

        def planned(plan) -> None:
            self._show(plan.proposals, sort_plan_note(plan))

        def failed(exc: Exception) -> None:
            self.status.setText(str(exc))
            QMessageBox.warning(self, "Can't sort these files", str(exc))

        self.runner.run(lambda: self.service.plan_sort(paths), planned, failed)

    def _show(self, proposals: list[Proposal], note: str = "") -> None:
        self._proposals = proposals
        self.table.setRowCount(len(proposals))
        for row, proposal in enumerate(proposals):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            tick.setCheckState(
                Qt.CheckState.Checked if proposal.selected else Qt.CheckState.Unchecked
            )
            self.table.setItem(row, 0, tick)
            name = QTableWidgetItem(proposal.path.name)
            name.setToolTip(str(proposal.path))
            self.table.setItem(row, 1, name)
            # A folder outside the root is shown by its name (the whole path as a tooltip).
            current = Path(proposal.current)
            now_in = QTableWidgetItem(current.name if current.is_absolute() else proposal.current)
            now_in.setToolTip(proposal.current)
            self.table.setItem(row, 2, now_in)
            target = QComboBox()
            target.addItem(LEAVE)
            target.addItems(self._folders)
            if proposal.target:
                target.setCurrentText(proposal.target)
            self.table.setCellWidget(row, 3, target)
            score = "" if proposal.confidence is None else f"{proposal.confidence:.2f}"
            self.table.setItem(row, 4, QTableWidgetItem(score))
            text = wording.reason_text(proposal.why)
            why = QTableWidgetItem(text)
            why.setToolTip(plain_tooltip(text))
            self.table.setItem(row, 5, why)
        ticked = sum(proposal.selected for proposal in proposals)
        self.status.setText(
            (
                f"{len(proposals)} file(s) could move; {ticked} ticked. Nothing has moved yet."
                if proposals
                else "Everything there is already where it belongs."
            )
            + (f"\n{note}" if note else "")
        )
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.apply_button.setEnabled(bool(self._proposals))
        self.undo_button.setEnabled(self._last_batch is not None)

    def _collect(self) -> list[Proposal]:
        for row, proposal in enumerate(self._proposals):
            proposal.selected = self.table.item(row, 0).checkState() == Qt.CheckState.Checked
            choice = self.table.cellWidget(row, 3).currentText()
            proposal.target = None if choice == LEAVE else choice
        return self._proposals

    def apply(self) -> None:
        """Move the ticked rows as one batch."""
        if self._folder is None and self._files is None:
            return
        proposals = self._collect()
        count = sum(1 for item in proposals if item.selected and item.target)
        if not count:
            self.status.setText("Nothing is ticked.")
            return
        folder = self._folder
        if folder is None:
            work = lambda: self.service.apply_sort(proposals)  # noqa: E731
        else:
            work = lambda: self.service.apply_organize(folder, proposals)  # noqa: E731

        def done(result: OrganizeResult) -> None:
            self._last_batch = result.batch_id if result.moved else None
            failed = "".join(f"\n{path.name}: {error}" for path, error in result.failed)
            self._proposals = []
            self.table.setRowCount(0)
            self.status.setText(
                f"Moved {len(result.moved)} file(s) as one batch. Undo it any time here or in "
                "History." + (f"\nCouldn't move:{failed}" if failed else "")
            )
            self.changed.emit(f"Organized {len(result.moved)} file(s)")
            self._update_buttons()

        self.runner.run(
            work, done, lambda exc: QMessageBox.warning(self, "Couldn't organize", str(exc))
        )

    def undo_batch(self) -> None:
        """Undo the last applied batch."""
        batch = self._last_batch
        if batch is None:
            return

        def done(results) -> None:
            self._last_batch = None
            self.status.setText(f"Undid {len(results)} move(s).")
            self.changed.emit(f"Undid {len(results)} move(s)")
            self._update_buttons()

        self.runner.run(
            lambda: self.service.undo_batch(batch),
            done,
            lambda exc: QMessageBox.warning(self, "Couldn't undo", str(exc)),
        )


class RenamePage(QWidget):
    """Files with meaningless names, with a better name to approve (SPEC section 5.2)."""

    changed = Signal(str)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._proposals = []
        self.status = QLabel(
            "Files with names like scan_0034 or document(3), and a name taken from their "
            "title or first heading. Nothing is renamed until you approve; renames can be undone."
        )
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Now called", "Rename to", "From its"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        scan = QPushButton("Look for meaningless names")
        scan.clicked.connect(self.refresh)
        self.ai_button = QPushButton("Ask the AI for a name")
        self.ai_button.clicked.connect(self.ask_ai)
        self.ai_button.setVisible(False)
        rename = QPushButton("Rename selected")
        rename.clicked.connect(self.rename_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(scan)
        buttons.addStretch(1)
        buttons.addWidget(self.ai_button)
        buttons.addWidget(rename)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        """Look for junk names (uses what AutoShelf already read; no AI)."""
        self.runner.run(
            self.service.rename_suggestions, self._show, lambda exc: self.status.setText(str(exc))
        )

    def _show(self, proposals) -> None:
        self._proposals = list(proposals)
        self.table.setRowCount(len(self._proposals))
        for row, proposal in enumerate(self._proposals):
            name = QTableWidgetItem(proposal.path.name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(str(proposal.path))
            source = QTableWidgetItem(proposal.source)
            source.setFlags(source.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(proposal.new_name))  # editable
            self.table.setItem(row, 2, source)
        self.status.setText(
            f"{len(self._proposals)} file(s) could have a better name. Edit a name if you "
            "like, select rows, then Rename selected."
            if self._proposals
            else "No meaningless names found (or no title or heading to use)."
        )

    def _selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def ask_ai(self) -> None:
        """Let the AI propose names for the selected files."""
        for row in self._selected_rows():
            path = self._proposals[row].path

            def done(proposal, row=row) -> None:
                if proposal is not None:
                    self.table.item(row, 1).setText(proposal.new_name)
                    self.table.item(row, 2).setText("AI")

            self.runner.run(
                lambda path=path: self.service.propose_name(path, use_ai=True),
                done,
                lambda exc: self.status.setText(str(exc)),
            )

    def rename_selected(self) -> None:
        """Rename the selected files to the names shown."""
        jobs = [
            (self._proposals[row].path, self.table.item(row, 1).text().strip())
            for row in self._selected_rows()
        ]
        if not jobs:
            self.status.setText("Select the files to rename first.")
            return

        def run():
            return [self.service.rename(path, name) for path, name in jobs if name]

        self.runner.run(
            run,
            lambda results: (self.changed.emit(f"Renamed {len(results)} file(s)"), self.refresh()),
            lambda exc: QMessageBox.warning(self, "Couldn't rename", str(exc)),
        )


class NewFoldersPage(QWidget):
    """Topics in the Inbox that no folder covers yet (SPEC section 5.8)."""

    changed = Signal(str)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._suggestions = []
        self.status = QLabel(
            "Groups of files waiting in the Inbox that look alike but fit none of your "
            "folders. AutoShelf only creates a folder when you click Create."
        )
        self.status.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        scan = QPushButton("Look for new topics")
        scan.clicked.connect(self.refresh)
        create = QPushButton("Create folder and move these files...")
        create.clicked.connect(self.create_selected)
        buttons = QHBoxLayout()
        buttons.addWidget(scan)
        buttons.addStretch(1)
        buttons.addWidget(create)
        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)

    def refresh(self) -> None:
        """Group the waiting files in the background."""
        self.status.setText("Looking at the Inbox...")
        self.runner.run(
            self.service.suggest_folders, self._show, lambda exc: self.status.setText(str(exc))
        )

    def _show(self, suggestions) -> None:
        self._suggestions = list(suggestions)
        self.tree.clear()
        for number, item in enumerate(self._suggestions):
            words = f"  (words: {', '.join(item.keywords)})" if item.keywords else ""
            parent = QTreeWidgetItem([f'{len(item.paths)} files look like "{item.name}"{words}'])
            parent.setData(0, PATH_ROLE, number)
            for path in item.paths:
                parent.addChild(QTreeWidgetItem([path.name]))
            self.tree.addTopLevelItem(parent)
            parent.setExpanded(True)
        self.status.setText(
            f"{len(self._suggestions)} possible new folder(s). Select one and click Create."
            if self._suggestions
            else "No new topics: every group of waiting files fits one of your folders."
        )

    def create_selected(self) -> None:
        """Ask for the folder's name, create it, and move the group's files into it."""
        item = self.tree.currentItem()
        if item is None:
            return
        top = item if item.parent() is None else item.parent()
        suggestion = self._suggestions[top.data(0, PATH_ROLE)]
        name, ok = QInputDialog.getText(
            self,
            "Create a folder",
            f"Create this folder and move the {len(suggestion.paths)} files into it?",
            text=suggestion.name,
        )
        if not ok or not name.strip():
            return
        self.runner.run(
            lambda: self.service.create_folder(name.strip(), list(suggestion.paths)),
            lambda result: (
                self.changed.emit(f"Created {result[0]} and moved {len(result[1])} file(s)"),
                self.refresh(),
            ),
            lambda exc: QMessageBox.warning(self, "Couldn't create the folder", str(exc)),
        )
