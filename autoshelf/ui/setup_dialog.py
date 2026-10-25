"""Choosing the folder AutoShelf sorts: on first start, and from Settings → Change.

The dialog suggests a folder for AutoShelf inside iCloud Drive, warns when the folder is
the top of iCloud Drive itself (every folder in it would become a place to sort into),
and says how long getting ready will take before you start.
"""

from pathlib import Path

import shiboken6
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    IconWidget,
    InfoBarIcon,
    LineEdit,
    MessageBoxBase,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TitleLabel,
)

from autoshelf import wording
from autoshelf.app_service import AppService
from autoshelf.config import ConfigError
from autoshelf.features.estimate import Estimate
from autoshelf.ui.tasks import TaskRunner

FOLDER_NAME = "AutoShelf"  # the folder suggested inside iCloud Drive
NOW = "now"
PAUSED = "paused"


def icloud_drive() -> Path | None:
    """The iCloud Drive folder of iCloud for Windows, if there is one."""
    folder = Path.home() / "iCloudDrive"
    return folder if folder.is_dir() else None


def _same(a: Path, b: Path | None) -> bool:
    return b is not None and str(a).rstrip("\\/").casefold() == str(b).rstrip("\\/").casefold()


class SetupDialog(MessageBoxBase):
    """Pick the folder, see what starting will involve, then Start or Start paused."""

    def __init__(
        self,
        service: AppService,
        runner: TaskRunner,
        parent: QWidget,
        *,
        current: Path | None = None,
        first: bool = False,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.folder: Path | None = None
        self.choice: str | None = None
        self.estimate: Estimate | None = None
        self._number = 0  # results for a folder chosen earlier are ignored

        title = TitleLabel("Welcome to AutoShelf" if first else "The folder AutoShelf sorts")
        intro = BodyLabel(
            "AutoShelf puts new files into the folders you made. It never deletes or "
            "overwrites anything, and every move can be undone."
            if first
            else "AutoShelf sorts new files in this folder into the folders inside it."
        )
        intro.setWordWrap(True)

        self.path = LineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("Choose a folder")
        self.path.setMinimumWidth(420)
        change = PushButton("Change…")
        change.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path, 1)
        path_row.addWidget(change)

        self.problem = CaptionLabel()
        self.problem.setWordWrap(True)
        self.problem.setTextColor("#c42b1c", "#ff99a4")
        self.problem.setVisible(False)
        self.make_button = PushButton()
        self.make_button.clicked.connect(self._make_suggested)
        self.make_button.setVisible(False)
        self.warning = self._warning_box()

        self.time = SubtitleLabel()
        self.lines = BodyLabel()
        self.lines.setWordWrap(True)
        self.note = CaptionLabel(
            "Files already in your folders are read once, to learn what each folder holds. "
            "Later starts take a few seconds."
        )
        self.note.setWordWrap(True)
        estimate_card = SimpleCardWidget()
        estimate_layout = QVBoxLayout(estimate_card)
        estimate_layout.setContentsMargins(16, 12, 16, 12)
        heading = QHBoxLayout()
        heading.addWidget(StrongBodyLabel("Before you start"), 1)
        heading.addWidget(self.time)
        estimate_layout.addLayout(heading)
        estimate_layout.addWidget(self.lines)
        estimate_layout.addWidget(self.note)

        self.viewLayout.addWidget(title)
        self.viewLayout.addWidget(intro)
        self.viewLayout.addWidget(StrongBodyLabel("The folder AutoShelf sorts"))
        self.viewLayout.addLayout(path_row)
        self.viewLayout.addWidget(self.problem)
        self.viewLayout.addWidget(self.make_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.viewLayout.addWidget(self.warning)
        self.viewLayout.addWidget(estimate_card)
        self.widget.setMinimumWidth(620)

        self.yesButton.setText("Start sorting")
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(lambda: self._finish(NOW))
        self.paused_button = PushButton("Start paused")
        self.paused_button.setToolTip("New files wait until you resume (tray menu or Settings)")
        self.paused_button.clicked.connect(lambda: self._finish(PAUSED))
        self.buttonLayout.insertWidget(1, self.paused_button, 1, Qt.AlignmentFlag.AlignVCenter)
        self.cancelButton.setText("Cancel")

        self.set_folder(current or self.suggestion())

    def _warning_box(self) -> QWidget:
        box = SimpleCardWidget()
        icon = IconWidget(InfoBarIcon.WARNING)
        icon.setFixedSize(20, 20)
        self.warning_text = BodyLabel(
            "This is the whole of iCloud Drive. AutoShelf would treat every folder in it as "
            "a place to sort into, including Desktop, Documents and the folders Apple's "
            "apps use. Make a folder just for AutoShelf instead."
        )
        self.warning_text.setWordWrap(True)
        self.use_subfolder = PushButton(f"Use iCloud Drive\\{FOLDER_NAME} instead")
        self.use_subfolder.clicked.connect(self._make_suggested)
        text = QVBoxLayout()
        text.addWidget(StrongBodyLabel("This is the top of iCloud Drive"))
        text.addWidget(self.warning_text)
        text.addWidget(self.use_subfolder, 0, Qt.AlignmentFlag.AlignLeft)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(text, 1)
        box.setVisible(False)
        return box

    # --- the folder ---------------------------------------------------------------------

    @staticmethod
    def suggestion() -> Path | None:
        """iCloud Drive\\AutoShelf when iCloud for Windows is there."""
        drive = icloud_drive()
        return drive / FOLDER_NAME if drive is not None else None

    def _browse(self) -> None:
        start = self.folder or icloud_drive() or Path.home()
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the folder AutoShelf should sort", str(start)
        )
        if folder:
            self.set_folder(Path(folder))

    def _make_suggested(self) -> None:
        suggested = self.suggestion()
        if suggested is None:
            return
        try:
            suggested.mkdir(exist_ok=True)  # the folder to sort, not a category folder
        except OSError as exc:
            self._show_problem(f"Couldn't make {suggested}: {exc}")
            return
        self.set_folder(suggested)

    def set_folder(self, folder: Path | None) -> None:
        """Check the folder and work out what starting there involves."""
        self._number += 1
        self.folder = None
        self.estimate = None
        self.path.setText(str(folder) if folder else "")
        self.problem.setVisible(False)
        self.make_button.setVisible(False)
        self.warning.setVisible(folder is not None and _same(folder, icloud_drive()))
        self._set_startable(False)
        self.time.setText("")
        if folder is None:
            self.lines.setText("Choose the folder you keep your sorted files in.")
            return
        if not folder.exists() and _same(folder, self.suggestion()):
            self.lines.setText("This folder doesn't exist yet.")
            self.make_button.setText(f"Make {folder.name} in iCloud Drive")
            self.make_button.setVisible(True)
            return
        try:
            root = self.service.check_root(folder)
        except ConfigError as exc:
            self._show_problem(str(exc))
            return
        self.folder = root
        self.lines.setText("Working out how long it will take...")
        number = self._number

        def current() -> bool:  # still the folder on show, in a dialog still open
            return number == self._number and shiboken6.isValid(self.lines)

        def done(result: Estimate) -> None:
            if current():
                self.show_estimate(result)

        def failed(exc: Exception) -> None:
            if current():
                self.lines.setText(f"Couldn't look at the folder: {exc}")
                self._set_startable(True)

        self.runner.run(lambda: self.service.estimate(root), done, failed)

    def _show_problem(self, text: str) -> None:
        self.problem.setText(text)
        self.problem.setVisible(True)
        self.lines.setText("")

    def show_estimate(self, result: Estimate) -> None:
        """Show what starting will involve, and allow starting."""
        self.estimate = result
        self.time.setText(wording.duration_range(result.low_seconds, result.high_seconds))
        self.lines.setText("\n".join(wording.estimate_lines(result)))
        self._set_startable(True)

    def _set_startable(self, startable: bool) -> None:
        self.yesButton.setEnabled(startable)
        self.paused_button.setEnabled(startable)

    def _finish(self, choice: str) -> None:
        if self.folder is None:
            return
        self.choice = choice
        self.accept()

    def done(self, code: int) -> None:
        self._number += 1  # an estimate still under way no longer shows
        super().done(code)
