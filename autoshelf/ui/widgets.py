"""Small UI helpers shared by the screens."""

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


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
