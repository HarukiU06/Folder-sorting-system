"""The Library screen, laid out like File Explorer (SPEC sections 5.1, 5.3, 5.4).

Your folders as a tree on the left (your tags below them), Back / Forward / Up and the
path above, the open folder's folders and files in the middle, and on the right the
details of the selected file (tags, related files, compress / convert) or folder (its
description, which helps AutoShelf recognise what belongs there). A search by meaning or
by exact words lists the matching folders and files instead.

It browses what one scan found (names, sizes and dates: no file is read, and nothing is
downloaded from iCloud), so it shows your folders and the files in them, not the files
still waiting to be sorted, nor hidden or ignored ones.

Things can be changed as in Explorer: drag files and folders onto a folder (or Move to...,
or Ctrl+X and Ctrl+V), make a folder, rename, and delete, which moves things into ``_Trash``
in your AutoShelf folder (nothing is ever deleted). Every change can be undone. A new folder
makes AutoShelf sort the loose files of the folder it is in again: files whose names match
move into it, and the ones that might belong are offered for review.
"""

import html
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMimeData, QModelIndex, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDrag, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QListWidgetItem,
    QMessageBox,
    QSplitter,
    QTableWidgetItem,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    BreadcrumbBar,
    CaptionLabel,
    FluentIcon,
    LineEdit,
    ListWidget,
    PlainTextEdit,
    PushButton,
    SearchLineEdit,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TableWidget,
    TransparentToggleToolButton,
    TransparentToolButton,
    TreeWidget,
)

from autoshelf import wording
from autoshelf.app_service import AppService, FileInfo, FolderInfo, Shelf
from autoshelf.features import convert
from autoshelf.features.resort import Candidate, ResortPlan, ResortResult
from autoshelf.ml.library import LibraryFile, folder_label
from autoshelf.ui.folder_dialogs import FolderPicker, ReviewDialog
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import (
    menu_at,
    open_folder,
    plain,
    plain_tooltip,
    show_in_explorer,
    show_menu,
)

PATH_ROLE = Qt.ItemDataRole.UserRole  # a file's path or a folder's key
KIND_ROLE = Qt.ItemDataRole.UserRole + 1
SORT_ROLE = Qt.ItemDataRole.UserRole + 2

FOLDER = "folder"
FILE = "file"
TAG = "tag"
SEARCH = "search"
type Location = tuple[str, str]  # (FOLDER, key) with "" for your AutoShelf folder, ...
ROOT: Location = (FOLDER, "")

COLUMNS = ("Name", "", "Date modified", "Size", "Folder")  # "": the status (a cloud icon)
NAME, STATUS, MODIFIED, SIZE, WHERE = range(len(COLUMNS))
COLUMN_WIDTHS = {STATUS: 32, MODIFIED: 110, SIZE: 80, WHERE: 150}
HINT = (
    'Search by meaning ("paper explaining k-means initialization") or by exact words '
    '("invoice 2025"). Folders whose names match are listed first.'
)
DESCRIBE_HINT = "What belongs in this folder? A few words help AutoShelf sort new files."
ONLINE_ONLY = "Online only: it is in iCloud, and opening it downloads it"
MIME = "application/x-autoshelf-items"  # dragged rows: a JSON list of [kind, value]
TREE_WIDTH = 220
DETAIL_WIDTH = 270
SEARCH_WIDTH = 280

_KINDS: dict[str, tuple[FluentIcon, str]] = {}
for _icon, _name, _suffixes in (
    (FluentIcon.DOCUMENT, "PDF document", "pdf"),
    (FluentIcon.DOCUMENT, "Word document", "doc docx"),
    (FluentIcon.DOCUMENT, "Document", "odt rtf pages"),
    (FluentIcon.DOCUMENT, "Text document", "txt log"),
    (FluentIcon.DOCUMENT, "Markdown document", "md"),
    (FluentIcon.DOCUMENT, "Spreadsheet", "xls xlsx xlsm ods numbers"),
    (FluentIcon.DOCUMENT, "CSV file", "csv tsv"),
    (FluentIcon.DOCUMENT, "Presentation", "ppt pptx odp key"),
    (FluentIcon.BOOK_SHELF, "E-book", "epub mobi"),
    (FluentIcon.GLOBE, "Web page", "html htm"),
    (FluentIcon.PHOTO, "Image", "jpg jpeg png gif bmp tif tiff webp heic heif svg"),
    (FluentIcon.MUSIC, "Audio", "mp3 m4a wav flac ogg aac"),
    (FluentIcon.VIDEO, "Video", "mp4 mov avi mkv webm m4v"),
    (FluentIcon.ZIP_FOLDER, "Compressed archive", "zip 7z rar tar gz"),
    (FluentIcon.CODE, "Code", "py js ts json xml yaml yml c cpp h java ipynb sql"),
):
    for _suffix in _suffixes.split():
        _KINDS[_suffix] = (_icon, _name)


def file_kind(name: str) -> tuple[FluentIcon, str]:
    """An icon and an Explorer-like type ("PDF document") from a file's extension only.

    The extension is enough, and asking Windows for a file's own icon could make iCloud
    download it."""
    suffix = Path(name).suffix.lower().lstrip(".")
    return _KINDS.get(suffix, (FluentIcon.DOCUMENT, f"{suffix.upper()} file" if suffix else "File"))


def _size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


def short_size(size: int) -> str:
    """A size as Explorer's list shows it: "1 KB", "820 KB", "12.5 MB"."""
    if size < 1024 * 1024:
        return f"{max(1, round(size / 1024)):,} KB" if size else "0 KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}{'' if number == 1 else 's'}"


def _parent(key: str) -> str:
    return key.rpartition("/")[0]


def _leaf(key: str) -> str:
    return key.rpartition("/")[2]


@dataclass(frozen=True)
class Row:
    """One line of the middle list: a folder or a file."""

    kind: str  # FOLDER or FILE
    value: str  # the folder's key or the file's path
    name: str
    where: str = ""  # the folder it is in (search and tag results)
    size: int | None = None  # bytes, or the number of items in a folder
    modified: float | None = None
    online_only: bool = False
    tip: str = ""


class _Item(QTableWidgetItem):
    """A cell that sorts by SORT_ROLE, so sizes and dates sort as numbers and folders
    stay together."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if mine is None or theirs is None:
            return super().__lt__(other)
        return mine < theirs


def _items(mime: QMimeData) -> list[tuple[str, str]]:
    """The rows a drag carries."""
    if not mime.hasFormat(MIME):
        return []
    try:
        return [(str(kind), str(value)) for kind, value in json.loads(bytes(mime.data(MIME)))]
    except (ValueError, TypeError):
        return []


class _ItemsTable(TableWidget):
    """The middle list: its rows can be dragged onto folders, here or in the tree."""

    def __init__(self, view: "LibraryView") -> None:
        super().__init__()
        self._view = view
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)

    def startDrag(self, actions) -> None:
        items = self._view.selected_items()
        if items:
            drag = QDrag(self)
            drag.setMimeData(self._view.mime_for(items))
            drag.exec(Qt.DropAction.MoveAction)  # the rows themselves stay until a rescan

    def _target(self, event) -> str | None:
        cell = self.itemAt(event.position().toPoint())
        if cell is None or cell.data(KIND_ROLE) != FOLDER:
            return None
        return cell.data(PATH_ROLE)

    def dragEnterEvent(self, event) -> None:
        event.acceptProposedAction() if event.mimeData().hasFormat(MIME) else event.ignore()

    def dragMoveEvent(self, event) -> None:
        key = self._target(event)
        if key is not None and self._view.can_drop(event.mimeData(), key):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        key = self._target(event)
        if key is not None:
            self._view.drop_mime(event.mimeData(), key)
        event.setDropAction(Qt.DropAction.IgnoreAction)  # nothing to remove from the list
        event.accept()


class _FolderTree(TreeWidget):
    """The folder tree: rows from the list can be dropped on its folders."""

    def __init__(self, view: "LibraryView") -> None:
        super().__init__()
        self._view = view
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def _target(self, event) -> str | None:
        item = self.itemAt(event.position().toPoint())
        if item is None or item.data(0, KIND_ROLE) != FOLDER:
            return None
        return item.data(0, PATH_ROLE)

    def dragEnterEvent(self, event) -> None:
        event.acceptProposedAction() if event.mimeData().hasFormat(MIME) else event.ignore()

    def dragMoveEvent(self, event) -> None:
        key = self._target(event)
        if key is not None and self._view.can_drop(event.mimeData(), key):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        key = self._target(event)
        if key is not None:
            self._view.drop_mime(event.mimeData(), key)
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()


class LibraryView(QWidget):
    """Your folders like File Explorer shows them, with search, tags and file details."""

    changed = Signal(str)  # a message after tags changed or a file was made or renamed
    saved = Signal(str)  # a message after a folder's description was saved
    edited = Signal(str)  # a message after files or folders were moved, made or deleted

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.ai_enabled = False
        self.location: Location = ROOT
        self._shelf: Shelf | None = None
        self._folders: dict[str, FolderInfo] = {}
        self._items: dict[str, int] = {}  # folder key -> folders and files directly in it
        self._files: dict[str, LibraryFile] = {}  # casefolded path -> file
        self._tags: list[tuple[str, int]] = []
        self._tree_items: dict[str, QTreeWidgetItem] = {}
        self.tags_item: QTreeWidgetItem | None = None
        self._back: list[Location] = []
        self._forward: list[Location] = []
        self._last_folder: Location = ROOT
        self._filled: Location | None = None  # what the middle list shows
        self._select_after_load: Path | None = None
        self._note = ""
        self._info: FileInfo | None = None
        self._folder: FolderInfo | None = None  # the folder in the details pane
        self._folder_key: str | None = None
        self._search_number = 0
        self._syncing = False
        self._clipboard: list[tuple[str, str]] = []  # cut, waiting to be pasted
        self._undo_ids: list[int] = []  # the last change, for its Undo button
        self.pending_review: ResortPlan | None = None  # files that might belong somewhere

        bar = self._build_bar()
        self.tree = _FolderTree(self)
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(1)
        self.tree.setMinimumWidth(160)
        self.tree.currentItemChanged.connect(self._tree_changed)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        self.results = self._build_results()
        self.detail = self._build_detail()

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        for widget in (self.tree, self.results, self.detail):
            self.splitter.addWidget(widget)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([TREE_WIDTH, 500, DETAIL_WIDTH])

        self.summary = plain(CaptionLabel())  # the status line, as at the foot of Explorer
        self.summary.setWordWrap(True)
        self.undo_button = PushButton(FluentIcon.RETURN, "Undo")
        self.undo_button.setToolTip("Undo the last change (it is also in History)")
        self.undo_button.clicked.connect(self.undo_last_change)
        self.undo_button.setVisible(False)
        self.review_button = PushButton(FluentIcon.VIEW, "Review")
        self.review_button.setToolTip("Files that might belong in the new folder")
        self.review_button.clicked.connect(self.review)
        self.review_button.setVisible(False)
        status = QHBoxLayout()
        status.addWidget(self.summary, 1)
        status.addWidget(self.review_button)
        status.addWidget(self.undo_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 12)
        layout.setSpacing(10)
        layout.addLayout(bar)
        layout.addWidget(self.splitter, 1)
        layout.addLayout(status)
        self._add_shortcuts()
        self._show_detail(None)
        self._update_bar()

    # --- building -----------------------------------------------------------------------

    def _build_bar(self) -> QHBoxLayout:
        self.back_button = TransparentToolButton(FluentIcon.LEFT_ARROW)
        self.back_button.setToolTip("Back (Alt+Left)")
        self.back_button.clicked.connect(self.go_back)
        self.forward_button = TransparentToolButton(FluentIcon.RIGHT_ARROW)
        self.forward_button.setToolTip("Forward (Alt+Right)")
        self.forward_button.clicked.connect(self.go_forward)
        self.up_button = TransparentToolButton(FluentIcon.UP)
        self.up_button.setToolTip("Up to the folder it is in (Alt+Up)")
        self.up_button.clicked.connect(self.go_up)
        self.refresh_button = TransparentToolButton(FluentIcon.SYNC)
        self.refresh_button.setToolTip("Refresh (F5)")
        self.refresh_button.clicked.connect(self.refresh)
        self.new_folder_button = TransparentToolButton(FluentIcon.FOLDER_ADD)
        self.new_folder_button.setToolTip("New folder (Ctrl+Shift+N)")
        self.new_folder_button.clicked.connect(self.new_folder)

        self.path_bar = BreadcrumbBar()
        self.path_bar.currentItemChanged.connect(self._crumb_clicked)
        address = SimpleCardWidget()  # the address box
        address_layout = QHBoxLayout(address)
        address_layout.setContentsMargins(12, 4, 12, 4)
        address_layout.addWidget(self.path_bar)

        self.query = SearchLineEdit()
        self.query.setPlaceholderText("Search your files")
        self.query.setToolTip(HINT)
        self.query.setFixedWidth(SEARCH_WIDTH)
        self.query.returnPressed.connect(self.search)
        self.query.searchSignal.connect(lambda _: self.search())
        self.query.clearSignal.connect(self._search_cleared)

        self.details_button = TransparentToggleToolButton(FluentIcon.INFO)
        self.details_button.setToolTip("Details pane")
        self.details_button.setChecked(True)
        self.details_button.toggled.connect(lambda shown: self.detail.setVisible(shown))

        bar = QHBoxLayout()
        bar.setSpacing(4)
        for button in (self.back_button, self.forward_button, self.up_button):
            bar.addWidget(button)
        bar.addWidget(address, 1)
        bar.addWidget(self.refresh_button)
        bar.addWidget(self.new_folder_button)
        bar.addSpacing(8)
        bar.addWidget(self.query)
        bar.addWidget(self.details_button)
        return bar

    def _build_results(self) -> TableWidget:
        table = _ItemsTable(self)
        table.setColumnCount(len(COLUMNS))
        table.setHorizontalHeaderLabels(COLUMNS)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.setMinimumWidth(320)
        table.horizontalHeaderItem(STATUS).setToolTip("Status: a cloud means online only")
        header = table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        for column, width in COLUMN_WIDTHS.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(column, width)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        table.setSortingEnabled(True)
        table.itemSelectionChanged.connect(self._selection_changed)
        table.itemDoubleClicked.connect(lambda _: self.open_selected())
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._context_menu)
        return table

    def _build_detail(self) -> QWidget:
        detail = QWidget()
        detail.setMinimumWidth(240)
        self.folder_box = self._build_folder_box()
        self.file_box = self._build_file_box()
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.folder_box, 1)
        layout.addWidget(self.file_box, 1)
        return detail

    def _build_folder_box(self) -> QWidget:
        box = SimpleCardWidget()
        self.folder_name = plain(SubtitleLabel())
        self.folder_name.setWordWrap(True)
        self.folder_facts = plain(BodyLabel())
        self.folder_facts.setWordWrap(True)
        self.describe_label = StrongBodyLabel("What belongs here")
        self.description = LineEdit()
        self.description.setPlaceholderText(DESCRIBE_HINT)
        self.description.setToolTip(DESCRIBE_HINT)
        self.description.editingFinished.connect(self._describe)
        self.describe_note = CaptionLabel(
            "A few words about this folder help AutoShelf recognise files that belong in it."
        )
        self.describe_note.setWordWrap(True)
        open_button = PushButton(FluentIcon.FOLDER, "Open in Explorer")
        open_button.clicked.connect(self._open_folder)
        self.study_button = PushButton(FluentIcon.BOOK_SHELF, "Study…")
        self.study_button.setToolTip("A reading list and flashcards for this folder (AI)")
        self.study_button.clicked.connect(self.study_folder)
        self.study_button.setVisible(False)
        self.resort_button = PushButton(FluentIcon.SYNC, "Sort into subfolders")
        self.resort_button.setToolTip(
            "Move the files lying in this folder into its subfolders: files whose names match "
            "move; others that might belong are offered"
        )
        self.resort_button.clicked.connect(
            lambda: self._folder_key is not None and self.resort_folder(self._folder_key)
        )
        self.resort_button.setVisible(False)
        buttons = QHBoxLayout()
        buttons.addWidget(open_button)
        buttons.addWidget(self.study_button)
        buttons.addStretch(1)
        layout_more = QHBoxLayout()
        layout_more.addWidget(self.resort_button)
        layout_more.addStretch(1)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(self.folder_name)
        layout.addWidget(self.folder_facts)
        layout.addSpacing(6)
        layout.addWidget(self.describe_label)
        layout.addWidget(self.description)
        layout.addWidget(self.describe_note)
        layout.addSpacing(6)
        layout.addLayout(buttons)
        layout.addLayout(layout_more)
        layout.addStretch(1)
        box.setVisible(False)
        return box

    def _build_file_box(self) -> QWidget:
        box = SimpleCardWidget()
        self.name = plain(SubtitleLabel())
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.menu_button = TransparentToolButton(FluentIcon.MORE)
        self.menu_button.setToolTip("More actions")
        self.menu_button.clicked.connect(lambda: show_menu(self.menu_button, self.menu_actions()))
        heading = QHBoxLayout()
        heading.addWidget(self.name, 1)
        heading.addWidget(self.menu_button, 0, Qt.AlignmentFlag.AlignTop)
        self.facts = BodyLabel()
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.summary_text = plain(BodyLabel())
        self.summary_text.setWordWrap(True)

        self.open_action = Action(FluentIcon.DOCUMENT, "Open", self)
        self.open_action.triggered.connect(self.open_file)
        self.show_action = Action(FluentIcon.FOLDER, "Show in Explorer", self)
        self.show_action.triggered.connect(self._show_in_explorer)
        self.rename_action = Action(FluentIcon.EDIT, "Rename…", self)
        self.rename_action.triggered.connect(self.rename_file)
        self.move_action = Action(FluentIcon.MOVE, "Move to…", self)
        self.move_action.triggered.connect(self.move_selected)
        self.cut_action = Action(FluentIcon.CUT, "Cut", self)
        self.cut_action.triggered.connect(self.cut)
        self.delete_action = Action(FluentIcon.DELETE, "Delete", self)
        self.delete_action.setToolTip("Move it to the Trash (in your AutoShelf folder)")
        self.delete_action.triggered.connect(self.delete_selected)
        self.compress_action = Action(FluentIcon.ZIP_FOLDER, "Make a smaller copy", self)
        self.compress_action.setToolTip(
            "A smaller copy (a PDF or an image) or a ZIP next to the file; "
            "the file itself stays as it is"
        )
        self.compress_action.triggered.connect(self.compress_file)
        self.convert_actions: list[Action] = []
        self.summarize_action = Action(FluentIcon.ROBOT, "Summarize", self)
        self.summarize_action.triggered.connect(self.summarize)

        self.file_tags = ListWidget()
        self.file_tags.setMaximumHeight(90)
        self.new_tag = LineEdit()
        self.new_tag.setPlaceholderText("Add a tag")
        self.new_tag.returnPressed.connect(self._add_typed_tag)
        add = PushButton("Add")
        add.clicked.connect(self._add_typed_tag)
        remove = PushButton("Remove")
        remove.clicked.connect(self._remove_tag)
        tag_row = QHBoxLayout()
        tag_row.addWidget(self.new_tag, 1)
        tag_row.addWidget(add)
        tag_row.addWidget(remove)
        self.suggested = QHBoxLayout()
        self.suggested_buttons: list[PushButton] = []

        self.related = ListWidget()
        self.related.itemDoubleClicked.connect(self._open_related)
        self.preview = PlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("No text preview.")

        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addLayout(heading)
        layout.addWidget(self.facts)
        layout.addWidget(self.summary_text)
        layout.addWidget(StrongBodyLabel("Tags"))
        layout.addWidget(self.file_tags)
        layout.addLayout(tag_row)
        layout.addLayout(self.suggested)
        layout.addWidget(StrongBodyLabel("Related files (double-click to show)"))
        layout.addWidget(self.related, 1)
        layout.addWidget(StrongBodyLabel("Preview"))
        layout.addWidget(self.preview, 1)
        return box

    def _add_shortcuts(self) -> None:
        for keys, slot in (
            ("Alt+Left", self.go_back),
            ("Backspace", self.go_back),
            ("Alt+Right", self.go_forward),
            ("Alt+Up", self.go_up),
            ("F5", self.refresh),
            ("Ctrl+F", self.query.setFocus),
            ("F3", self.query.setFocus),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(slot)
        enter = QShortcut(QKeySequence("Return"), self.results)
        enter.setContext(Qt.ShortcutContext.WidgetShortcut)
        enter.activated.connect(self.open_selected)
        new_folder = QShortcut(QKeySequence("Ctrl+Shift+N"), self)
        new_folder.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        new_folder.activated.connect(self.new_folder)
        # Only in the list and the tree, so Delete in the search box deletes nothing.
        for keys, slot in (
            ("Delete", self.delete_selected),
            ("F2", self.rename_selected),
            ("Ctrl+X", self.cut),
            ("Ctrl+V", self.paste),
        ):
            shortcut = QShortcut(QKeySequence(keys), self.results)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(slot)
        for keys, slot in (
            ("Delete", lambda: self._on_tree_folder(self._delete_items)),
            ("F2", lambda: self._on_tree_folder(lambda items: self.rename_folder(items[0][1]))),
        ):
            shortcut = QShortcut(QKeySequence(keys), self.tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(slot)

    def menu_actions(self) -> list[Action | None]:
        """What the ⋯ menu (and a right-click) offers for the selected file."""
        if len(self.selected_items()) > 1:
            return [self.move_action, self.cut_action, self.delete_action]
        actions: list[Action | None] = [self.open_action, self.show_action, self.rename_action]
        actions += [self.move_action, self.cut_action, self.delete_action]
        actions += [None, self.compress_action, *self.convert_actions]
        if self.ai_enabled:
            actions += [None, self.summarize_action]
        return actions

    # --- loading ------------------------------------------------------------------------

    def refresh(self) -> None:
        """Rescan your folders (names, sizes, dates) and reload the tags."""
        self.runner.run(self.service.shelf, self._loaded, self._failed)
        self.runner.run(self.service.tags, self._show_tags)

    def _loaded(self, shelf: Shelf) -> None:
        self._shelf = shelf
        self._folders = {folder.key: folder for folder in shelf.folders}
        self._items = {folder.key: folder.files for folder in shelf.folders}
        self._items[""] = 0
        for folder in shelf.folders:
            self._items[_parent(folder.key)] += 1
        self._files = {str(item.path).casefold(): item for item in shelf.files}
        self._build_tree()
        kind, value = self.location
        if kind == FOLDER:
            while value and value not in self._folders:  # it was renamed or removed
                value = _parent(value)
            self.location = (FOLDER, value)
            self._show_location()
        elif kind == TAG:
            self._show_location()
        else:
            self._update_bar()
        self._select_after_load = None
        self._note = ""

    def _show_tags(self, tags: list[tuple[str, int]]) -> None:
        self._tags = tags
        self._fill_tags()

    def _build_tree(self) -> None:
        shelf = self._shelf
        if shelf is None:
            return
        self._syncing = True
        expanded = {key for key, item in self._tree_items.items() if item.isExpanded()}
        first = not self._tree_items
        self.tree.clear()
        folder_icon = FluentIcon.FOLDER.icon()
        root = QTreeWidgetItem([shelf.root.name])
        root.setIcon(0, FluentIcon.HOME.icon())
        root.setData(0, KIND_ROLE, FOLDER)
        root.setData(0, PATH_ROLE, "")
        root.setToolTip(0, plain_tooltip(f"Your AutoShelf folder\n{shelf.root}"))
        self.tree.addTopLevelItem(root)
        self._tree_items = {"": root}
        for folder in shelf.folders:  # sorted, so a folder comes before the ones inside it
            parent = self._tree_items.get(_parent(folder.key), root)
            item = QTreeWidgetItem(parent, [_leaf(folder.key)])
            item.setIcon(0, folder_icon)
            item.setData(0, KIND_ROLE, FOLDER)
            item.setData(0, PATH_ROLE, folder.key)
            tip = f"{folder.key.replace('/', ' / ')}: {_count(folder.files, 'file')}"
            item.setToolTip(0, plain_tooltip(f"{tip}\n{folder.description}".strip()))
            self._tree_items[folder.key] = item
        self.tags_item = QTreeWidgetItem(["Tags"])
        self.tags_item.setIcon(0, FluentIcon.TAG.icon())
        self.tags_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        self.tree.addTopLevelItem(self.tags_item)
        for key, item in self._tree_items.items():
            item.setExpanded(key in expanded or (first and key == ""))
        self.tags_item.setExpanded(True)
        self._fill_tags()
        self._syncing = False
        self._sync_tree()

    def _fill_tags(self) -> None:
        if self.tags_item is None:
            return
        self._syncing = True
        self.tags_item.takeChildren()
        tag_icon = FluentIcon.TAG.icon()
        for name, count in self._tags:
            item = QTreeWidgetItem(self.tags_item, [name])
            item.setIcon(0, tag_icon)
            item.setData(0, KIND_ROLE, TAG)
            item.setData(0, PATH_ROLE, name)
            item.setToolTip(0, _count(count, "file"))
        if not self._tags:
            empty = QTreeWidgetItem(self.tags_item, ["(no tags yet)"])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
        self._syncing = False
        self._sync_tree()

    def tag_names(self) -> list[str]:
        """The tags under Tags in the tree."""
        if self.tags_item is None:
            return []
        items = (self.tags_item.child(i) for i in range(self.tags_item.childCount()))
        return [item.data(0, PATH_ROLE) for item in items if item.data(0, KIND_ROLE) == TAG]

    def tree_children(self, key: str) -> list[str]:
        """The folders under ``key`` ("" for your AutoShelf folder) in the tree."""
        item = self._tree_items.get(key)
        if item is None:
            return []
        return [item.child(i).data(0, PATH_ROLE) for i in range(item.childCount())]

    # --- moving around ------------------------------------------------------------------

    def go_to(self, key: str) -> None:
        """Open one of your folders ("" for your AutoShelf folder itself)."""
        self._navigate((FOLDER, key))

    def go_up(self) -> None:
        """Go to the folder the open folder is in (from search results: back to the folder)."""
        kind, value = self.location
        if kind != FOLDER:
            self._navigate(self._last_folder)
        elif value:
            self._navigate((FOLDER, _parent(value)))

    def go_back(self) -> None:
        """Go back to where you were before."""
        if self._back:
            self._forward.append(self.location)
            self._set_location(self._back.pop())

    def go_forward(self) -> None:
        """Undo a Back."""
        if self._forward:
            self._back.append(self.location)
            self._set_location(self._forward.pop())

    def show_tag(self, name: str) -> None:
        """List the files with a tag."""
        self._navigate((TAG, name))

    def show_file(self, path: Path, note: str = "") -> None:
        """Open the folder a file is in, with the file selected (after a rescan, so a file
        just made is there too)."""
        self._select_after_load = path
        self._note = note
        self._navigate((FOLDER, self._key_of(path)))
        self.refresh()

    def search(self) -> None:
        """Search for the typed query in the background."""
        query = self.query.text().strip()
        if query:
            self._navigate((SEARCH, query))

    def _search_cleared(self) -> None:
        if self.location[0] == SEARCH:
            self._navigate(self._last_folder)

    def _navigate(self, location: Location) -> None:
        if location != self.location:
            self._back.append(self.location)
            self._forward.clear()
        self._set_location(location)

    def _set_location(self, location: Location) -> None:
        if location != self.location:
            self.undo_button.setVisible(False)
        self.location = location
        if location[0] == FOLDER:
            self._last_folder = location
        self._show_location()

    def _show_location(self) -> None:
        self._search_number += 1  # a search or tag list still under way no longer shows
        kind, value = self.location
        if kind == FOLDER:
            self._show_folder_contents(value)
        elif kind == TAG:
            self._load_tag(value)
        else:
            self._run_search(value)
        self._update_bar()

    def _crumb_clicked(self, route: str) -> None:
        if route.startswith(f"{FOLDER}:"):
            self.go_to(route.partition(":")[2])

    def _tree_changed(self, current: QTreeWidgetItem | None, _previous) -> None:
        if self._syncing or current is None:
            return
        kind, value = current.data(0, KIND_ROLE), current.data(0, PATH_ROLE)
        if kind == FOLDER:
            self.go_to(value)
        elif kind == TAG:
            self.show_tag(value)

    def _update_bar(self) -> None:
        kind, value = self.location
        self.back_button.setEnabled(bool(self._back))
        self.forward_button.setEnabled(bool(self._forward))
        self.up_button.setEnabled(self.location != ROOT)
        self.path_bar.blockSignals(True)
        self.path_bar.clear()
        self.path_bar.addItem(f"{FOLDER}:", self._shelf.root.name if self._shelf else "AutoShelf")
        if kind == FOLDER and value:
            parts = value.split("/")
            for end in range(1, len(parts) + 1):
                self.path_bar.addItem(f"{FOLDER}:{'/'.join(parts[:end])}", parts[end - 1])
        elif kind == SEARCH:
            self.path_bar.addItem(SEARCH, f"Search results for “{value}”")
        elif kind == TAG:
            self.path_bar.addItem(TAG, f"Tagged “{value}”")
        self.path_bar.blockSignals(False)
        if kind != SEARCH and self.query.text():
            self.query.blockSignals(True)
            self.query.clear()
            self.query.blockSignals(False)
        self._sync_tree()

    def _sync_tree(self) -> None:
        """Select the open folder or tag in the tree."""
        kind, value = self.location
        item = None
        if kind == FOLDER:
            item = self._tree_items.get(value)
        elif kind == TAG and self.tags_item is not None:
            children = (self.tags_item.child(i) for i in range(self.tags_item.childCount()))
            item = next((c for c in children if c.data(0, PATH_ROLE) == value), None)
        self._syncing = True
        if item is None:
            self.tree.clearSelection()
            self.tree.setCurrentIndex(QModelIndex())
        else:
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        self._syncing = False

    # --- what the middle list shows -----------------------------------------------------

    def _show_folder_contents(self, key: str) -> None:
        if self._shelf is None:
            self._fill([])
            self.summary.setText("Looking at your folders...")
            return
        folders = [
            self._folder_row(folder.key)
            for folder in self._shelf.folders
            if folder.key != key and _parent(folder.key) == key
        ]
        files = sorted(
            (item for item in self._shelf.files if item.folder == key),
            key=lambda item: item.path.name.casefold(),
        )
        self._fill(
            folders + [self._file_row(item.path, item.folder) for item in files],
            by_name=True,
            select=self._select_after_load,
        )
        if folders or files:
            parts = [_count(len(folders), "folder")] if folders else []
            parts += [_count(len(files), "file")] if files else []
            text = ", ".join(parts)
        elif key:
            text = "This folder is empty."
        else:
            text = (
                "No folders yet. Make one here (New folder), in Explorer or on your iPhone, and "
                "AutoShelf sorts new files into them."
            )
        self.summary.setText(self._note or text)

    def _load_tag(self, name: str) -> None:
        number = self._search_number
        self.summary.setText(f"Looking for files tagged “{name}”...")

        def found(paths: list[Path]) -> None:
            if number != self._search_number:
                return
            self._fill([self._file_row(path, self._key_of(path)) for path in paths], by_name=True)
            self.summary.setText(f"{_count(len(paths), 'file')} tagged “{name}”")

        self.runner.run(lambda: self.service.tagged(name), found, self._failed)

    def _run_search(self, query: str) -> None:
        number = self._search_number
        if self.query.text().strip() != query:  # Back or Forward to a search
            self.query.blockSignals(True)
            self.query.setText(query)
            self.query.blockSignals(False)
        folders = [self._folder_row(key, where=True) for key in self.matching_folders(query)]
        self._fill(folders)
        self.summary.setText(f"Searching for “{query}”... (the first search loads the model)")

        def found(hits) -> None:
            if number != self._search_number:
                return  # a newer search is under way
            files = [
                self._file_row(
                    hit.path,
                    hit.folder,
                    ("[has the words] " if hit.keyword else "")
                    + (f"Summary: {hit.summary}" if hit.summary else hit.snippet),
                )
                for hit in hits
            ]
            self._fill(folders + files, select_first_file=True)
            parts = [_count(len(folders), "folder")] if folders else []
            parts += [_count(len(files), "file")] if files or not folders else []
            self.summary.setText(
                f"{' and '.join(parts)} for “{query}”"
                if folders or files
                else f"Nothing found for “{query}”."
            )

        self.runner.run(lambda: self.service.search(query), found, self._failed)

    def matching_folders(self, query: str) -> list[str]:
        """Your folders whose names contain every word of ``query``."""
        words = query.casefold().split()
        return [
            key
            for key in self._folders
            if all(word in f"{key} {folder_label(key)}".casefold() for word in words)
        ]

    def _folder_row(self, key: str, *, where: bool = False) -> Row:
        folder = self._folders.get(key)
        tip = key.replace("/", " / ")
        if folder is not None and folder.description:
            tip += f"\n{folder.description}"
        return Row(
            FOLDER,
            key,
            _leaf(key),
            where=self._place(_parent(key)) if where else "",
            size=self._items.get(key, 0),
            tip=tip,
        )

    def _file_row(self, path: Path, folder: str, note: str = "") -> Row:
        item = self._files.get(str(path).casefold())
        return Row(
            FILE,
            str(path),
            path.name,
            where=self._place(folder),
            size=item.size if item else None,
            modified=item.mtime if item else None,
            online_only=item.online_only if item else False,
            tip=f"{path}\n{note}" if note else str(path),
        )

    def _place(self, key: str) -> str:
        if not key:
            return self._shelf.root.name if self._shelf else "your AutoShelf folder"
        return key.replace("/", " / ")

    def _key_of(self, path: Path) -> str:
        if self._shelf is not None:
            try:
                return "/".join(path.parent.relative_to(self._shelf.root).parts)
            except ValueError:
                pass
        return path.parent.name

    def _fill(
        self,
        rows: list[Row],
        *,
        by_name: bool = False,
        select: Path | tuple[str, str] | None = None,
        select_first_file: bool = False,
    ) -> None:
        """Show ``rows``: folders first, sorted by name (``by_name``), or in the order given
        (search results, best first). Keeps the selection when the list is only refreshed."""
        kept = self.selected_item() if self._filled == self.location else None
        self._filled = self.location
        table = self.results
        table.setSortingEnabled(False)
        table.blockSignals(True)
        table.clearSelection()
        table.setRowCount(len(rows))
        for number, row in enumerate(rows):
            for column, cell in enumerate(self._cells(row)):
                table.setItem(number, column, cell)
        table.setColumnHidden(WHERE, self.location[0] == FOLDER)
        header = table.horizontalHeader()
        header.setSortIndicator(NAME if by_name else -1, Qt.SortOrder.AscendingOrder)
        table.setSortingEnabled(True)
        table.blockSignals(False)

        wanted: tuple[str, str] | None = kept
        if isinstance(select, tuple):
            wanted = select
        elif select is not None:
            wanted = (FILE, str(select))
        elif select_first_file:
            wanted = next(((FILE, row.value) for row in rows if row.kind == FILE), None)
        if wanted is None or not self.select(*wanted):
            self._selection_changed()

    def _cells(self, row: Row) -> list[QTableWidgetItem]:
        group = 0 if row.kind == FOLDER else 1
        if row.kind == FOLDER:
            icon, kind = FluentIcon.FOLDER, "File folder"
            size = _count(row.size or 0, "item")
        else:
            icon, kind = file_kind(row.name)
            size = short_size(row.size) if row.size is not None else ""
        modified = (
            datetime.fromtimestamp(row.modified).strftime("%Y-%m-%d")
            if row.modified is not None
            else ""
        )
        texts = {
            NAME: (row.name, row.name.casefold()),
            STATUS: ("", row.online_only),
            MODIFIED: (modified, row.modified or 0.0),
            SIZE: (size, row.size or 0),
            WHERE: (row.where, row.where.casefold()),
        }
        cells = []
        for column in range(len(COLUMNS)):
            text, key = texts[column]
            cell = _Item(text)
            cell.setData(SORT_ROLE, (group, key))
            cell.setData(PATH_ROLE, row.value)
            cell.setData(KIND_ROLE, row.kind)
            cell.setToolTip(plain_tooltip(f"{kind}\n{row.tip}"))
            cells.append(cell)
        cells[NAME].setIcon(icon.icon())
        if row.online_only:
            cells[STATUS].setIcon(FluentIcon.CLOUD.icon())
            cells[STATUS].setToolTip(ONLINE_ONLY)
        cells[SIZE].setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return cells

    def row_names(self) -> list[str]:
        """The names in the middle list, top to bottom."""
        return [self.results.item(row, NAME).text() for row in range(self.results.rowCount())]

    def selected_item(self) -> tuple[str, str] | None:
        """(FOLDER, key) or (FILE, path) of the selected row (the first, if several)."""
        items = self.selected_items()
        return items[0] if items else None

    def selected_items(self) -> list[tuple[str, str]]:
        """(FOLDER, key) or (FILE, path) of every selected row, top to bottom."""
        rows = sorted({index.row() for index in self.results.selectionModel().selectedRows()})
        items = []
        for row in rows:
            cell = self.results.item(row, NAME)
            if cell is not None:
                items.append((cell.data(KIND_ROLE), cell.data(PATH_ROLE)))
        return items

    def select(self, kind: str, value: str) -> bool:
        """Select the row of a folder (by key) or a file (by path)."""
        wanted = value.casefold() if kind == FILE else value
        for row in range(self.results.rowCount()):
            cell = self.results.item(row, NAME)
            mine = cell.data(PATH_ROLE)
            if (
                cell.data(KIND_ROLE) == kind
                and (mine.casefold() if kind == FILE else mine) == wanted
            ):
                self.results.selectRow(row)
                self.results.scrollToItem(cell)
                return True
        return False

    def open_selected(self) -> None:
        """Open the selected folder here, or the selected file with its usual program."""
        selected = self.selected_item()
        if selected is None:
            return
        kind, value = selected
        if kind == FOLDER:
            self.go_to(value)
        else:
            self.open_file()

    def _context_menu(self, pos: QPoint) -> None:
        cell = self.results.itemAt(pos)
        if cell is None:
            self.results.clearSelection()
            menu_at(self.results.viewport(), pos, self._background_actions())
            return
        if not self.results.item(cell.row(), NAME).isSelected():
            self.results.selectRow(cell.row())
        if len(self.selected_items()) == 1 and cell.data(KIND_ROLE) == FOLDER:
            actions = self._folder_actions(cell.data(PATH_ROLE))
        else:
            actions = self.menu_actions()
        menu_at(self.results.viewport(), pos, actions)

    def _failed(self, exc: Exception) -> None:
        self.summary.setText(str(exc))

    # --- the details pane ---------------------------------------------------------------

    def current_path(self) -> Path | None:
        """The selected file."""
        selected = self.selected_item()
        return Path(selected[1]) if selected and selected[0] == FILE else None

    def _selection_changed(self) -> None:
        if len(self.selected_items()) > 1:
            self._show_many(len(self.selected_items()))
            return
        selected = self.selected_item()
        if selected is not None and selected[0] == FOLDER:
            self._show_folder_details(selected[1])
            return
        path = self.current_path()
        if path is None:
            kind, value = self.location
            if kind == FOLDER:
                self._show_folder_details(value)
            else:
                self._show_detail(None)
            return
        self.runner.run(
            lambda: (
                self.service.file_info(path),
                self.service.related(path),
                self.service.conversions(path),
            ),
            lambda result: self._show_detail(*result) if self.current_path() == path else None,
            lambda exc: self.summary.setText(f"{path.name}: {exc}"),
        )

    def _show_folder_details(self, key: str) -> None:
        self.file_box.setVisible(False)
        self.folder_box.setVisible(True)
        self._info = None
        self._folder_key = key
        self._folder = self._folders.get(key) if key else None
        if not key:
            name = self._shelf.root.name if self._shelf else "Your AutoShelf folder"
            facts = (
                "Your AutoShelf folder. New files arrive here and are sorted into the folders "
                "inside it."
            )
        else:
            name = _leaf(key)
            files = self._folder.files if self._folder else 0
            folders = self._items.get(key, 0) - files
            where = f"In {self._place(_parent(key))} · "
            facts = where + (
                ", ".join(
                    ([_count(folders, "folder")] if folders else []) + [_count(files, "file")]
                )
            )
        self.folder_name.setText(name)
        self.folder_facts.setText(facts)
        has_description = bool(key)
        for widget in (self.describe_label, self.description, self.describe_note):
            widget.setVisible(has_description)
        self.description.setText(self._folder.description if self._folder else "")
        self.study_button.setVisible(self.ai_enabled and has_description)
        self.resort_button.setVisible(bool(key) and self._has_subfolders(key))

    def _describe(self) -> None:
        folder = self._folder
        text = self.description.text().strip()
        if folder is None or text == folder.description:
            return
        key = folder.key
        self._folder = FolderInfo(key, folder.files, text)
        self._folders[key] = self._folder
        self.runner.run(
            lambda: self.service.describe(key, text),
            lambda _: (self.saved.emit(f"Saved the description of {key}"), self.refresh()),
            self._failed,
        )

    def _open_folder(self) -> None:
        if self._folder_key is not None:
            self._open_in_explorer(self._folder_key)

    def _open_in_explorer(self, key: str) -> None:
        if self._shelf is not None:
            open_folder(self._shelf.root.joinpath(*key.split("/")) if key else self._shelf.root)

    def study_folder(self) -> None:
        """Open study mode (reading list, flashcards) for the folder in the details pane."""
        from autoshelf.ui.study_dialog import StudyDialog

        if self._folder_key:
            StudyDialog(self.service, self.runner, self._folder_key, self).exec()

    def _show_detail(self, info: FileInfo | None, related=(), conversions=()) -> None:
        self.folder_box.setVisible(False)
        self.file_box.setVisible(True)
        self._info = info
        self.file_box.setEnabled(info is not None)
        self._fill_conversions(conversions)
        if info is None:
            self.name.setText("Select a file")
            for widget in (self.facts, self.summary_text):
                widget.setText("")
            self.file_tags.clear()
            self.related.clear()
            self.preview.clear()
            self._show_suggestions([])
            return
        self.name.setText(info.path.name)
        modified = datetime.fromtimestamp(info.modified).strftime("%Y-%m-%d %H:%M")
        where = self._place(info.folder)
        title = f"<br>Title: {html.escape(info.title)}" if info.title else ""
        kind = file_kind(info.path.name)[1]
        self.facts.setText(
            f"{kind} &middot; {_size(info.size)}<br>In {html.escape(where)}"
            f"<br>Modified {modified}{title}"
        )
        self.summary_text.setText(f"Summary: {info.summary}" if info.summary else "")
        self.summary_text.setVisible(bool(info.summary))
        self.file_tags.clear()
        self.file_tags.addItems(info.tags)
        self._show_suggestions(info.tag_suggestions)
        self.related.clear()
        for item in related:
            entry = QListWidgetItem(f"{item.path.name}  ({wording.place(item.folder)})")
            entry.setData(PATH_ROLE, str(item.path))
            entry.setToolTip(f"{item.path}\nsimilarity {item.similarity:.2f}")
            self.related.addItem(entry)
        self.preview.setPlainText(info.excerpt or "")

    def _fill_conversions(self, formats) -> None:
        self.convert_actions = []
        for to in formats:
            action = Action(FluentIcon.SYNC, f"Convert to {convert.label(to)}", self)
            action.triggered.connect(lambda _=False, to=to: self.convert_file(to))
            self.convert_actions.append(action)

    def compress_file(self) -> None:
        """Make a smaller copy or a ZIP of the selected file, next to it (in the background)."""
        path = self.current_path()
        if path is not None:
            self._make(lambda: [self.service.compress([path])])

    def convert_file(self, to: str) -> None:
        """Make a copy of the selected file in format ``to``, next to it."""
        path = self.current_path()
        if path is not None:
            self._make(lambda: self.service.convert([path], to))

    def _make(self, work) -> None:
        self.summary.setText("Making the new file...")

        def done(created) -> None:
            names = ", ".join(item.dst.name for item in created)
            note = f"Made {names} next to the original (undo in History)."
            self.summary.setText(note)
            self.changed.emit(f"Made {names}")
            if created:
                self.show_file(created[-1].dst, note)

        def failed(exc: Exception) -> None:
            self.summary.setText(str(exc))
            QMessageBox.warning(self, "Couldn't make the file", str(exc))

        self.runner.run(work, done, failed)

    def set_ai_enabled(self, enabled: bool) -> None:
        """Offer the AI features (summaries, study mode) only when an AI provider is on."""
        self.ai_enabled = enabled
        self.study_button.setVisible(enabled and bool(self._folder_key))

    def summarize(self) -> None:
        """Ask the AI for a summary of the selected file (cached afterwards)."""
        info = self._info
        if info is None:
            return
        self.summary_text.setVisible(True)
        self.summary_text.setText("Summarizing...")
        self.runner.run(
            lambda: self.service.summarize(info.path),
            lambda text: self.summary_text.setText(f"Summary: {text}"),
            lambda exc: self.summary_text.setText(str(exc)),
        )

    def rename_file(self) -> None:
        """Propose a better name (title, heading, or the AI when on); rename if approved."""
        path = self.current_path()
        if path is None:
            return
        use_ai = self.ai_enabled

        def ask(proposal) -> None:
            current = proposal.new_name if proposal else path.name
            source = f" (from its {proposal.source})" if proposal else ""
            name, ok = QInputDialog.getText(
                self, "Rename", f"New name for {path.name}{source}:", text=current
            )
            if ok and name.strip() and name.strip() != path.name:
                self.runner.run(
                    lambda: self.service.rename(path, name.strip()),
                    renamed,
                    lambda exc: QMessageBox.warning(self, "Couldn't rename", str(exc)),
                )

        def renamed(result) -> None:
            note = f"Renamed to {result.dst.name} (undo in History)"
            self.changed.emit(note)
            self.show_file(result.dst, note)

        self.runner.run(
            lambda: self.service.propose_name(path, use_ai=use_ai), ask, lambda exc: ask(None)
        )

    def _show_suggestions(self, names: list[str]) -> None:
        for button in self.suggested_buttons:
            self.suggested.removeWidget(button)
            button.deleteLater()
        self.suggested_buttons = []
        for name in names:
            button = PushButton(f"+ {name}")
            button.setToolTip(f"Tag it '{name}' (it also fits that folder)")
            button.clicked.connect(lambda _=False, tag=name: self.add_tags([tag]))
            self.suggested.addWidget(button)
            self.suggested_buttons.append(button)

    def add_tags(self, names: list[str]) -> None:
        """Tag the selected file."""
        info = self._info
        if info is None or not names:
            return

        def done(added: list[str]) -> None:
            self.changed.emit(f"Tagged {info.path.name}: {', '.join(added)}" if added else "")
            self.refresh()
            self._selection_changed()

        self.runner.run(
            lambda: self.service.tag(info.path, names),
            done,
            lambda exc: QMessageBox.warning(self, "Couldn't add the tag", str(exc)),
        )

    def _add_typed_tag(self) -> None:
        text = self.new_tag.text().strip()
        if text:
            self.new_tag.clear()
            self.add_tags([text])

    def _remove_tag(self) -> None:
        info = self._info
        item = self.file_tags.currentItem()
        if info is None or item is None:
            return
        name = item.text()

        def done(_) -> None:
            self.changed.emit(f"Removed the tag {name!r} from {info.path.name}")
            self.refresh()
            self._selection_changed()

        self.runner.run(lambda: self.service.untag(info.path, name), done)

    def open_file(self) -> None:
        """Open the selected file with its usual program."""
        path = self.current_path()
        if path is not None:
            self.runner.run(lambda: self.service.mark_opened(path))
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _show_in_explorer(self) -> None:
        path = self.current_path()
        if path is not None:
            show_in_explorer(path)

    def _open_related(self, item: QListWidgetItem) -> None:
        name = self._info.path.name if self._info else ""
        self.show_file(Path(item.data(PATH_ROLE)), f"Related to {name}" if name else "")

    # --- changing things (as in Explorer; every change can be undone) -----------------------

    def _show_many(self, count: int) -> None:
        self._show_detail(None)
        self.name.setText(f"{count} items selected")

    def _has_subfolders(self, key: str) -> bool:
        return any(_parent(other) == key for other in self._folders if other != key)

    def _folder_actions(self, key: str) -> list[Action | None]:
        """What a right-click on a folder offers."""
        items = [(FOLDER, key)]

        def action(icon, text: str, slot) -> Action:
            item = Action(icon, text, self)
            item.triggered.connect(slot)
            return item

        actions: list[Action | None] = [
            action(FluentIcon.FOLDER, "Open", lambda: self.go_to(key)),
            action(FluentIcon.LINK, "Open in Explorer", lambda: self._open_in_explorer(key)),
            action(FluentIcon.EDIT, "Rename…", lambda: self.rename_folder(key)),
            action(FluentIcon.MOVE, "Move to…", lambda: self._ask_target(items)),
            action(FluentIcon.CUT, "Cut", lambda: self._cut(items)),
            action(FluentIcon.DELETE, "Delete", lambda: self._delete_items(items)),
        ]
        if self._clipboard:
            actions.append(action(FluentIcon.PASTE, "Paste into it", lambda: self.paste(key)))
        if self._has_subfolders(key):
            actions += [
                None,
                action(FluentIcon.SYNC, "Sort into subfolders", lambda: self.resort_folder(key)),
            ]
        return actions

    def _background_actions(self) -> list[Action | None]:
        """What a right-click on the list's empty space offers (inside a folder)."""
        if self.location[0] != FOLDER:
            return []
        new = Action(FluentIcon.FOLDER_ADD, "New folder", self)
        new.triggered.connect(self.new_folder)
        actions: list[Action | None] = [new]
        if self._clipboard:
            paste = Action(FluentIcon.PASTE, "Paste", self)
            paste.triggered.connect(lambda: self.paste())
            actions.append(paste)
        return actions

    def _tree_menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if item is None or item.data(0, KIND_ROLE) != FOLDER:
            return
        key = item.data(0, PATH_ROLE)
        if key:
            actions = self._folder_actions(key)
        else:
            self.go_to("")
            actions = self._background_actions()
        menu_at(self.tree.viewport(), pos, actions)

    def _on_tree_folder(self, act) -> None:
        item = self.tree.currentItem()
        if item is not None and item.data(0, KIND_ROLE) == FOLDER and item.data(0, PATH_ROLE):
            act([(FOLDER, item.data(0, PATH_ROLE))])

    def mime_for(self, items: list[tuple[str, str]]) -> QMimeData:
        """What dragging these rows carries."""
        mime = QMimeData()
        mime.setData(MIME, json.dumps([list(item) for item in items]).encode())
        return mime

    def can_drop(self, mime: QMimeData, key: str) -> bool:
        """True if the dragged rows may go into the folder ``key``: files never go into the
        root itself (they would be sorted again), and a folder never into itself."""
        items = _items(mime)
        if not items:
            return False
        for kind, value in items:
            if kind == FILE and (not key or self._key_of(Path(value)) == key):
                return False
            if kind == FOLDER and (
                key == value or key.startswith(value + "/") or key == _parent(value)
            ):
                return False
        return True

    def drop_mime(self, mime: QMimeData, key: str) -> None:
        """Move the dragged rows into the folder ``key``."""
        if self.can_drop(mime, key):
            self._move_items(_items(mime), key)

    def new_folder(self) -> None:
        """Make a folder inside the open folder (AutoShelf then sorts into it what belongs)."""
        kind, parent = self.location
        if kind != FOLDER:
            self.summary.setText("Open a folder to make a new folder in it.")
            return
        name, ok = QInputDialog.getText(
            self,
            "New folder",
            f"Name of the new folder in {self._place(parent)}:",
            text="New folder",
        )
        if not ok or not name.strip():
            return

        def made(key: str) -> None:
            self._select_after_load = (FOLDER, key)
            self.undo_button.setVisible(False)
            self._note = (
                f"Made {_leaf(key)}. Files in {self._place(parent)} whose names match it move "
                "into it in a moment."
            )
            self.edited.emit(f"Made the folder {_leaf(key)}")
            self.refresh()

        self.runner.run(
            lambda: self.service.make_folder(parent, name.strip()),
            made,
            self._failed_change("Couldn't make the folder"),
        )

    def rename_selected(self) -> None:
        """Rename the selected file or folder (F2)."""
        items = self.selected_items()
        if len(items) != 1:
            return
        kind, value = items[0]
        if kind == FOLDER:
            self.rename_folder(value)
        else:
            self.rename_file()

    def rename_folder(self, key: str) -> None:
        """Ask for a new name for a folder; its description and rules follow it."""
        name, ok = QInputDialog.getText(
            self, "Rename folder", f"New name for {_leaf(key)}:", text=_leaf(key)
        )
        if not ok or not name.strip() or name.strip() == _leaf(key):
            return

        def renamed(change) -> None:
            if change is None or self._shelf is None:
                return
            new_key = "/".join(change.dst.relative_to(self._shelf.root).parts)
            kind, value = self.location
            if kind == FOLDER and (value == key or value.startswith(key + "/")):
                self.location = (FOLDER, new_key + value[len(key) :])
            self._after_change(
                [change.history_id],
                f"Renamed {_leaf(key)} to {_leaf(new_key)}",
                select=(FOLDER, new_key),
            )

        self.runner.run(
            lambda: self.service.rename_folder(key, name.strip()),
            renamed,
            self._failed_change("Couldn't rename the folder"),
        )

    def move_selected(self) -> None:
        """Ask where to move the selected rows (Move to...)."""
        items = self.selected_items()
        if items:
            self._ask_target(items)

    def move_selected_to(self, key: str) -> None:
        """Move the selected rows into the folder ``key``."""
        items = self.selected_items()
        if items:
            self._move_items(items, key)

    def _ask_target(self, items: list[tuple[str, str]]) -> None:
        if self._shelf is None:
            return
        picker = FolderPicker(
            self._shelf.root.name,
            [folder.key for folder in self._shelf.folders],
            allow_root=all(kind == FOLDER for kind, _ in items),
            exclude=tuple(value for kind, value in items if kind == FOLDER),
            parent=self,
        )
        if picker.exec() == QDialog.DialogCode.Accepted and picker.chosen() is not None:
            self._move_items(items, picker.chosen())

    def _move_items(self, items: list[tuple[str, str]], key: str) -> None:
        files = [Path(value) for kind, value in items if kind == FILE]
        folders = [value for kind, value in items if kind == FOLDER]
        if not key and files:
            self.summary.setText(
                "Files can't go into your AutoShelf folder itself (AutoShelf would sort them "
                "again). Choose a folder."
            )
            return
        what = self._what(items)

        def moved(changes) -> None:
            self._after_change(
                changes.history_ids, f"Moved {what} to {self._place(key)}", changes.failed
            )

        self.runner.run(
            lambda: self.service.move_items(files, folders, key),
            moved,
            self._failed_change("Couldn't move it"),
        )

    @staticmethod
    def _what(items: list[tuple[str, str]]) -> str:
        """ "report.pdf", "Notes" or "3 items"."""
        if len(items) != 1:
            return _count(len(items), "item")
        kind, value = items[0]
        return Path(value).name if kind == FILE else _leaf(value)

    def cut(self) -> None:
        """Cut the selected rows, to paste them into another folder (Ctrl+X)."""
        self._cut(self.selected_items())

    def _cut(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        self._clipboard = list(items)
        self.summary.setText(f"Cut {self._what(items)}: open a folder and paste (Ctrl+V).")

    def paste(self, key: str | None = None) -> None:
        """Move what was cut into ``key``, or into the open folder (Ctrl+V)."""
        if key is None:
            kind, key = self.location
            if kind != FOLDER:
                self.summary.setText("Open a folder to paste into it.")
                return
        items, self._clipboard = self._clipboard, []
        if items:
            self._move_items(items, key)

    def delete_selected(self) -> None:
        """Move the selected rows to the Trash (Del); a folder asks first."""
        self._delete_items(self.selected_items())

    def _delete_items(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        files = [Path(value) for kind, value in items if kind == FILE]
        folders = [value for kind, value in items if kind == FOLDER]
        if folders and not self._confirm_delete(folders):
            return
        what = self._what(items)

        def binned(changes) -> None:
            self._after_change(
                changes.history_ids,
                f"Moved {what} to the Trash (the _Trash folder in your AutoShelf folder)",
                changes.failed,
            )

        self.runner.run(
            lambda: self.service.trash_items(files, folders),
            binned,
            self._failed_change("Couldn't move it to the Trash"),
        )

    def _confirm_delete(self, folders: list[str]) -> bool:
        files = 0
        if self._shelf is not None:
            files = sum(
                1
                for item in self._shelf.files
                if any(item.folder == key or item.folder.startswith(key + "/") for key in folders)
            )
        if len(folders) > 1:
            what = f"{len(folders)} folders and their {_count(files, 'file')}"
        elif files:
            what = f"{_leaf(folders[0])} and its {_count(files, 'file')}"
        else:
            what = f"the empty folder {_leaf(folders[0])}"
        answer = QMessageBox.question(
            self,
            "Delete",
            f"Move {what} to the Trash?\n\nNothing is deleted: it goes into the _Trash folder "
            "in your AutoShelf folder, and Undo brings it back.",
        )
        return answer == QMessageBox.StandardButton.Yes

    def resort_folder(self, key: str, *, review: bool = True) -> None:
        """Sort the files lying in ``key`` into its subfolders: names that match move; the
        others that might belong are offered (``review``: right away)."""
        self.summary.setText(f"Looking at the files in {self._place(key)}...")

        def done(result: ResortResult) -> None:
            if not result.moved and not result.plan.maybes:
                self.summary.setText(f"No file in {self._place(key)} belongs in its subfolders.")
                return
            self.show_resort(result)
            if review and result.plan.maybes:
                self.review()

        self.runner.run(
            lambda: self.service.resort(key),
            done,
            self._failed_change("Couldn't sort into the subfolders"),
        )

    def show_resort(self, result: ResortResult) -> None:
        """Say what sorting into a new folder did, with Undo and (if any) Review."""
        if not result.moved and not result.plan.maybes:
            return
        targets = sorted({_leaf(item.target) for item in result.plan.moves}, key=str.casefold)
        parts = []
        if result.moved:
            parts.append(f"Moved {_count(len(result.moved), 'file')} into {', '.join(targets)}.")
        if result.plan.maybes:
            parts.append(f"{_count(len(result.plan.maybes), 'file')} might belong too: Review.")
        self.pending_review = result.plan if result.plan.maybes else None
        self.review_button.setVisible(self.pending_review is not None)
        if result.moved:
            self._after_change([item.history_id for item in result.moved], " ".join(parts))
        else:
            self._note = " ".join(parts)
            self.summary.setText(self._note)
            self.refresh()

    def review(self) -> None:
        """Choose which of the files that might belong in the new folder move."""
        if self.pending_review is None:
            return
        dialog = ReviewDialog(self.pending_review, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.move_reviewed(dialog.chosen())

    def move_reviewed(self, chosen: list[Candidate]) -> None:
        """Move the files ticked in the review (AutoShelf learns from the choice)."""
        plan, self.pending_review = self.pending_review, None
        self.review_button.setVisible(False)
        if plan is None or not chosen:
            return
        targets = sorted({_leaf(item.target) for item in chosen}, key=str.casefold)

        def moved(result: ResortResult) -> None:
            self._after_change(
                [item.history_id for item in result.moved],
                f"Moved {_count(len(result.moved), 'file')} into {', '.join(targets)}",
                result.failed,
            )

        self.runner.run(
            lambda: self.service.move_maybes(plan, chosen),
            moved,
            self._failed_change("Couldn't move them"),
        )

    def undo_last_change(self) -> None:
        """Undo the change the status line reports."""
        ids, self._undo_ids = self._undo_ids, []
        self.undo_button.setVisible(False)
        if not ids:
            return

        def undone(results) -> None:
            self._note = f"Undone ({_count(len(results), 'change')})"
            self.edited.emit(self._note)
            self.refresh()

        self.runner.run(
            lambda: self.service.undo_changes(ids), undone, self._failed_change("Couldn't undo it")
        )

    def _after_change(
        self,
        history_ids: list[int],
        message: str,
        failed: list[tuple[Path, str]] = (),
        select: tuple[str, str] | None = None,
    ) -> None:
        if failed:
            message += f" ({_count(len(failed), 'item')} couldn't: {failed[0][1]})"
        self._undo_ids = list(history_ids)
        self.undo_button.setVisible(bool(history_ids))
        self._note = message
        self._select_after_load = select
        self.summary.setText(message)
        self.edited.emit(message)
        self.refresh()

    def _failed_change(self, title: str):
        def failed(exc: Exception) -> None:
            self.summary.setText(str(exc))
            QMessageBox.warning(self, title, str(exc))

        return failed
