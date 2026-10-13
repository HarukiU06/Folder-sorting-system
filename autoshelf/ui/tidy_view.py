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
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService
from autoshelf.features.duplicates import DuplicateGroup, VersionGroup
from autoshelf.features.organize import OrganizeResult, Proposal
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import show_in_explorer

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
        folder = self._chosen_folder()
        if folder is None:
            return
        recursive = self.recursive.isChecked()
        self.status.setText(f"Reading the files in {folder}...")
        self.table.setRowCount(0)

        def planned(proposals: list[Proposal]) -> None:
            self._folder = folder
            self._show(proposals)

        self.runner.run(
            lambda: self.service.plan_organize(folder, recursive=recursive),
            planned,
            lambda exc: self.status.setText(str(exc)),
        )

    def _show(self, proposals: list[Proposal]) -> None:
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
            self.table.setItem(row, 2, QTableWidgetItem(proposal.current))
            target = QComboBox()
            target.addItem(LEAVE)
            target.addItems(self._folders)
            if proposal.target:
                target.setCurrentText(proposal.target)
            self.table.setCellWidget(row, 3, target)
            score = "" if proposal.confidence is None else f"{proposal.confidence:.2f}"
            self.table.setItem(row, 4, QTableWidgetItem(score))
            why = QTableWidgetItem(
                f"{proposal.reason}: {proposal.explanation}"
                if proposal.reason.startswith("Rule")
                else proposal.explanation
            )
            why.setToolTip(proposal.explanation)
            self.table.setItem(row, 5, why)
        ticked = sum(proposal.selected for proposal in proposals)
        self.status.setText(
            f"{len(proposals)} file(s) could move; {ticked} ticked. Nothing has moved yet."
            if proposals
            else "Everything there is already where it belongs."
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
        if self._folder is None:
            return
        proposals = self._collect()
        count = sum(1 for item in proposals if item.selected and item.target)
        if not count:
            self.status.setText("Nothing is ticked.")
            return
        folder = self._folder

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
            lambda: self.service.apply_organize(folder, proposals),
            done,
            lambda exc: QMessageBox.warning(self, "Couldn't organize", str(exc)),
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


class TidyView(QWidget):
    """Duplicates, possible versions and organizing, as three pages."""

    changed = Signal(str)

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.duplicates = DuplicatesPage(service, runner)
        self.versions = VersionsPage(service, runner)
        self.organize = OrganizePage(service, runner)
        self.pages = QTabWidget()
        self.pages.addTab(self.organize, "Organize a folder")
        self.pages.addTab(self.duplicates, "Duplicates")
        self.pages.addTab(self.versions, "Possible versions")
        for page in (self.duplicates, self.organize):
            page.changed.connect(self.changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pages)

    def refresh(self) -> None:
        """Reload what is cheap to reload (scans run when asked)."""
        self.organize.refresh()
