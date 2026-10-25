"""Small UI helpers shared by the screens."""

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QDesktopServices, Qt  # QtGui's Qt also has the rich-text helpers
from PySide6.QtWidgets import QLabel, QWidget
from qfluentwidgets import RoundMenu


def plain_label(text: str = "") -> QLabel:
    """A label that shows its text literally, for text from files or the AI.

    Qt renders label text that looks like HTML as HTML, so a document title could, for
    example, load an image from a network share (which leaks the Windows sign-in hash).
    """
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def plain[L: QLabel](label: L) -> L:
    """Make any label (a Fluent one too) show its text literally; see ``plain_label``."""
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def plain_tooltip(text: str) -> str:
    """``text`` as a tooltip that is shown literally (tooltips are rendered like labels)."""
    return Qt.convertFromPlainText(text) if Qt.mightBeRichText(text) else text


def show_menu(button: QWidget, actions: Iterable[QAction | None]) -> RoundMenu:
    """Open a menu of ``actions`` (None: a separator) just below a "⋯" button.

    The menu is made afresh each time, so it lists exactly the actions that apply now
    (Fluent menus show hidden actions too).
    """
    menu = RoundMenu(parent=button)
    for action in actions:
        if action is None:
            menu.addSeparator()
        else:
            menu.addAction(action)
    menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
    return menu


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
