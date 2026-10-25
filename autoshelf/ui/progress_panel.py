"""The progress bar above every page while AutoShelf gets ready: downloading the language
model (first start only), reading the files already in your folders, sorting the new
files that were waiting. It says how long is left until everything is sorted."""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    IndeterminateProgressBar,
    ProgressBar,
    SimpleCardWidget,
    StrongBodyLabel,
)

from autoshelf import wording
from autoshelf.app_service import AppService
from autoshelf.features.estimate import LOAD_SECONDS, Estimate, Eta
from autoshelf.ui.tasks import TaskRunner
from autoshelf.watcher import service as watch

DOWNLOAD_POLL_MSECS = 1000
DONE_MSECS = 5000  # how long "Done" stays before the panel goes away


class ProgressPanel(SimpleCardWidget):
    """Shows the watcher's Progress with the time left; hidden while nothing is going on."""

    def __init__(self, service: AppService, runner: TaskRunner, parent=None) -> None:
        super().__init__(parent)
        self.service = service
        self.runner = runner
        self.estimate: Estimate | None = None
        self.stage: str | None = None
        self._eta: Eta | None = None
        self._downloading = False

        self.title = StrongBodyLabel()
        self.detail = CaptionLabel()
        self.left = BodyLabel()
        self.bar = ProgressBar()
        self.busy = IndeterminateProgressBar()
        self.busy.setVisible(False)
        top = QHBoxLayout()
        top.addWidget(self.title)
        top.addWidget(self.detail, 1)
        top.addWidget(self.left)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.addLayout(top)
        layout.addWidget(self.bar)
        layout.addWidget(self.busy)

        self._download_timer = QTimer(self, interval=DOWNLOAD_POLL_MSECS)
        self._download_timer.timeout.connect(self._poll_download)
        self._hide_timer = QTimer(self, singleShot=True, interval=DONE_MSECS)
        self._hide_timer.timeout.connect(lambda: self.setVisible(False))
        self.setVisible(False)

    def set_estimate(self, estimate: Estimate | None) -> None:
        """What the remaining stages are expected to take (from features.estimate)."""
        self.estimate = estimate

    # --- progress from the watcher ------------------------------------------------------

    def show_progress(self, progress: watch.Progress) -> None:
        """Show how far the watcher has got."""
        if progress.stage != self.stage:
            self._start_stage(progress)
        if progress.stage == watch.LOADING:
            if not self._downloading:
                self._show(
                    "Starting",
                    "loading the language model",
                    self._left(LOAD_SECONDS + self._reading() + self._sorting()),
                    None,
                )
        elif progress.stage == watch.READING:
            left = self._eta.update(progress.done) if self._eta else None
            self._show(
                "Reading your folders",
                f"{progress.done:,} of {progress.total:,} files (once only)",
                self._left(None if left is None else left + self._sorting()),
                (progress.done, progress.total),
            )
        elif progress.stage == watch.SORTING:
            left = self._eta.update(progress.done) if self._eta else None
            self._show(
                "Sorting new files",
                f"{progress.done:,} of {progress.total:,}",
                self._left(left),
                (progress.done, progress.total),
            )
        elif progress.stage == watch.PAUSED:
            count = progress.total
            self._show(
                "Paused",
                f"{count:,} new file{'' if count == 1 else 's'} will be sorted when you resume",
                "",
                None,
                busy=False,
            )
        elif progress.stage == watch.READY:
            self._finish()

    def _start_stage(self, progress: watch.Progress) -> None:
        self.stage = progress.stage
        self._hide_timer.stop()
        self._download_timer.stop()
        self._downloading = False
        self._eta = None
        if progress.stage == watch.LOADING:
            self.runner.run(self.service.model_download, self._download_state, lambda exc: None)
        elif progress.stage == watch.READING:
            expected = self.estimate.reading_seconds if self.estimate else None
            self._eta = Eta(progress.total, expected or None)
        elif progress.stage == watch.SORTING:
            expected = self.estimate.sorting_seconds if self.estimate else None
            self._eta = Eta(progress.total, expected or None)

    def _finish(self) -> None:
        self._download_timer.stop()
        was_shown = self.isVisible()
        self.stage = watch.READY
        self.estimate = None  # used up: the next start works out its own
        if was_shown:
            self._show("Done", "AutoShelf is watching your folder for new files", "", (1, 1))
            self._hide_timer.start()

    # --- the model download -------------------------------------------------------------

    def _download_state(self, state: tuple[int, int] | None) -> None:
        if self.stage != watch.LOADING:
            return
        if state is None:  # already on this PC: only loading
            self.show_progress(watch.Progress(watch.LOADING))
            return
        self._downloading = True
        self._eta = Eta(state[1])
        self._show_download(state)
        self._download_timer.start()

    def _poll_download(self) -> None:
        self.runner.run(self.service.model_download, self._downloaded, lambda exc: None)

    def _downloaded(self, state: tuple[int, int] | None) -> None:
        if self.stage != watch.LOADING or not self._downloading:
            return
        if state is None:  # finished: now it loads
            self._download_timer.stop()
            self._downloading = False
            self.show_progress(watch.Progress(watch.LOADING))
            return
        self._show_download(state)

    def _show_download(self, state: tuple[int, int]) -> None:
        done, total = state
        left = self._eta.update(done) if self._eta else None
        later = LOAD_SECONDS + self._reading() + self._sorting()
        self._show(
            "Downloading the language model",
            f"{done / 1e6:,.0f} of {total / 1e6:,.0f} MB (first start only)",
            self._left(None if left is None else left + later),
            (done, total),
        )

    # --- showing ------------------------------------------------------------------------

    def _reading(self) -> float:
        return self.estimate.reading_seconds if self.estimate else 0.0

    def _sorting(self) -> float:
        return self.estimate.sorting_seconds if self.estimate else 0.0

    @staticmethod
    def _left(seconds: float | None) -> str:
        return "" if seconds is None else f"{wording.duration(seconds)} left"

    def _show(
        self,
        title: str,
        detail: str,
        left: str,
        fraction: tuple[float, float] | None,
        *,
        busy: bool = True,
    ) -> None:
        self.title.setText(title)
        self.detail.setText(detail)
        self.left.setText(left)
        known = fraction is not None and fraction[1] > 0
        self.bar.setVisible(known or not busy)
        self.busy.setVisible(busy and not known)
        if known:
            self.bar.setValue(round(100 * fraction[0] / fraction[1]))
        elif not busy:
            self.bar.setValue(0)
        if self.busy.isVisible():
            self.busy.start()
        else:
            self.busy.stop()
        self.setVisible(True)
