"""Dialogs of the Library's editing: choosing a folder to move to, and reviewing the files
that might belong in a new folder (nothing moves until the user ticks them)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QListWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, FluentIcon, ListWidget, TreeWidget

from autoshelf.features.resort import Candidate, ResortPlan

KEY_ROLE = Qt.ItemDataRole.UserRole


def _leaf(key: str) -> str:
    return key.rpartition("/")[2]


class FolderPicker(QDialog):
    """Pick one of your folders (or the top level, when allowed) to move things into."""

    def __init__(
        self,
        root_name: str,
        keys: list[str],
        *,
        allow_root: bool,
        exclude: tuple[str, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move to")
        self.resize(360, 420)
        self.tree = TreeWidget()
        self.tree.setHeaderHidden(True)
        root = QTreeWidgetItem([root_name])
        root.setIcon(0, FluentIcon.HOME.icon())
        root.setData(0, KEY_ROLE, "")
        if not allow_root:
            root.setFlags(Qt.ItemFlag.ItemIsEnabled)  # shown, not selectable
            root.setToolTip(0, "Files can't go here: AutoShelf would sort them again")
        self.tree.addTopLevelItem(root)
        items = {"": root}
        folded = tuple(key.casefold() for key in exclude)
        for key in keys:  # sorted: a folder comes before the ones inside it
            item = QTreeWidgetItem(items.get(key.rpartition("/")[0], root), [_leaf(key)])
            item.setIcon(0, FluentIcon.FOLDER.icon())
            item.setData(0, KEY_ROLE, key)
            low = key.casefold()
            if any(low == other or low.startswith(other + "/") for other in folded):
                item.setDisabled(True)  # a folder can't go into itself
            items[key] = item
        root.setExpanded(True)
        self.tree.itemDoubleClicked.connect(lambda *_: self._accept_if_chosen())
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept_if_chosen)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(BodyLabel("Move to which folder?"))
        layout.addWidget(self.tree, 1)
        layout.addWidget(buttons)

    def chosen(self) -> str | None:
        """The key of the selected folder ("" for the top level), or None."""
        item = self.tree.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            return None
        return item.data(0, KEY_ROLE)

    def _accept_if_chosen(self) -> None:
        if self.chosen() is not None:
            self.accept()


class ReviewDialog(QDialog):
    """Files that might belong in a subfolder: tick the ones to move."""

    def __init__(self, plan: ResortPlan, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("These might belong in a new folder")
        self.resize(460, 380)
        where = plan.parent.replace("/", " / ")
        intro = BodyLabel(
            f"Their names don't say so, but their content looks like it. Tick the ones to "
            f"move out of {where}; the others stay where they are."
        )
        intro.setWordWrap(True)
        self.list = ListWidget()
        for candidate in plan.maybes:
            score = f" ({candidate.score:.0%} match)" if candidate.score is not None else ""
            item = QListWidgetItem(f"{candidate.path.name}  →  {_leaf(candidate.target)}{score}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setToolTip(f"{candidate.path}\nto {candidate.target.replace('/', ' / ')}")
            self.list.addItem(item)
        buttons = QDialogButtonBox()
        self.move_button = buttons.addButton("Move", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.list, 1)
        layout.addWidget(buttons)

    def chosen(self) -> list[Candidate]:
        """The ticked files."""
        return [
            candidate
            for row, candidate in enumerate(self.plan.maybes)
            if self.list.item(row).checkState() == Qt.CheckState.Checked
        ]
