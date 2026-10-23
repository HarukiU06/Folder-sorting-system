"""The desktop app: tray icon, main window and the watcher, wired together.

Run it with ``python -m autoshelf`` or the ``autoshelf-app`` command (no console window).
"""

import argparse
import contextlib
import ctypes
import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from autoshelf import commands
from autoshelf.app_service import AppService, WhyHere
from autoshelf.commands import Command, CommandError
from autoshelf.config import AppPaths, ConfigError, get_app_paths
from autoshelf.logging_setup import setup_logging
from autoshelf.ml import calibrate as calibration
from autoshelf.pipeline import Outcome
from autoshelf.ui.icons import app_icon
from autoshelf.ui.main_window import MainWindow
from autoshelf.ui.tasks import TaskRunner
from autoshelf.ui.tray import Tray
from autoshelf.ui.why_panel import WhyDialog

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "HarukiU06.AutoShelf"
NOTIFY_DELAY_MSECS = 1500  # files arriving together get one notification
REFRESH_DELAY_MSECS = 700
# Explorer starts one AutoShelf per selected file; what arrives within this long of the
# previous one is handled together (ten right-clicked images: one ZIP).
BATCH_MSECS = 1500
# How long a launch keeps trying to reach the app another launch is starting.
FORWARD_SECONDS = 20.0
SHOW_COMMAND = Command(commands.SHOW)
QUIT_COMMAND = Command(commands.QUIT)


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
        self._calibration_checked = False

        self._notify_timer = QTimer(self, singleShot=True, interval=NOTIFY_DELAY_MSECS)
        self._notify_timer.timeout.connect(self._notify)
        self._batch = commands.Batch()
        self._batch_timer = QTimer(self, singleShot=True, interval=BATCH_MSECS)
        self._batch_timer.timeout.connect(self._run_batch)
        self._dialogs: list[WhyDialog] = []
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
        self.runner.run(self.service.repair_explorer_menu, None, lambda exc: None)
        self.window.set_state("Paused" if settings.paused else "Starting...")
        self.window.refresh_all()
        if show:
            self.show_window()
        self._report_skipped()

    def _report_skipped(self) -> None:
        """Say so when files that stopped the last run are sorted by name only from now on."""
        skipped = self.service.newly_skipped()
        if not skipped:
            return
        names = ", ".join(path.name for path in skipped[:3])
        more = f" and {len(skipped) - 3} more" if len(skipped) > 3 else ""
        self.window.show_message(
            f"AutoShelf stopped while reading {names}{more} last time, so it sorts them by "
            "name only now. Settings lists them, to read them again."
        )

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
        self._maybe_offer_calibration()

    def _maybe_offer_calibration(self) -> None:
        """Once ever, when the folders already hold enough files: offer to test how well
        AutoShelf would sort them (Settings)."""
        if self._calibration_checked:
            return
        self._calibration_checked = True

        def files_to_test() -> int | None:
            if self.service.settings().calibration_offered:
                return None  # offered before: never again
            return self.service.library_file_count()

        def checked(files: int | None) -> None:
            if files is None:
                return
            if files < calibration.MIN_FILES:
                self._calibration_checked = False  # look again when the window next opens
                return
            self.offer_calibration(files)

        def failed(exc: Exception) -> None:
            self._calibration_checked = False  # e.g. no folder chosen yet

        self.runner.run(files_to_test, checked, failed)

    def offer_calibration(self, files: int) -> None:
        """Ask whether to test sorting on the files already in the folders."""
        self.runner.run(
            lambda: self.service.save_settings(
                replace(self.service.settings(), calibration_offered=True)
            )
        )
        answer = QMessageBox.question(
            self.window,
            "How well would AutoShelf sort your files?",
            f"Your folders already hold {files} files. AutoShelf can sort each of them as if "
            "it had just arrived and compare the result with where it is, to show how often "
            "it would be right at each setting. Nothing is moved.\n\nTest it now?",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.show_window(self.window.settings)
            self.window.settings.calibrate()

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
            self._maybe_offer_calibration()

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

    # --- commands from Explorer's menu, notifications and later launches ------------

    def receive(self, command: Command) -> None:
        """A command from another launch. File commands wait briefly for the rest of a
        multi-file selection (one process per file), then run together."""
        if command.action == commands.QUIT:
            log.info("Quit requested by another process")
            QTimer.singleShot(0, QApplication.quit)
        elif command.action == commands.SHOW:
            self.show_window()
        else:
            self._batch.add(command)
            self._batch_timer.start()

    def _run_batch(self) -> None:
        for command in self._batch.take():
            self.run_command(command)

    def run_command(self, command: Command) -> None:
        """Do what a (merged) command asks."""
        files = [Path(item) for item in command.files]
        log.info("Command %s for %d file(s)", command.action, len(files))
        if command.action == commands.SORT:
            self.show_window(self.window.tidy)
            self.window.tidy.sort_files(files)
        elif command.action == commands.WHY:
            self.explain(files[0])
        elif command.action == commands.COMPRESS:
            self.make(lambda: [self.service.compress(files)])
        elif command.action == commands.CONVERT:
            to = command.to or ""
            self.make(lambda: self.service.convert(files, to))
        else:
            self.show_window()

    def explain(self, path: Path) -> None:
        """Show why a file is where it is."""

        def found(result: WhyHere) -> None:
            self.show_window()
            dialog = WhyDialog(result, self.window)
            self._dialogs = [item for item in self._dialogs if item.isVisible()] + [dialog]
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()

        self.runner.run(
            lambda: self.service.why_here(path),
            found,
            lambda exc: self._warn(f"Why is {path.name} here?", str(exc)),
        )

    def make(self, work: Callable[[], list]) -> None:
        """Compress or convert in the background, then say what was made."""

        def done(created) -> None:
            names = ", ".join(item.dst.name for item in created)
            self.window.history.refresh()
            self._tell(f"Made {names}", "It is next to the original, which is unchanged.")

        self.runner.run(work, done, lambda exc: self._warn("Couldn't make the file", str(exc)))

    def _tell(self, title: str, message: str) -> None:
        self.window.show_message(f"{title}. {message}")
        if not self.window.isVisible() or self.window.isMinimized():
            self.tray.notify(title, message)

    def _warn(self, title: str, text: str) -> None:
        parent = self.window if self.window.isVisible() else None
        QMessageBox.warning(parent, title, text)

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


def send_command(
    name: str, command: Command, *, wait_seconds: float = 0.0, timeout_ms: int = 3000
) -> bool:
    """Send a command to the app running for a data folder.

    Returns True once the app has acknowledged it. With ``wait_seconds``, keeps trying to
    connect that long (another launch may be starting the app) and waits as long for the
    answer. A command is written at most once, so it never runs twice. Blocks, so the
    receiving app must be in another process (or thread).
    """
    deadline = time.monotonic() + wait_seconds
    socket = QLocalSocket()
    while True:
        socket.connectToServer(name)
        if socket.waitForConnected(timeout_ms):
            break
        socket.abort()
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    socket.write(commands.encode(command))
    socket.flush()
    answer = b""
    answer_deadline = time.monotonic() + max(timeout_ms / 1000, wait_seconds)
    while not answer.endswith(b"\n"):
        remaining = int((answer_deadline - time.monotonic()) * 1000)
        if remaining <= 0 or not socket.waitForReadyRead(remaining):
            break
        answer += bytes(socket.readAll().data())
    socket.disconnectFromServer()
    return answer == commands.ACK


def _serve_commands(server: QLocalServer, app: "AutoShelfApp") -> None:
    """Handle commands from later launches, Explorer's menu, notifications and tools."""

    def serve(connection: QLocalSocket) -> None:
        reader = commands.LineReader()

        def read() -> None:
            try:
                received = [commands.decode(line) for line in reader.feed(_read_all(connection))]
            except CommandError as exc:
                log.warning("Ignored a command from another process: %s", exc)
                connection.write(commands.REFUSED)
                connection.flush()
                connection.disconnectFromServer()
                return
            for command in received:
                connection.write(commands.ACK)
                connection.flush()
                app.receive(command)

        connection.readyRead.connect(read)
        connection.disconnected.connect(connection.deleteLater)
        if connection.bytesAvailable():
            read()

    def accept() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                serve(connection)

    server.newConnection.connect(accept)


def _read_all(connection: QLocalSocket) -> bytes:
    return bytes(connection.readAll().data())


def listen(name: str) -> QLocalServer:
    """Listen for commands; only this Windows user's processes may connect."""
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    QLocalServer.removeServer(name)  # a stale name left by a crash
    if not server.listen(name):
        log.warning("Can't receive commands from other launches: %s", server.errorString())
    return server


def run_app(*, verbose: bool = False, show: bool = True, command: Command | None = None) -> int:
    """Run the desktop app until the user quits, or hand ``command`` (or "show the window")
    to the app that is already running. Returns the exit code."""
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

    name = server_name(paths)
    message = command or SHOW_COMMAND
    # Hand the command to the running app (Explorer starts one launch per selected file).
    if send_command(name, message):
        log.info("AutoShelf is already running; handed it %s", message.action)
        return 0
    if message.action == commands.QUIT:
        return 0  # nothing to quit
    service = AppService(paths)
    if not service.acquire():
        # Another launch is starting the app right now, or `autoshelf watch` is running.
        if send_command(name, message, wait_seconds=FORWARD_SECONDS):
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

    server = listen(name)
    app = AutoShelfApp(service)
    _serve_commands(server, app)
    log.info("AutoShelf app started (data folder %s)", paths.home)
    app.start(show=show and command is None)
    if command is not None:
        app.receive(command)
    try:
        return qapp.exec()
    finally:
        server.close()
        app.shutdown()


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point of the ``autoshelf-app`` command (and of the packaged AutoShelf.exe)."""
    parser = argparse.ArgumentParser(prog="AutoShelf")
    parser.add_argument("--verbose", "-v", action="store_true", help="log debug output")
    parser.add_argument("--background", action="store_true", help="start in the tray")
    commands.add_app_arguments(parser)
    args = parser.parse_args(commands.safe_url_argv(list(sys.argv[1:] if argv is None else argv)))
    try:
        command = commands.from_args(args)
    except CommandError as exc:
        parser.error(str(exc))
    sys.exit(run_app(verbose=args.verbose, show=not args.background, command=command))
