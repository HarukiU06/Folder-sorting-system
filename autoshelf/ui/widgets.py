"""Small UI helpers shared by the screens."""

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, Qt  # QtGui's Qt also has the rich-text helpers
from PySide6.QtWidgets import QLabel


def plain_label(text: str = "") -> QLabel:
    """A label that shows its text literally, for text from files or the AI.

    Qt renders label text that looks like HTML as HTML, so a document title could, for
    example, load an image from a network share (which leaks the Windows sign-in hash).
    """
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def plain_tooltip(text: str) -> str:
    """``text`` as a tooltip that is shown literally (tooltips are rendered like labels)."""
    return Qt.convertFromPlainText(text) if Qt.mightBeRichText(text) else text


def open_folder(folder: Path) -> None:
    """Open a folder in Explorer."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))


def show_in_explorer(path: Path) -> None:
    """Open Explorer with ``path`` selected (or just its folder elsewhere)."""
    if sys.platform == "win32" and path.exists():
        # Explorer parses its own command line; the quotes must wrap only the path.
        subprocess.Popen(f'explorer /select,"{path}"')
    else:
        open_folder(path.parent)
