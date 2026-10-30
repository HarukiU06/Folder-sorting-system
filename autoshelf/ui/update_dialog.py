"""The Update dialog: what's new in a release, then the download with a progress bar.

Nothing is downloaded until the user clicks Update. The download runs off the UI thread and
can be cancelled; a damaged one is reported and can be tried again. When the installer is
downloaded and verified, the dialog is accepted and ``installer`` holds its path (the app
then starts it and quits).
"""

import threading
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    StrongBodyLabel,
)

from autoshelf import __version__
from autoshelf.updates import Release

MB = 1024 * 1024


class UpdateDialog(QDialog):
    """Offers a release; downloads its installer when the user clicks Update."""

    progressed = Signal(int, int)  # (bytes so far, of), from the download's thread

    def __init__(self, release: Release, service, runner, parent=None) -> None:
        super().__init__(parent)
        self.release = release
        self.service = service
        self.runner = runner
        self.installer: Path | None = None
        self._cancel = threading.Event()
        self._busy = False
        self.setWindowTitle("Update AutoShelf")
        self.resize(520, 420)

        heading = StrongBodyLabel(
            f"AutoShelf {release.version} is available (you have {__version__})"
        )
        self.notes = PlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlainText(release.notes.strip() or "No notes for this release.")
        size = f"about {release.size / MB:,.0f} MB" if release.size else "the installer"
        about = BodyLabel(
            f"Update downloads {size} from GitHub. AutoShelf then closes, installs it, and "
            "opens again. Your settings and history stay."
        )
        about.setWordWrap(True)
        self.progress = ProgressBar(useAni=False)  # no animation still running when it closes
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.status = CaptionLabel("")
        self.status.setWordWrap(True)
        self.update_button = PrimaryPushButton("Update")
        self.update_button.clicked.connect(self.start)
        self.cancel_button = PushButton("Not now")
        self.cancel_button.clicked.connect(self.cancel)
        self.progressed.connect(self._show_progress)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.update_button)
        buttons.addWidget(self.cancel_button)
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self.notes, 1)
        layout.addWidget(about)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)
        layout.addLayout(buttons)

    def start(self) -> None:
        """Download and verify the installer in the background."""
        if self._busy:
            return
        self._busy = True
        self._cancel.clear()
        self.update_button.setEnabled(False)
        self.cancel_button.setText("Cancel")
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.status.setText("Downloading...")
        self.runner.run(
            lambda: self.service.download_update(
                self.release, self.progressed.emit, self._cancel.is_set
            ),
            self._downloaded,
            self._failed,
        )

    def cancel(self) -> None:
        """Stop the download (the dialog closes once it has stopped), or close."""
        if self._busy:
            self._cancel.set()
            self.status.setText("Cancelling...")
        else:
            super().reject()

    def reject(self) -> None:  # Escape or the window's close button
        self.cancel()

    def _show_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(min(100, done * 100 // total))
            self.status.setText(f"Downloading... {done / MB:,.0f} of {total / MB:,.0f} MB")

    def _downloaded(self, installer: Path) -> None:
        self._busy = False
        self.installer = installer
        self.status.setText("Downloaded and checked. Installing...")
        self.accept()

    def _failed(self, exc: Exception) -> None:
        self._busy = False
        if self._cancel.is_set():
            super().reject()
            return
        self.progress.setVisible(False)
        self.status.setText(str(exc))
        self.update_button.setText("Try again")
        self.update_button.setEnabled(True)
        self.cancel_button.setText("Not now")
