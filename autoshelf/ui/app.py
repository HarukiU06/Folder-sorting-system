"""The desktop app: tray icon, main window and the watcher, wired together.

Run it with ``python -m autoshelf`` or the ``autoshelf-app`` command (no console window).
"""

import contextlib
import ctypes
import hashlib
import logging
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from autoshelf.app_service import AppService
from autoshelf.config import AppPaths, ConfigError, get_app_paths
from autoshelf.logging_setup import setup_logging
from autoshelf.pipeline import Outcome
from autoshelf.ui.icons import app_icon
from autoshelf.ui.main_window import MainWindow
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.tray import Tray

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "HarukiU06.AutoShelf"
NOTIFY_DELAY_MSECS = 1500  # files arriving together get one notification
REFRESH_DELAY_MSECS = 700
SHOW_COMMAND = b"show"
QUIT_COMMAND = b"quit"
ACK = b"ok"


def icloud_drive() -> Path | None:
    """The iCloud Drive folder of iCloud for Windows, if there is one."""
    folder = Path.home() / "iCloudDrive"
    return folder if folder.is_dir() else None


def notification_text(outcomes: list[Outcome]) -> tuple[str, str]:
    """Title and message for files that now need a decision."""
    if len(outcomes) == 1:
        outcome = outcomes[0]
        top = outcome.classification.top(3) if outcome.classification else ()
        guesses = ", ".join(f"{score.folder} {score.score:.2f}" for score in top)
        message = f"Best guesses: {guesses}. " if guesses else ""
        return f"Where should {outcome.path.name} go?", message + "Click to choose a folder."
    names = [outcome.path.name for outcome in outcomes]
    shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return f"{len(names)} files need your decision", f"{shown}. Click to open your Inbox."


class AutoShelfApp(QObject):
    """Connects the AppService, the window and the tray. Lives on the GUI thread."""

    outcome_arrived = Signal(object)  # emitted from the watcher's thread
    status_arrived = Signal(str)
    learned_arrived = Signal(object)

    def __init__(self, service: AppService, *, tray: Tray | None = None) -> None:
        super().__init__()
        self.service = service
        self.runner = TaskRunner(self)
        self.runner.on_error = self._task_failed
        self.tray = tray or Tray(self)
        self.window = MainWindow(service, self.runner, keep_running_on_close=self.tray.available)
        self._pending: list[Outcome] = []
        self._notifications = True
        self._notify_path: Path | None = None

        self._notify_timer = QTimer(self, singleShot=True, interval=NOTIFY_DELAY_MSECS)
        self._notify_timer.timeout.connect(self._notify)
        self._refresh_timer = QTimer(self, singleShot=True, interval=REFRESH_DELAY_MSECS)
        self._refresh_timer.timeout.connect(self._refresh_after_outcomes)

        service.on_outcome = self.outcome_arrived.emit
        service.on_status = self.status_arrived.emit
        service.on_learned = self.learned_arrived.emit
        self.outcome_arrived.connect(self._on_outcome)
        self.status_arrived.connect(self._on_status)
        self.learned_arrived.connect(lambda relocations: self.window.after_learning())

        self.tray.open_requested.connect(lambda: self.show_window())
        self.tray.inbox_requested.connect(self._open_inbox_from_tray)
        self.tray.pause_toggled.connect(self.set_paused)
        self.tray.quit_requested.connect(QApplication.quit)
        settings = self.window.settings
        settings.change_root_requested.connect(self.choose_root)
        settings.paused_toggled.connect(self.set_paused)
        settings.notifications_toggled.connect(self._set_notifications)
        settings.restart_needed.connect(self.restart)
        self.window.inbox.count_changed.connect(self.tray.set_waiting)

    # --- lifecycle ----------------------------------------------------------------

    def start(self, *, show: bool = True) -> None:
        """Start watching (or ask for the root folder on first run)."""
        settings = self.service.settings()
        self._notifications = settings.notifications
        self.tray.set_paused(settings.paused)
        if self.service.root() is None:
            self.window.set_state("Choose the folder to sort (Settings)")
            self.show_window(self.window.settings)
            QTimer.singleShot(0, self.choose_root)
            return
        self.service.start()
        self.window.set_state("Paused" if settings.paused else "Starting...")
        self.window.refresh_all()
        if show:
            self.show_window()

    def shutdown(self) -> None:
        """Stop everything (called after the event loop ends)."""
        self.tray.hide()
        self.window.hide()
        self.runner.wait(5000)
        self.service.release()

    def show_window(self, tab=None) -> None:
        """Bring the main window to the front."""
        if tab is not None:
            self.window.show_tab(tab)
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    # --- user actions -------------------------------------------------------------

    def set_paused(self, paused: bool) -> None:
        """Pause or resume automatic sorting (from the tray or Settings)."""
        self.tray.set_paused(paused)
        checkbox = self.window.settings.paused
        checkbox.blockSignals(True)
        checkbox.setChecked(paused)
        checkbox.blockSignals(False)
        self.window.set_state("Paused" if paused else "Sorting")

        def saved(_) -> None:
            self.window.settings.load()  # a load started earlier may show the old state
            self.window.show_message(
                "Sorting paused: new files wait until you resume" if paused else "Sorting resumed"
            )

        self.runner.run(lambda: self.service.set_paused(paused), saved)

    def restart(self, message: str) -> None:
        """Restart the watcher in the background so new settings apply."""

        def restarted(watching: bool) -> None:
            self.window.show_message(message)
            self.window.refresh_all()

        self.runner.run(self.service.restart, restarted)

    def choose_root(self) -> None:
        """Let the user pick the folder to sort, then (re)start watching it."""
        current = self.service.root()
        start = current or icloud_drive() or Path.home()
        folder = QFileDialog.getExistingDirectory(
            self.window, "Choose the folder AutoShelf should sort", str(start)
        )
        if not folder:
            return
        try:
            root = self.service.check_root(Path(folder))
        except ConfigError as exc:
            QMessageBox.warning(self.window, "This folder can't be used", str(exc))
            return
        if current is not None and root == current:
            return
        paused = self.service.settings().paused
        loose = self.service.loose_files(root)
        if loose:
            choice = self.ask_about_loose_files(root, loose)
            if choice is None:
                return
            paused = choice == "paused"

        def switch() -> bool:
            self.service.set_root(root)
            self.service.set_paused(paused)
            return self.service.restart()

        def switched(watching: bool) -> None:
            self.tray.set_paused(paused)
            self.window.show_message(f"Now sorting {root}" + (" (paused)" if paused else ""))
            self.window.refresh_all()

        self.runner.run(switch, switched)

    def ask_about_loose_files(self, root: Path, count: int) -> str | None:
        """Ask whether to sort files already in a new root: "now", "paused" or None."""
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Files are waiting in this folder")
        box.setText(
            f"{count} file{'' if count == 1 else 's'} in {root} (or its _Inbox) would be "
            "sorted straight away."
        )
        box.setInformativeText(
            "Sort them now, or start paused so you can look first? You can resume any time "
            "from the tray menu or Settings."
        )
        now = box.addButton("Sort them now", QMessageBox.ButtonRole.AcceptRole)
        paused = box.addButton("Start paused", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is now:
            return "now"
        if clicked is paused:
            return "paused"
        return None

    # --- watcher events -------------------------------------------------------------

    def _on_outcome(self, outcome: Outcome) -> None:
        if outcome.needs_decision and self._notifications:
            self._pending.append(outcome)
            self._notify_timer.start()
        self._refresh_timer.start()

    def _on_status(self, message: str) -> None:
        if not self.tray.pause_action.isChecked():
            self.window.set_state(message)
        self.tray.set_status(message)

    def _notify(self) -> None:
        pending, self._pending = self._pending, []
        if not pending:
            return
        title, message = notification_text(pending)
        self._notify_path = pending[0].dst if len(pending) == 1 else None
        if not self.tray.notify(title, message):
            self.window.show_message(f"{title} {message}")

    def _open_inbox_from_tray(self) -> None:
        self.show_window()
        self.window.show_inbox(self._notify_path)
        self._notify_path = None

    def _refresh_after_outcomes(self) -> None:
        self.window.inbox.refresh()
        self.window.history.refresh()

    def _set_notifications(self, enabled: bool) -> None:
        self._notifications = enabled

    def _task_failed(self, exc: Exception) -> None:
        log.error("Background task failed: %s", exc)
        self.window.show_message(f"Something went wrong: {exc}")


# --- process entry points -------------------------------------------------------------


def _ensure_streams() -> None:
    """Without a console (pythonw), stdout/stderr are None; libraries still print."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115


def _set_app_id() -> None:
    """Group the app under its own taskbar icon instead of Python's."""
    if sys.platform == "win32":
        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def server_name(paths: AppPaths) -> str:
    """Name of the local socket a running instance listens on (one per data folder)."""
    digest = hashlib.sha1(str(paths.home).casefold().encode()).hexdigest()[:12]
    return f"AutoShelf-{digest}"


def send_command(name: str, command: bytes, timeout_ms: int = 3000) -> bool:
    """Send ``show`` or ``quit`` to the app running for a data folder.

    Returns True once the app has acknowledged it. Blocks, so the receiving app must be
    in another process (or thread).
    """
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(timeout_ms):
        return False
    socket.write(command)
    socket.flush()
    acknowledged = socket.waitForReadyRead(timeout_ms) and bytes(socket.readAll().data()) == ACK
    socket.disconnectFromServer()
    return acknowledged


def _serve_commands(server: QLocalServer, app: "AutoShelfApp") -> None:
    """Handle commands from later launches (show the window) or tools (quit)."""

    def accept() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def read() -> None:
            command = bytes(connection.readAll().data()).strip()
            if not command:
                return
            connection.write(ACK)
            connection.flush()
            if command == QUIT_COMMAND:
                log.info("Quit requested by another process")
                QTimer.singleShot(0, QApplication.quit)
            else:
                app.show_window()

        connection.readyRead.connect(read)
        connection.disconnected.connect(connection.deleteLater)
        if connection.bytesAvailable():
            read()

    server.newConnection.connect(accept)


def run_app(*, verbose: bool = False, show: bool = True) -> int:
    """Run the desktop app until the user quits. Returns the exit code."""
    _ensure_streams()
    _set_app_id()
    qapp = QApplication.instance() or QApplication(sys.argv[:1])
    qapp.setApplicationName("AutoShelf")
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(app_icon())
    try:
        paths = get_app_paths()
    except ConfigError as exc:
        QMessageBox.critical(None, "AutoShelf", str(exc))
        return 1
    setup_logging(paths.log_dir, verbose=verbose)

    service = AppService(paths)
    name = server_name(paths)
    if not service.acquire():
        if send_command(name, SHOW_COMMAND):
            log.info("AutoShelf is already running; showed its window instead")
            return 0
        holder = service.lock_holder()
        QMessageBox.warning(
            None,
            "AutoShelf",
            "AutoShelf is already sorting files"
            + (f" (process {holder})" if holder else "")
            + ", probably 'autoshelf watch' in a terminal. Stop it, then start the app again.",
        )
        return 1

    server = QLocalServer()
    QLocalServer.removeServer(name)  # a stale name left by a crash
    server.listen(name)
    app = AutoShelfApp(service)
    _serve_commands(server, app)
    log.info("AutoShelf app started (data folder %s)", paths.home)
    app.start(show=show)
    try:
        return qapp.exec()
    finally:
        server.close()
        app.shutdown()


def main() -> None:
    """Entry point of the ``autoshelf-app`` command."""
    sys.exit(run_app())
