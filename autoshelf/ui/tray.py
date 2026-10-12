"""The system tray icon: menu, status tooltip and notifications."""

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from autoshelf.ui.icons import app_icon

NOTIFICATION_MSECS = 10_000


class Tray(QObject):
    """The tray icon. Works (and does nothing visible) where there is no system tray."""

    open_requested = Signal()
    inbox_requested = Signal()  # also emitted when a notification is clicked
    pause_toggled = Signal(bool)
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self._icons = {False: app_icon(), True: app_icon(paused=True)}
        self._waiting = 0
        self._paused = False
        self._status = ""

        self.menu = QMenu()
        open_action = QAction("Open AutoShelf", self.menu)
        open_action.triggered.connect(self.open_requested)
        self.inbox_action = QAction("Inbox", self.menu)
        self.inbox_action.triggered.connect(self.inbox_requested)
        self.pause_action = QAction("Pause sorting", self.menu)
        self.pause_action.setCheckable(True)
        self.pause_action.toggled.connect(self.pause_toggled)
        quit_action = QAction("Quit AutoShelf", self.menu)
        quit_action.triggered.connect(self.quit_requested)
        self.menu.addAction(open_action)
        self.menu.addAction(self.inbox_action)
        self.menu.addSeparator()
        self.menu.addAction(self.pause_action)
        self.menu.addSeparator()
        self.menu.addAction(quit_action)

        self.icon = QSystemTrayIcon(self._icons[False], self)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._activated)
        self.icon.messageClicked.connect(self.inbox_requested)
        self._update()
        if self.available:
            self.icon.show()

    def set_waiting(self, count: int) -> None:
        """Show how many files wait in the Inbox."""
        self._waiting = count
        self._update()

    def set_paused(self, paused: bool) -> None:
        """Reflect the pause state (without emitting ``pause_toggled``)."""
        self._paused = paused
        self.pause_action.blockSignals(True)
        self.pause_action.setChecked(paused)
        self.pause_action.blockSignals(False)
        self._update()

    def set_status(self, status: str) -> None:
        """A short line about what AutoShelf is doing, shown in the tooltip."""
        self._status = status
        self._update()

    def notify(self, title: str, message: str) -> bool:
        """Show a notification. Returns False if the system can't show one."""
        if not (self.available and self.icon.isVisible() and self.icon.supportsMessages()):
            return False
        self.icon.showMessage(title, message, self._icons[False], NOTIFICATION_MSECS)
        return True

    def hide(self) -> None:
        """Remove the icon (on quit)."""
        self.icon.hide()

    def tooltip(self) -> str:
        """The current tooltip text."""
        lines = ["AutoShelf" + (" (paused)" if self._paused else "")]
        if self._waiting:
            lines.append(f"{self._waiting} file{'' if self._waiting == 1 else 's'} in the Inbox")
        if self._status:
            lines.append(self._status)
        return "\n".join(lines)

    def _update(self) -> None:
        self.icon.setIcon(self._icons[self._paused])
        self.inbox_action.setText(f"Inbox ({self._waiting})" if self._waiting else "Inbox")
        self.icon.setToolTip(self.tooltip()[:120])

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.open_requested.emit()
