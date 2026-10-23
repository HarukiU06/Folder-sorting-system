"""Windows notifications with buttons (SPEC Phase 14), through windows-toasts.

Every click opens an ``autoshelf:`` link (protocol activation), so it reaches AutoShelf the
same way whether the app is running or not: Windows starts AutoShelf with the link, and
that launch hands it to the running app. Used only when the link is registered for an
AutoShelf that exists (by the installer, or Settings); otherwise the tray icon's plain
notifications are used.
"""

import logging
import sys
from dataclasses import dataclass
from typing import Protocol

from autoshelf import explorer

log = logging.getLogger(__name__)

MAX_LABEL = 32  # a longer folder name is shortened on its button


@dataclass(frozen=True)
class Note:
    """One notification: text, the link a click on it opens, buttons that open links, and
    a button that only closes it."""

    title: str
    message: str
    launch: str | None = None
    buttons: tuple[tuple[str, str], ...] = ()  # (label, link)
    dismiss: str | None = None


class Toaster(Protocol):
    """Shows notifications (a fake in tests)."""

    def show(self, note: Note) -> None: ...


class WindowsToaster:
    """Notifications from AutoShelf's AppUserModelID."""

    def __init__(self, app_id: str = explorer.APP_ID) -> None:
        from windows_toasts import InteractableWindowsToaster

        self._toaster = InteractableWindowsToaster("AutoShelf", notifierAUMID=app_id)

    def show(self, note: Note) -> None:
        from windows_toasts import (
            Toast,
            ToastButton,
            ToastSystemButton,
            ToastSystemButtonAction,
        )

        toast = Toast([note.title, note.message], launch_action=note.launch)
        for label, link in note.buttons:
            toast.AddAction(ToastButton(short(label), launch=link))
        if note.dismiss:
            toast.AddAction(ToastSystemButton(ToastSystemButtonAction.Dismiss, note.dismiss))
        self._toaster.show_toast(toast)


def short(label: str) -> str:
    """A button label that fits: "Machine Learning/Unsupervised" keeps its end."""
    return label if len(label) <= MAX_LABEL else "…" + label[-(MAX_LABEL - 1) :]


def default_toaster() -> Toaster | None:
    """Notifications with buttons if ``autoshelf:`` links reach this PC's AutoShelf, else
    None (the tray's notifications are used instead)."""
    if sys.platform != "win32":
        return None
    try:
        if not explorer.link_ready():
            return None
        return WindowsToaster()
    except Exception as exc:  # any failure: fall back to the tray
        log.warning("Notifications with buttons are unavailable: %s", exc)
        return None
