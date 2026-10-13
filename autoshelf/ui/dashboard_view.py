"""The Dashboard: numbers about the library and how well sorting goes (SPEC section 5.12)."""

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService
from autoshelf.features.stats import LibraryStats
from autoshelf.ui.tasks import TaskRunner

BAR_COLOUR = QColor("#3b82a0")


class BarChart(QWidget):
    """Horizontal bars with labels, drawn in code (no chart library needed)."""

    ROW = 22

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: list[tuple[str, int]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_items(self, items: list[tuple[str, int]]) -> None:
        """Show these (label, value) pairs."""
        self.items = items
        self.setMinimumHeight(max(self.ROW, self.ROW * len(items)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        text = self.palette().color(QPalette.ColorRole.WindowText)
        largest = max((value for _, value in self.items), default=0) or 1
        label_width = min(self.width() // 2, 220)
        bar_space = max(10, self.width() - label_width - 60)
        for row, (label, value) in enumerate(self.items):
            top = row * self.ROW
            painter.setPen(text)
            painter.drawText(
                QRectF(0, top, label_width - 8, self.ROW),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                painter.fontMetrics().elidedText(
                    label, Qt.TextElideMode.ElideLeft, label_width - 8
                ),
            )
            width = bar_space * value / largest
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(BAR_COLOUR)
            painter.drawRoundedRect(QRectF(label_width, top + 4, max(width, 1), self.ROW - 8), 3, 3)
            painter.setPen(text)
            painter.drawText(
                QRectF(label_width + width + 6, top, 60, self.ROW),
                Qt.AlignmentFlag.AlignVCenter,
                str(value),
            )
        painter.end()


def _card(title: str) -> tuple[QGroupBox, QLabel]:
    box = QGroupBox(title)
    value = QLabel("-")
    font = value.font()
    font.setPointSizeF(font.pointSizeF() * 1.8)
    font.setBold(True)
    value.setFont(font)
    layout = QVBoxLayout(box)
    layout.addWidget(value)
    return box, value


class DashboardView(QWidget):
    """Cards, two bar charts and two lists."""

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner

        cards = QHBoxLayout()
        self.values: dict[str, QLabel] = {}
        for key, title in (
            ("files", "Files in your folders"),
            ("inbox", "Waiting in the Inbox"),
            ("accuracy", "Automatic moves kept"),
            ("rules", "Moved by your rules"),
        ):
            box, value = _card(title)
            self.values[key] = value
            cards.addWidget(box)
        self.accuracy_note = QLabel()
        self.accuracy_note.setWordWrap(True)

        self.per_folder = BarChart()
        self.per_month = BarChart()
        self.corrected = QListWidget()
        self.stale = QListWidget()
        self.stale_title = QLabel("Not opened in 6+ months")

        charts = QGridLayout()
        charts.addWidget(QLabel("<b>Files per folder</b>"), 0, 0)
        charts.addWidget(QLabel("<b>Files added per month</b>"), 0, 1)
        charts.addWidget(self.per_folder, 1, 0, Qt.AlignmentFlag.AlignTop)
        charts.addWidget(self.per_month, 1, 1, Qt.AlignmentFlag.AlignTop)
        charts.addWidget(QLabel("<b>Most corrected (moved from -> to)</b>"), 2, 0)
        charts.addWidget(self.stale_title, 2, 1)
        charts.addWidget(self.corrected, 3, 0)
        charts.addWidget(self.stale, 3, 1)

        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bottom = QHBoxLayout()
        self.status = QLabel()
        bottom.addWidget(self.status, 1)
        bottom.addWidget(refresh)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(cards)
        layout.addWidget(self.accuracy_note)
        layout.addLayout(charts, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll, 1)
        outer.addLayout(bottom)

    def refresh(self) -> None:
        """Recompute the numbers in the background."""
        self.status.setText("Counting...")
        self.runner.run(
            self.service.stats, self.show_stats, lambda exc: self.status.setText(str(exc))
        )

    def show_stats(self, stats: LibraryStats) -> None:
        """Display the numbers."""
        self.status.setText("")
        self.values["files"].setText(str(stats.total_files))
        self.values["inbox"].setText(str(stats.inbox))
        self.values["rules"].setText(str(stats.rule_moves))
        if stats.accuracy is None:
            self.values["accuracy"].setText("-")
            self.accuracy_note.setText("No automatic moves yet.")
        else:
            self.values["accuracy"].setText(f"{stats.accuracy:.0%}")
            kept = stats.auto_moves - stats.auto_corrected
            self.accuracy_note.setText(
                f"All {kept} automatic moves were kept."
                if kept == stats.auto_moves
                else f"{kept} of {stats.auto_moves} automatic moves were kept; the rest you "
                "undid or moved to another folder."
            )
        self.per_folder.set_items(stats.files_per_folder)
        self.per_month.set_items(stats.added_per_month)
        self.corrected.clear()
        for source, target, count in stats.corrected_pairs:
            self.corrected.addItem(f"{count}x  {source} -> {target}")
        if not stats.corrected_pairs:
            self.corrected.addItem("No corrections yet.")
        self.stale_title.setText(f"<b>Not opened in 6+ months ({stats.stale_total})</b>")
        self.stale.clear()
        for item in stats.stale:
            self.stale.addItem(f"{item.last_used:%Y-%m-%d}  {item.path.name}")
