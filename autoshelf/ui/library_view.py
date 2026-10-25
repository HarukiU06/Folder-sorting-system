"""The Library screen: search, your folders and tags on the left, and a file's details
(tags, related files, compress/convert) (SPEC sections 5.1, 5.3, 5.4).

Choosing a folder lists its files and lets you describe what belongs in it, which helps
AutoShelf recognise such files (this used to be a Folders screen of its own).
"""

import html
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QListWidgetItem,
    QMessageBox,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
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
    TitleLabel,
    TransparentToolButton,
)

from autoshelf.app_service import AppService, FileInfo, FolderInfo
from autoshelf.features import convert
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import open_folder, plain, plain_tooltip, show_in_explorer, show_menu

PATH_ROLE = Qt.ItemDataRole.UserRole
COLUMNS = ("File", "Folder")
HINT = (
    'Search by meaning ("paper explaining k-means initialization") or by exact words '
    '("invoice 2025"), or pick a folder or a tag on the left.'
)
DESCRIBE_HINT = "What belongs in this folder? A few words help AutoShelf sort new files."
LEFT_WIDTH = 200
DETAIL_WIDTH = 320
FOLDER_COLUMN_WIDTH = 130


def _size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


class LibraryView(QWidget):
    """Folders and tags on the left, search results or a folder's files in the middle, the
    selected file's details on the right."""

    changed = Signal(str)  # a message after tags changed or a file was made or renamed
    saved = Signal(str)  # a message after a folder's description was saved

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.ai_enabled = False
        self._paths: list[Path] = []
        self._info: FileInfo | None = None
        self._folders: list[FolderInfo] = []
        self._folder: FolderInfo | None = None
        self._search_number = 0

        self.query = SearchLineEdit()
        self.query.setPlaceholderText("Search your files")
        self.query.returnPressed.connect(self.search)
        self.query.searchSignal.connect(lambda _: self.search())
        self.summary = plain(CaptionLabel(HINT))
        self.summary.setWordWrap(True)

        self.folder_list = ListWidget()
        self.folder_list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.folder_list.itemClicked.connect(self._folder_clicked)
        self.tag_list = ListWidget()
        self.tag_list.itemClicked.connect(self._tag_clicked)
        left = QWidget()
        left.setFixedWidth(LEFT_WIDTH)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(StrongBodyLabel("Folders"))
        left_layout.addWidget(self.folder_list, 2)
        left_layout.addWidget(StrongBodyLabel("Tags"))
        left_layout.addWidget(self.tag_list, 1)

        self.folder_box = self._build_folder_box()
        self.results = TableWidget()
        self.results.setColumnCount(len(COLUMNS))
        self.results.setHorizontalHeaderLabels(COLUMNS)
        self.results.verticalHeader().setVisible(False)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setWordWrap(False)
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.results.setColumnWidth(1, FOLDER_COLUMN_WIDTH)
        self.results.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.results.itemSelectionChanged.connect(self._selected)
        self.results.itemDoubleClicked.connect(lambda _: self.open_file())
        middle = QVBoxLayout()
        middle.addWidget(self.folder_box)
        middle.addWidget(self.results, 1)

        self.detail = self._build_detail()
        body = QHBoxLayout()
        body.setSpacing(16)
        body.addWidget(left)
        body.addLayout(middle, 1)
        body.addWidget(self.detail)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        layout.addWidget(TitleLabel("Library"))
        layout.addWidget(self.query)
        layout.addWidget(self.summary)
        layout.addLayout(body, 1)
        self._show_detail(None)

    def _build_folder_box(self) -> QWidget:
        box = SimpleCardWidget()
        self.folder_name = plain(SubtitleLabel())
        self.description = LineEdit()
        self.description.setPlaceholderText(DESCRIBE_HINT)
        self.description.setToolTip(DESCRIBE_HINT)
        self.description.editingFinished.connect(self._describe)
        open_button = PushButton(FluentIcon.FOLDER, "Open in Explorer")
        open_button.clicked.connect(self._open_folder)
        self.study_button = PushButton(FluentIcon.BOOK_SHELF, "Study…")
        self.study_button.setToolTip("A reading list and flashcards for this folder (AI)")
        self.study_button.clicked.connect(self.study_folder)
        self.study_button.setVisible(False)
        heading = QHBoxLayout()
        heading.addWidget(self.folder_name, 1)
        heading.addWidget(self.study_button)
        heading.addWidget(open_button)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addLayout(heading)
        layout.addWidget(self.description)
        box.setVisible(False)
        return box

    def _build_detail(self) -> QWidget:
        box = SimpleCardWidget()
        box.setFixedWidth(DETAIL_WIDTH)
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

    def menu_actions(self) -> list[Action | None]:
        """What the ⋯ menu offers for the selected file."""
        actions: list[Action | None] = [self.open_action, self.show_action, self.rename_action]
        actions += [None, self.compress_action, *self.convert_actions]
        if self.ai_enabled:
            actions += [None, self.summarize_action]
        return actions

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the folders and tags."""
        self.runner.run(self.service.folders, self._show_folders, lambda exc: None)
        self.runner.run(self.service.tags, self._show_tags)

    def _show_folders(self, folders: list[FolderInfo]) -> None:
        self._folders = folders
        self.folder_list.clear()
        for folder in folders:
            item = QListWidgetItem(f"{folder.key.replace('/', ' / ')}  ({folder.files})")
            item.setData(PATH_ROLE, folder.key)
            item.setToolTip(plain_tooltip(folder.description) if folder.description else "")
            self.folder_list.addItem(item)
        if not folders:
            self.folder_list.addItem(QListWidgetItem("(no folders yet)"))
        if self._folder is not None:
            keys = [folder.key for folder in folders]
            if self._folder.key in keys:
                self._folder = folders[keys.index(self._folder.key)]
            else:
                self._folder = None
                self.folder_box.setVisible(False)

    def _show_tags(self, tags: list[tuple[str, int]]) -> None:
        self.tag_list.clear()
        for name, count in tags:
            item = QListWidgetItem(f"{name}  ({count})")
            item.setData(PATH_ROLE, name)
            self.tag_list.addItem(item)
        if not tags:
            self.tag_list.addItem(QListWidgetItem("(no tags yet)"))

    def search(self) -> None:
        """Search for the typed query in the background."""
        query = self.query.text().strip()
        if not query:
            return
        self._search_number += 1
        number = self._search_number
        self._show_folder(None)
        self.summary.setText(f"Searching for {query!r}... (the first search loads the model)")

        def found(hits) -> None:
            if number != self._search_number:
                return  # a newer search is under way
            rows = [
                (
                    hit.path,
                    hit.folder or "(root)",
                    ("[has the words] " if hit.keyword else "")
                    + (f"Summary: {hit.summary}" if hit.summary else hit.snippet),
                )
                for hit in hits
            ]
            self._show_rows(rows)
            self.summary.setText(
                f"{len(hits)} file{'' if len(hits) == 1 else 's'} for {query!r}"
                if hits
                else f"Nothing found for {query!r}."
            )

        self.runner.run(lambda: self.service.search(query), found, self._failed)

    def _folder_clicked(self, item: QListWidgetItem) -> None:
        key = item.data(PATH_ROLE)
        folder = next((folder for folder in self._folders if folder.key == key), None)
        if folder is None:
            return
        self._search_number += 1  # a search still under way no longer shows
        self._show_folder(folder)

        def found(paths: list[Path]) -> None:
            self._show_rows([(path, key, "") for path in paths])
            count = len(paths)
            self.summary.setText(f"{count} file{'' if count == 1 else 's'} in {key}")

        self.runner.run(lambda: self.service.folder_files(key), found, self._failed)

    def _show_folder(self, folder: FolderInfo | None) -> None:
        self._folder = folder
        self.folder_box.setVisible(folder is not None)
        if folder is not None:
            self.folder_name.setText(folder.key.replace("/", " / "))
            self.description.setText(folder.description)

    def _describe(self) -> None:
        folder = self._folder
        text = self.description.text().strip()
        if folder is None or text == folder.description:
            return
        key = folder.key
        self._folder = FolderInfo(key, folder.files, text)
        self.runner.run(
            lambda: self.service.describe(key, text),
            lambda _: (self.saved.emit(f"Saved the description of {key}"), self.refresh()),
            self._failed,
        )

    def _open_folder(self) -> None:
        folder = self._folder
        if folder is None:
            return
        self.runner.run(
            self.service.root,
            lambda root: root and open_folder(root.joinpath(*folder.key.split("/"))),
        )

    def study_folder(self) -> None:
        """Open study mode (reading list, flashcards) for the chosen folder."""
        from autoshelf.ui.study_dialog import StudyDialog

        if self._folder is not None:
            StudyDialog(self.service, self.runner, self._folder.key, self).exec()

    def _tag_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(PATH_ROLE)
        if not name:
            return
        self._search_number += 1
        self._show_folder(None)

        def found(paths: list[Path]) -> None:
            self._show_rows([(path, path.parent.name, "") for path in paths])
            self.summary.setText(f"Tagged {name!r}: {len(paths)} file(s)")

        self.runner.run(lambda: self.service.tagged(name), found, self._failed)

    def _failed(self, exc: Exception) -> None:
        self.summary.setText(str(exc))

    def _show_rows(self, rows: list[tuple[Path, str, str]]) -> None:
        self._paths = [path for path, _, _ in rows]
        self.results.clearSelection()  # so selecting row 0 below shows the new first file
        self.results.setRowCount(len(rows))
        for row, (path, folder, match) in enumerate(rows):
            tip = plain_tooltip(f"{path}\n{match}" if match else str(path))
            for column, text in enumerate((path.name, folder)):
                item = QTableWidgetItem(text)
                item.setData(PATH_ROLE, str(path))
                item.setToolTip(tip)
                self.results.setItem(row, column, item)
        if rows:
            self.results.selectRow(0)
        else:
            self._show_detail(None)

    # --- the selected file ------------------------------------------------------------

    def current_path(self) -> Path | None:
        """The selected file."""
        rows = self.results.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self._paths):
            return None
        return self._paths[rows[0].row()]

    def _selected(self) -> None:
        path = self.current_path()
        if path is None:
            self._show_detail(None)
            return
        self.runner.run(
            lambda: (
                self.service.file_info(path),
                self.service.related(path),
                self.service.conversions(path),
            ),
            lambda result: self._show_detail(*result),
            lambda exc: self.summary.setText(f"{path.name}: {exc}"),
        )

    def _show_detail(self, info: FileInfo | None, related=(), conversions=()) -> None:
        self._info = info
        self.detail.setEnabled(info is not None)
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
        where = info.folder or "(root)"
        title = f"<br>Title: {html.escape(info.title)}" if info.title else ""
        self.facts.setText(
            f"In {html.escape(where)} &middot; {_size(info.size)} &middot; {modified}{title}"
        )
        self.summary_text.setText(f"Summary: {info.summary}" if info.summary else "")
        self.summary_text.setVisible(bool(info.summary))
        self.file_tags.clear()
        self.file_tags.addItems(info.tags)
        self._show_suggestions(info.tag_suggestions)
        self.related.clear()
        for item in related:
            entry = QListWidgetItem(f"{item.path.name}  ({item.folder})")
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
        info = self._info
        if info is not None:
            self._make(lambda: [self.service.compress([info.path])])

    def convert_file(self, to: str) -> None:
        """Make a copy of the selected file in format ``to``, next to it."""
        info = self._info
        if info is not None:
            self._make(lambda: self.service.convert([info.path], to))

    def _make(self, work) -> None:
        self.summary.setText("Making the new file...")

        def done(created) -> None:
            names = ", ".join(item.dst.name for item in created)
            self.summary.setText(f"Made {names} next to the original (undo in History).")
            self.changed.emit(f"Made {names}")
            if created:
                folder = self._info.folder if self._info else ""
                self._show_rows([(item.dst, folder or "(root)", "") for item in created])

        def failed(exc: Exception) -> None:
            self.summary.setText(str(exc))
            QMessageBox.warning(self, "Couldn't make the file", str(exc))

        self.runner.run(work, done, failed)

    def set_ai_enabled(self, enabled: bool) -> None:
        """Offer the AI features (summaries, study mode) only when an AI provider is on."""
        self.ai_enabled = enabled
        self.study_button.setVisible(enabled)

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
        info = self._info
        if info is None:
            return
        use_ai = self.ai_enabled

        def ask(proposal) -> None:
            current = proposal.new_name if proposal else info.path.name
            source = f" (from its {proposal.source})" if proposal else ""
            name, ok = QInputDialog.getText(
                self, "Rename", f"New name for {info.path.name}{source}:", text=current
            )
            if ok and name.strip() and name.strip() != info.path.name:
                self.runner.run(
                    lambda: self.service.rename(info.path, name.strip()),
                    lambda result: (
                        self.changed.emit(f"Renamed to {result.dst.name} (undo in History)"),
                        self._show_rows([(result.dst, info.folder or "(root)", "")]),
                    ),
                    lambda exc: QMessageBox.warning(self, "Couldn't rename", str(exc)),
                )

        def failed(exc: Exception) -> None:
            ask(None)

        self.runner.run(lambda: self.service.propose_name(info.path, use_ai=use_ai), ask, failed)

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
            self._selected()

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
            self._selected()

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
        path = Path(item.data(PATH_ROLE))
        self._show_rows([(path, path.parent.name, "")])
        self.summary.setText(f"Related to {self._info.path.name if self._info else ''}")
