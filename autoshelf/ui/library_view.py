"""The Library screen: search by meaning or words, tag views, and a file's details
(tags, related files) (SPEC sections 5.1, 5.3, 5.4)."""

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
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService, FileInfo
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import plain_label, plain_tooltip, show_in_explorer

PATH_ROLE = Qt.ItemDataRole.UserRole
CONVERSION_LABELS = {
    "pdf": "PDF",
    "docx": "Word document (.docx; the layout is only approximated)",
    "png": "PNG image",
    "jpg": "JPG image",
    "webp": "WEBP image",
    "bmp": "BMP image",
    "tiff": "TIFF image",
}
COLUMNS = ("File", "Folder", "Match")
HINT = (
    'Search your files by meaning ("paper explaining k-means initialization") or by exact '
    'words ("invoice 2025"). Tags on the left list every file with that tag, wherever it is.'
)


def _size(size: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return str(size)


class LibraryView(QWidget):
    """Search results or a tag's files on the left, the selected file's details on the right."""

    changed = Signal(str)  # a message after tags changed

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self._paths: list[Path] = []
        self._info: FileInfo | None = None
        self._search_number = 0

        self.query = QLineEdit()
        self.query.setPlaceholderText("Search your library...")
        self.query.returnPressed.connect(self.search)
        search = QPushButton("Search")
        search.clicked.connect(self.search)
        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(search)
        self.summary = plain_label(HINT)
        self.summary.setWordWrap(True)

        self.tag_list = QListWidget()
        self.tag_list.itemClicked.connect(self._tag_clicked)
        tags_box = QWidget()
        tags_layout = QVBoxLayout(tags_box)
        tags_layout.setContentsMargins(0, 0, 0, 0)
        tags_layout.addWidget(QLabel("Tags"))
        tags_layout.addWidget(self.tag_list, 1)

        self.results = QTableWidget(0, len(COLUMNS))
        self.results.setHorizontalHeaderLabels(COLUMNS)
        self.results.verticalHeader().setVisible(False)
        self.results.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.results.setWordWrap(False)
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.results.itemSelectionChanged.connect(self._selected)
        self.results.itemDoubleClicked.connect(lambda _: self.open_file())

        self.detail = self._build_detail()
        splitter = QSplitter()
        splitter.addWidget(tags_box)
        splitter.addWidget(self.results)
        splitter.addWidget(self.detail)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([150, 520, 380])

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(splitter, 1)
        self._show_detail(None)

    def _build_detail(self) -> QWidget:
        box = QWidget()
        self.name = plain_label()
        self.name.setWordWrap(True)
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = self.name.font()
        font.setBold(True)
        self.name.setFont(font)
        self.facts = QLabel()
        self.facts.setWordWrap(True)
        self.facts.setTextFormat(Qt.TextFormat.RichText)
        self.summary_text = plain_label()
        self.summary_text.setWordWrap(True)
        open_button = QPushButton("Open")
        open_button.clicked.connect(self.open_file)
        show = QPushButton("Show in Explorer")
        show.clicked.connect(self._show_in_explorer)
        self.summarize_button = QPushButton("Summarize")
        self.summarize_button.setToolTip("Ask the AI for a 2-3 sentence summary")
        self.summarize_button.clicked.connect(self.summarize)
        self.summarize_button.setVisible(False)
        rename = QPushButton("Rename...")
        rename.clicked.connect(self.rename_file)
        buttons = QHBoxLayout()
        self.compress_button = QPushButton("Compress")
        self.compress_button.setToolTip(
            "Make a smaller copy (a PDF or an image) or a ZIP next to the file; "
            "the file itself stays as it is"
        )
        self.compress_button.clicked.connect(self.compress_file)
        self.convert_button = QToolButton()
        self.convert_button.setText("Convert to")
        self.convert_button.setToolTip("Make a copy in another format next to the file")
        self.convert_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.convert_menu = QMenu(self.convert_button)
        self.convert_button.setMenu(self.convert_menu)
        self.convert_button.setEnabled(False)
        buttons.addWidget(open_button)
        buttons.addWidget(show)
        buttons.addWidget(rename)
        buttons.addWidget(self.compress_button)
        buttons.addWidget(self.convert_button)
        buttons.addWidget(self.summarize_button)
        buttons.addStretch(1)

        self.file_tags = QListWidget()
        self.file_tags.setMaximumHeight(90)
        self.new_tag = QLineEdit()
        self.new_tag.setPlaceholderText("Add a tag...")
        self.new_tag.returnPressed.connect(self._add_typed_tag)
        add = QPushButton("Add")
        add.clicked.connect(self._add_typed_tag)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_tag)
        tag_row = QHBoxLayout()
        tag_row.addWidget(self.new_tag, 1)
        tag_row.addWidget(add)
        tag_row.addWidget(remove)
        self.suggested = QHBoxLayout()
        self.suggested_buttons: list[QPushButton] = []

        self.related = QListWidget()
        self.related.itemDoubleClicked.connect(self._open_related)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setPlaceholderText("No text preview.")

        layout = QVBoxLayout(box)
        layout.addWidget(self.name)
        layout.addWidget(self.facts)
        layout.addWidget(self.summary_text)
        layout.addLayout(buttons)
        layout.addWidget(QLabel("Tags"))
        layout.addWidget(self.file_tags)
        layout.addLayout(tag_row)
        layout.addLayout(self.suggested)
        layout.addWidget(QLabel("Related files (double-click to show)"))
        layout.addWidget(self.related, 1)
        layout.addWidget(QLabel("Preview"))
        layout.addWidget(self.preview, 1)
        return box

    # --- loading ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the tag list."""
        self.runner.run(self.service.tags, self._show_tags)

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

    def _tag_clicked(self, item: QListWidgetItem) -> None:
        name = item.data(PATH_ROLE)
        if not name:
            return

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
            for column, text in enumerate((path.name, folder, match)):
                item = QTableWidgetItem(text)
                item.setData(PATH_ROLE, str(path))
                item.setToolTip(str(path) if column < 2 else plain_tooltip(text))
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
            entry = QListWidgetItem(f"{item.similarity:.2f}  {item.path.name}  ({item.folder})")
            entry.setData(PATH_ROLE, str(item.path))
            entry.setToolTip(str(item.path))
            self.related.addItem(entry)
        self.preview.setPlainText(info.excerpt or "")

    def _fill_conversions(self, formats) -> None:
        self.convert_menu.clear()
        for to in formats:
            action = self.convert_menu.addAction(CONVERSION_LABELS.get(to, to.upper()))
            action.triggered.connect(lambda _=False, to=to: self.convert_file(to))
        self.convert_button.setEnabled(bool(formats))

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
        """Show the AI buttons only when an AI provider is on."""
        self.ai_enabled = enabled
        self.summarize_button.setVisible(enabled)

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
        use_ai = getattr(self, "ai_enabled", False)

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
            button = QPushButton(f"+ {name}")
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
