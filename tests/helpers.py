"""Small helpers shared by tests."""

import sys
from pathlib import Path

import pytest

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows file semantics")


def make(path: Path, content: str = "hello") -> Path:
    """Create a file (and its parent folders) with ``content``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def set_windows_attributes(path: Path, attributes: int) -> None:
    """Set Windows file attributes (e.g. hidden = 0x2, system = 0x4)."""
    import ctypes

    if not ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes):
        raise ctypes.WinError()
