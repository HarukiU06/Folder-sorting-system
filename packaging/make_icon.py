"""Write the app icon (drawn in code, see autoshelf.ui.icons) as a Windows .ico file.

Usage: python packaging/make_icon.py OUTPUT.ico
"""

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

from autoshelf.ui.icons import app_icon

SIZES = (16, 24, 32, 48, 64, 128, 256)


def main(target: Path) -> None:
    """Render the icon at 256 px and save it with every common size."""
    QApplication.instance() or QApplication([])
    pixmap = app_icon().pixmap(256, 256)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    image = Image.open(io.BytesIO(bytes(buffer.data()))).convert("RGBA")
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="ICO", sizes=[(size, size) for size in SIZES])
    print(f"Wrote {target}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
