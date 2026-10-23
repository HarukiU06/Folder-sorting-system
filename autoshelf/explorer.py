"""Explorer's right-click menu (SPEC Phase 14): per-user registry keys, no administrator
rights needed.

All files get an "AutoShelf" submenu: "Sort with AutoShelf", "Why is this here?" and
"Compress". Files AutoShelf can convert also get "Convert with AutoShelf" with the formats
that file type allows. Each entry starts the app with an option (``AutoShelf.exe --sort
"<file>"``), which hands the command to the running app. On Windows 11 the entries are under
"Show more options". The installer removes the same keys on uninstall
(packaging/autoshelf.iss lists them; a test keeps both in step).
"""

import contextlib
import sys
from pathlib import Path
from typing import Protocol

from autoshelf.features import convert
from autoshelf.startup import app_command, app_executable

CLASSES = r"Software\Classes"
MENU_KEY = CLASSES + r"\*\shell\AutoShelf"
CONVERT_VERB = "AutoShelf.Convert"
# Every file type a "Convert with AutoShelf" menu may be added to (and removed from).
CONVERT_SUFFIXES = tuple(
    sorted(
        convert.IMAGE_SUFFIXES | convert.HEIC_SUFFIXES | {".pdf"} | frozenset(convert.OFFICE_KINDS)
    )
)
# verb key, label, option, which selections it is offered for
MENU = (
    ("1sort", "Sort with AutoShelf", "--sort", "Player"),  # any number of files
    ("2why", "Why is this here?", "--why", "Single"),  # one file
    ("3compress", "Compress", "--compress", "Player"),
)


class ExplorerError(Exception):
    """The menu couldn't be read or changed."""


class Registry(Protocol):
    """The few operations on HKEY_CURRENT_USER needed (a fake in tests)."""

    def get(self, key: str, name: str = "") -> str | None: ...

    def set(self, key: str, name: str, value: str) -> None: ...

    def delete_tree(self, key: str) -> None: ...


class WindowsRegistry:
    """The current user's part of the real registry."""

    def get(self, key: str, name: str = "") -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                value, _ = winreg.QueryValueEx(handle, name)
                return str(value)
        except FileNotFoundError:
            return None

    def set(self, key: str, name: str, value: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key) as handle:
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)

    def delete_tree(self, key: str) -> None:
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_WRITE
            ) as handle:
                while True:
                    try:
                        child = winreg.EnumKey(handle, 0)
                    except OSError:
                        break
                    self.delete_tree(rf"{key}\{child}")
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def default_registry() -> Registry:
    """The real registry (tests replace this function)."""
    return WindowsRegistry()


def convert_key(suffix: str) -> str:
    """The key of the "Convert with AutoShelf" menu of one file type."""
    return rf"{CLASSES}\SystemFileAssociations\{suffix}\shell\{CONVERT_VERB}"


def _registry(registry: Registry | None) -> Registry:
    if registry is None and sys.platform != "win32":
        raise ExplorerError("Explorer's menu is only available on Windows")
    return registry or default_registry()


def is_added(registry: Registry | None = None) -> bool:
    """True if AutoShelf is in Explorer's right-click menu."""
    if registry is None and sys.platform != "win32":
        return False
    try:
        return _registry(registry).get(MENU_KEY, "MUIVerb") is not None
    except OSError as exc:
        raise ExplorerError(f"Couldn't read Explorer's menu: {exc}") from exc


def program(registry: Registry | None = None) -> Path | None:
    """The program the menu starts (AutoShelf.exe, or pythonw.exe from source), if added."""
    try:
        command = _registry(registry).get(rf"{MENU_KEY}\shell\1sort\command")
    except OSError as exc:
        raise ExplorerError(f"Couldn't read Explorer's menu: {exc}") from exc
    if not command or not command.startswith('"'):
        return None
    return Path(command[1:].split('"', 1)[0])


def is_broken(registry: Registry | None = None) -> bool:
    """True if the menu is added but its program is gone (AutoShelf was moved, or a copy
    was deleted without turning the menu off)."""
    if not is_added(registry):
        return False
    started = program(registry)
    return started is None or not started.exists()


def add(registry: Registry | None = None, *, command: str | None = None, targets=None) -> None:
    """Add (or update) AutoShelf's entries in Explorer's right-click menu.

    ``targets(path)`` lists the formats a file can be converted to (convert.targets by
    default); a file type gets a Convert menu only for formats available on this PC."""
    registry = _registry(registry)
    command = command or app_command()
    targets = targets or convert.targets
    try:
        _remove(registry)  # formats may have changed (e.g. Office installed since)
        registry.set(MENU_KEY, "MUIVerb", "AutoShelf")
        registry.set(MENU_KEY, "SubCommands", "")
        registry.set(MENU_KEY, "MultiSelectModel", "Player")
        icon = _icon()
        if icon:
            registry.set(MENU_KEY, "Icon", icon)
        for verb, label, option, model in MENU:
            key = rf"{MENU_KEY}\shell\{verb}"
            registry.set(key, "MUIVerb", label)
            registry.set(key, "MultiSelectModel", model)
            registry.set(rf"{key}\command", "", _entry(command, option))
        for suffix in CONVERT_SUFFIXES:
            formats = targets(Path(f"file{suffix}"))
            if formats:
                _add_convert(registry, suffix, formats, command, icon)
    except OSError as exc:
        raise ExplorerError(f"Couldn't change Explorer's menu: {exc}") from exc
    _shell_changed(registry)


def remove(registry: Registry | None = None) -> None:
    """Take AutoShelf's entries out of Explorer's right-click menu."""
    registry = _registry(registry)
    try:
        _remove(registry)
    except OSError as exc:
        raise ExplorerError(f"Couldn't change Explorer's menu: {exc}") from exc
    _shell_changed(registry)


def _remove(registry: Registry) -> None:
    registry.delete_tree(MENU_KEY)
    for suffix in CONVERT_SUFFIXES:
        registry.delete_tree(convert_key(suffix))


def _add_convert(
    registry: Registry, suffix: str, formats: list[str], command: str, icon: str | None
) -> None:
    key = convert_key(suffix)
    registry.set(key, "MUIVerb", "Convert with AutoShelf")
    registry.set(key, "SubCommands", "")
    registry.set(key, "MultiSelectModel", "Player")
    if icon:
        registry.set(key, "Icon", icon)
    for number, to in enumerate(formats, start=1):
        entry = rf"{key}\shell\{number}{to}"
        registry.set(entry, "MUIVerb", convert.label(to))
        registry.set(entry, "MultiSelectModel", "Player")
        registry.set(rf"{entry}\command", "", _entry(command, f"--convert {to}"))


def _entry(command: str, option: str) -> str:
    return f'{command} {option} "%1"'


def _icon() -> str | None:
    app = app_executable()
    return f"{app},0" if app is not None else None


def _shell_changed(registry: Registry) -> None:
    """Tell Explorer that file type settings changed, so menus update without a restart."""
    if sys.platform != "win32" or not isinstance(registry, WindowsRegistry):
        return
    import ctypes

    shcne_assocchanged, shcnf_idlist = 0x08000000, 0
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.shell32.SHChangeNotify(shcne_assocchanged, shcnf_idlist, None, None)
