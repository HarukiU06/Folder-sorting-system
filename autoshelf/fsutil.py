"""Small filesystem helpers: hashing, containment checks, hidden files and lock probing."""

import hashlib
import stat
import sys
from pathlib import Path

_HASH_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents.

    Reading an iCloud placeholder downloads it, so call this off the UI thread.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def is_within(path: Path, root: Path) -> bool:
    """Return True if ``path`` is ``root`` or below it.

    Both paths must already be resolved. Comparison is case-insensitive on Windows.
    """
    return path == root or path.is_relative_to(root)


def is_hidden_or_system(path: Path) -> bool:
    """Return True for dotfiles and files with the Windows hidden or system attribute."""
    if path.name.startswith("."):
        return True
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL

    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    _SHARING_ERRORS = {32, 33}  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION

    def is_locked(path: Path) -> bool:
        """Return True if another process currently has the file open for writing.

        Opens the file for reading while refusing to share write access. Windows
        rejects that with a sharing violation if any other handle can write to or
        delete the file, e.g. a browser still downloading or iCloud still syncing.
        """
        handle = _CreateFileW(
            str(path),
            _GENERIC_READ,
            _FILE_SHARE_READ,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE:
            error = ctypes.get_last_error()
            if error in _SHARING_ERRORS:
                return True
            raise ctypes.WinError(error)
        _CloseHandle(handle)
        return False

else:

    def is_locked(path: Path) -> bool:
        """Return True if the file can't be opened for reading (non-Windows fallback)."""
        try:
            with open(path, "rb"):
                return False
        except PermissionError:
            return True
