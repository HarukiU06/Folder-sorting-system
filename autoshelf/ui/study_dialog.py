"""Study mode for one folder (SPEC section 5.11): a reading list, and flashcards to make,
review and export for Anki. Uses the optional AI."""

from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService
from autoshelf.db.repository import Flashcard
from autoshelf.ui.tasks import TaskRunner


class StudyDialog(QDialog):
    """Reading list and flashcards for one folder."""

    def __init__(self, service: AppService, runner: TaskRunner, folder: str, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.folder = folder
        self.cards: list[Flashcard] = []
        self.position = 0
        self.showing_answer = False
        self.setWindowTitle(f"Study: {folder}")
        self.resize(720, 520)

        tabs = QTabWidget()
        tabs.addTab(self._reading_page(), "Reading list")
        tabs.addTab(self._cards_page(), "Flashcards")
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs, 1)
        layout.addWidget(self.status)
        self.load_cards()

    # --- reading list ----------------------------------------------------------------

    def _reading_page(self) -> QWidget:
        page = QWidget()
        self.reading = QListWidget()
        make = QPushButton("Make a reading list")
        make.clicked.connect(self.make_reading_list)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("The folder's files, from introductory to advanced:"))
        layout.addWidget(self.reading, 1)
        layout.addWidget(make)
        return page

    def make_reading_list(self) -> None:
        """Ask the AI to order the folder's files."""
        self.status.setText("Asking the AI to order the files...")

        def done(items) -> None:
            self.status.setText("")
            self.reading.clear()
            for number, item in enumerate(items, start=1):
                reason = f"  - {item.reason}" if item.reason else ""
                self.reading.addItem(f"{number}. {item.path.name}{reason}")

        self.runner.run(
            lambda: self.service.reading_list(self.folder),
            done,
            lambda e: self.status.setText(str(e)),
        )

    # --- flashcards --------------------------------------------------------------------

    def _cards_page(self) -> QWidget:
        page = QWidget()
        self.card_text = QLabel()
        self.card_text.setWordWrap(True)
        self.card_text.setMinimumHeight(160)
        font = self.card_text.font()
        font.setPointSizeF(font.pointSizeF() * 1.3)
        self.card_text.setFont(font)
        self.counter = QLabel()
        flip = QPushButton("Show answer")
        flip.clicked.connect(self.flip)
        self.flip_button = flip
        next_card = QPushButton("Next")
        next_card.clicked.connect(self.next_card)
        review = QHBoxLayout()
        review.addWidget(self.counter, 1)
        review.addWidget(flip)
        review.addWidget(next_card)

        self.count = QSpinBox()
        self.count.setRange(3, 30)
        self.count.setValue(10)
        make = QPushButton("Make cards from this folder")
        make.clicked.connect(self.make_cards)
        export = QPushButton("Export for Anki...")
        export.clicked.connect(self.export)
        tools = QHBoxLayout()
        tools.addWidget(QLabel("Cards:"))
        tools.addWidget(self.count)
        tools.addWidget(make)
        tools.addStretch(1)
        tools.addWidget(export)

        layout = QVBoxLayout(page)
        layout.addWidget(self.card_text, 1)
        layout.addLayout(review)
        layout.addLayout(tools)
        return page

    def load_cards(self) -> None:
        """Load the folder's saved cards."""
        self.runner.run(lambda: self.service.flashcards(self.folder), self._show_cards)

    def _show_cards(self, cards: list[Flashcard]) -> None:
        self.cards = cards
        self.position = 0
        self.showing_answer = False
        self._show_card()

    def _show_card(self) -> None:
        if not self.cards:
            self.card_text.setText("No cards yet. Make some from this folder's files.")
            self.counter.setText("")
            return
        card = self.cards[self.position]
        text = card.answer if self.showing_answer else card.question
        self.card_text.setText(("A: " if self.showing_answer else "Q: ") + text)
        self.flip_button.setText("Show question" if self.showing_answer else "Show answer")
        self.counter.setText(f"Card {self.position + 1} of {len(self.cards)}")

    def flip(self) -> None:
        """Turn the card over."""
        self.showing_answer = not self.showing_answer
        self._show_card()

    def next_card(self) -> None:
        """Go to the next card (question side)."""
        if self.cards:
            self.position = (self.position + 1) % len(self.cards)
            self.showing_answer = False
            self._show_card()

    def make_cards(self) -> None:
        """Ask the AI for new cards about the folder."""
        count = self.count.value()
        self.status.setText("Asking the AI for flashcards...")

        def done(new_cards) -> None:
            self.status.setText(f"Made {len(new_cards)} card(s).")
            self.load_cards()

        self.runner.run(
            lambda: self.service.make_flashcards(folder=self.folder, count=count),
            done,
            lambda e: self.status.setText(str(e)),
        )

    def export(self) -> None:
        """Save the folder's cards as a CSV file Anki can import."""
        if not self.cards:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Export flashcards", f"{self.folder.replace('/', ' - ')} cards.csv", "CSV (*.csv)"
        )
        if not target:
            return
        try:
            count = self.service.export_flashcards(self.cards, Path(target))
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't export", str(exc))
            return
        self.status.setText(f"Wrote {count} card(s) to {target}. In Anki: File > Import.")
