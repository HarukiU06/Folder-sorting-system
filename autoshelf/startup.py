"""Start with Windows (SPEC section 2): a per-user entry in the registry's Run key.

``HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` gets a value named
"AutoShelf" that starts the app in the tray (``--background``). Nothing is installed and no
administrator rights are needed; turning the option off deletes the value again.
"""

import sys
from pathlib import Path
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "AutoShelf"
BACKGROUND_FLAG = "--background"
APP_EXE = "AutoShelf.exe"  # the packaged tray app (next to autoshelf-cli.exe)


class StartupError(Exception):
    """The Run entry couldn't be read or changed."""


class RunKey(Protocol):
    """The few registry operations needed (replaced by a fake in tests)."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, command: str) -> None: ...

    def delete(self, name: str) -> None: ...


class WindowsRunKey:
    """The current user's Run key in the real registry."""

    def get(self, name: str) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, name)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, name: str, command: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)

    def delete(self, name: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


def default_run_key() -> RunKey:
    """The registry Run key (tests replace this function)."""
    return WindowsRunKey()


def app_executable() -> Path | None:
    """The packaged tray app (AutoShelf.exe), or None when running from source."""
    if not getattr(sys, "frozen", False):
        return None
    executable = Path(sys.executable)
    app = executable.with_name(APP_EXE)
    return app if app.exists() else executable


def app_command() -> str:
    """How Windows starts the desktop app, without a console window: the packaged app, or
    pythonw with this package. Options go after it."""
    app = app_executable()
    if app is not None:
        # Always the tray app, also when a setting is changed from autoshelf-cli.exe
        # (a console program would open a window every time).
        return f'"{app}"'
    executable = Path(sys.executable)
    windowed = executable.with_name("pythonw.exe")
    interpreter = windowed if windowed.exists() else executable
    return f'"{interpreter}" -m autoshelf'


def startup_command() -> str:
    """The command Windows runs at sign-in: the app, in the tray."""
    return f"{app_command()} {BACKGROUND_FLAG}"


def is_enabled(run_key: RunKey | None = None) -> bool:
    """True if AutoShelf starts with Windows."""
    if sys.platform != "win32" and run_key is None:
        return False
    try:
        return (run_key or default_run_key()).get(VALUE_NAME) is not None
    except OSError as exc:
        raise StartupError(f"Couldn't read the startup setting: {exc}") from exc


def set_enabled(enabled: bool, run_key: RunKey | None = None) -> None:
    """Make AutoShelf start (or stop starting) with Windows."""
    if sys.platform != "win32" and run_key is None:
        raise StartupError("Starting with the system is only supported on Windows")
    key = run_key or default_run_key()
    try:
        if enabled:
            key.set(VALUE_NAME, startup_command())
        else:
            key.delete(VALUE_NAME)
    except OSError as exc:
        raise StartupError(f"Couldn't change the startup setting: {exc}") from exc
