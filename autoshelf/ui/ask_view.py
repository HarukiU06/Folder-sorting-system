"""The Ask screen: questions about your files, answered by the optional AI with citations
(SPEC section 5.10)."""

import html
import re
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from autoshelf.app_service import AppService
from autoshelf.features.ask import Answer
from autoshelf.llm import PRIVACY_NOTE
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.widgets import plain_label, show_in_explorer

_CITATION = re.compile(r"\[(\d+)\]")


def answer_html(answer: Answer) -> str:
    """An answer with clickable citations, followed by its sources."""
    numbers = {source.number for source in answer.sources}

    def link(match: re.Match) -> str:
        number = int(match.group(1))
        return f'<a href="source:{number}">[{number}]</a>' if number in numbers else match.group()

    text = _CITATION.sub(link, html.escape(answer.text)).replace("\n", "<br>")
    shown = answer.cited or tuple(sorted(numbers))
    sources = "".join(
        f'<li><a href="source:{source.number}">[{source.number}] '
        f"{html.escape(source.path.name)}</a>"
        f" <small>({html.escape(source.folder or 'root')})</small></li>"
        for source in answer.sources
        if source.number in shown
    )
    return (
        f"<p><b>You:</b> {html.escape(answer.question)}</p><p>{text}</p>"
        f"<p><small>Sources:</small></p><ul>{sources}</ul><hr>"
    )


class AskView(QWidget):
    """A conversation about the library; citations open the cited file in Explorer."""

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.history: list[Answer] = []

        note = QLabel(
            'Ask anything about your files, e.g. "What do my notes say about PCA?". The '
            "most relevant passages are sent to the AI you chose in Settings. " + PRIVACY_NOTE
        )
        note.setWordWrap(True)
        self.transcript = QTextBrowser()
        self.transcript.setOpenLinks(False)
        self.transcript.anchorClicked.connect(self._open_source)
        self.question = QLineEdit()
        self.question.setPlaceholderText("Your question...")
        self.question.returnPressed.connect(self.ask)
        self.ask_button = QPushButton("Ask")
        self.ask_button.clicked.connect(self.ask)
        clear = QPushButton("New conversation")
        clear.clicked.connect(self.clear)
        row = QHBoxLayout()
        row.addWidget(self.question, 1)
        row.addWidget(self.ask_button)
        row.addWidget(clear)
        self.status = plain_label()

        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(self.transcript, 1)
        layout.addLayout(row)
        layout.addWidget(self.status)
        self._html: list[str] = []

    def ask(self) -> None:
        """Send the typed question (in the background)."""
        question = self.question.text().strip()
        if not question:
            return
        self.ask_button.setEnabled(False)
        self.status.setText("Reading your files and asking the AI...")
        history = list(self.history)

        def answered(answer: Answer) -> None:
            self.ask_button.setEnabled(True)
            self.status.setText("")
            self.question.clear()
            self.history.append(answer)
            self._html.append(answer_html(answer))
            self.transcript.setHtml("".join(self._html))
            self.transcript.verticalScrollBar().setValue(
                self.transcript.verticalScrollBar().maximum()
            )

        def failed(exc: Exception) -> None:
            self.ask_button.setEnabled(True)
            self.status.setText(str(exc))

        self.runner.run(lambda: self.service.ask(question, history), answered, failed)

    def clear(self) -> None:
        """Start over (follow-up questions no longer see the earlier ones)."""
        self.history = []
        self._html = []
        self.transcript.clear()

    def _open_source(self, url: QUrl) -> None:
        number = int(url.toString().split(":", 1)[1])
        for answer in reversed(self.history):
            for source in answer.sources:
                if source.number == number:
                    show_in_explorer(source.path)
                    return

    @staticmethod
    def open_file(path: Path) -> None:
        """Open a file with its usual program."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
