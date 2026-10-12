"""Run slow work (file reading, the model, the database) off the UI thread.

Results come back through a signal of the ``TaskRunner``, which lives on the GUI thread,
so the callbacks always run there and may touch widgets.
"""

import itertools
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

log = logging.getLogger(__name__)

OnDone = Callable[[Any], None]
OnError = Callable[[Exception], None]


class _Job(QRunnable):
    def __init__(self, runner: "TaskRunner", job_id: int, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._runner = runner
        self._id = job_id
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:
            log.debug("Background task failed", exc_info=True)
            self._runner.failed.emit(self._id, exc)
        else:
            self._runner.finished.emit(self._id, result)


class TaskRunner(QObject):
    """Runs functions on a small thread pool and calls back on the GUI thread."""

    finished = Signal(int, object)
    failed = Signal(int, object)

    def __init__(self, parent: QObject | None = None, *, threads: int = 3) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(threads)
        self._ids = itertools.count(1)
        self._callbacks: dict[int, tuple[OnDone | None, OnError | None]] = {}
        self.on_error: OnError = lambda exc: log.error("Background task failed: %s", exc)
        self.finished.connect(self._deliver)
        self.failed.connect(self._deliver_error)

    def run(
        self,
        fn: Callable[[], Any],
        on_done: OnDone | None = None,
        on_error: OnError | None = None,
    ) -> None:
        """Run ``fn()`` in the background; then call ``on_done(result)`` or ``on_error``."""
        job_id = next(self._ids)
        self._callbacks[job_id] = (on_done, on_error)
        self._pool.start(_Job(self, job_id, fn))

    @property
    def busy(self) -> bool:
        """True while any task hasn't delivered its result yet."""
        return bool(self._callbacks)

    def wait(self, msecs: int = 30_000) -> bool:
        """Block until the pool is idle (for shutdown and tests)."""
        return self._pool.waitForDone(msecs)

    @Slot(int, object)
    def _deliver(self, job_id: int, result: Any) -> None:
        on_done, _ = self._callbacks.pop(job_id, (None, None))
        if on_done is not None:
            on_done(result)

    @Slot(int, object)
    def _deliver_error(self, job_id: int, exc: Exception) -> None:
        _, on_error = self._callbacks.pop(job_id, (None, None))
        (on_error or self.on_error)(exc)
