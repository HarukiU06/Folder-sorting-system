"""The app icon, drawn in code (books on a shelf), so there are no image files to ship."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

_BACKGROUND = QColor("#2f6f8f")
_PAUSED_BACKGROUND = QColor("#7a7f85")
_BOOKS = (
    (0.20, 0.30, QColor("#f2c14e")),  # (left, top, colour) as fractions of the size
    (0.36, 0.22, QColor("#f78154")),
    (0.52, 0.36, QColor("#b4d6c1")),
)
_SHELF = QColor("#f4f1ea")


def _draw(size: int, paused: bool) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_PAUSED_BACKGROUND if paused else _BACKGROUND)
    painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.2, size * 0.2)
    shelf_top = size * 0.74
    for left, top, colour in _BOOKS:
        painter.setBrush(colour)
        painter.drawRect(QRectF(size * left, size * top, size * 0.13, shelf_top - size * top))
    # A leaning book on the right.
    painter.save()
    painter.translate(size * 0.80, shelf_top)
    painter.rotate(-18)
    painter.setBrush(QColor("#e8e8e8"))
    painter.drawRect(QRectF(-size * 0.13, -size * 0.42, size * 0.12, size * 0.42))
    painter.restore()
    painter.setBrush(_SHELF)
    painter.drawRect(QRectF(size * 0.12, shelf_top, size * 0.76, size * 0.07))
    painter.end()
    return pixmap


def app_icon(paused: bool = False) -> QIcon:
    """The AutoShelf icon (grey while sorting is paused). Needs a QGuiApplication."""
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_draw(size, paused))
    return icon
