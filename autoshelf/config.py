"""Application paths, layout validation and user settings.

App-internal data (database, caches, logs) always lives in ``%APPDATA%\\AutoShelf``.
It must never be inside the watched root or inside iCloud Drive, because syncing a
live SQLite file corrupts it.
"""

import math
import os
import sqlite3
from dataclasses import dataclass, field, fields
from pathlib import Path

from autoshelf.db import repository

APP_NAME = "AutoShelf"
HOME_ENV_VAR = "AUTOSHELF_HOME"
DB_FILENAME = "autoshelf.db"

DEFAULT_STABLE_SECONDS = 3.0
# Multilingual, so Japanese and English files (and folder names) can be compared.
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# A file is moved automatically only if its best folder scores at least the threshold and
# beats every unrelated folder by the margin. Tuned for DEFAULT_EMBEDDING_MODEL: another
# model scores on a different scale, so change these together with the model.
DEFAULT_AUTO_THRESHOLD = 0.40
DEFAULT_AUTO_MARGIN = 0.10


@dataclass(frozen=True)
class ConfidenceLevel:
    """A named (threshold, margin) pair for the "cautious <-> automatic" slider."""

    name: str
    threshold: float
    margin: float


CONFIDENCE_LEVELS = (
    ConfidenceLevel("Very cautious", 0.50, 0.15),
    ConfidenceLevel("Cautious", 0.45, 0.12),
    ConfidenceLevel("Balanced", DEFAULT_AUTO_THRESHOLD, DEFAULT_AUTO_MARGIN),
    ConfidenceLevel("Automatic", 0.35, 0.08),
    ConfidenceLevel("Very automatic", 0.30, 0.06),
)


def confidence_level(threshold: float, margin: float) -> int | None:
    """Index of the preset in CONFIDENCE_LEVELS with these values, or None if custom."""
    for index, level in enumerate(CONFIDENCE_LEVELS):
        if math.isclose(level.threshold, threshold) and math.isclose(level.margin, margin):
            return index
    return None


DEFAULT_IGNORE_PATTERNS = (
    "~$*",
    "*.tmp",
    "*.crdownload",
    "*.part",
    "*.download",
    "desktop.ini",
    ".DS_Store",
    "Thumbs.db",
)

# Folder names used by iCloud for Windows ("iCloudDrive" is the default).
_ICLOUD_DIR_NAMES = frozenset({"iclouddrive", "icloud drive"})


class ConfigError(Exception):
    """Raised when paths or settings would break a safety rule."""


@dataclass(frozen=True)
class AppPaths:
    """Locations of AutoShelf's own files."""

    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / DB_FILENAME

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def model_dir(self) -> Path:
        return self.cache_dir / "models"


def default_home() -> Path:
    """Return the app data folder: ``$AUTOSHELF_HOME``, else ``%APPDATA%\\AutoShelf``."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def is_in_icloud(path: Path) -> bool:
    """Return True if any component of ``path`` is an iCloud Drive folder."""
    return any(part.casefold() in _ICLOUD_DIR_NAMES for part in Path(path).parts)


def get_app_paths(home: Path | None = None) -> AppPaths:
    """Resolve the app data folder, refuse unsafe locations, and create its subfolders."""
    resolved = (home or default_home()).expanduser().resolve()
    if is_in_icloud(resolved):
        raise ConfigError(
            f"AutoShelf's data folder must not be inside iCloud Drive: {resolved}. "
            f"Set {HOME_ENV_VAR} to a local folder."
        )
    paths = AppPaths(resolved)
    for folder in (paths.home, paths.log_dir, paths.cache_dir):
        folder.mkdir(parents=True, exist_ok=True)
    return paths


def validate_root(root: Path, paths: AppPaths) -> Path:
    """Check that ``root`` is a safe folder to sort and return it fully resolved.

    Refuses missing paths, files, drive roots, the user's home folder, the Windows
    folder, and any root that would contain AutoShelf's own data folder.
    """
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ConfigError(f"Root folder does not exist: {root}") from None
    if not resolved.is_dir():
        raise ConfigError(f"Root is not a folder: {resolved}")
    if resolved.parent == resolved:
        raise ConfigError(f"Refusing to use a whole drive as the root: {resolved}")
    if resolved == Path.home().resolve():
        raise ConfigError("Refusing to use your home folder as the root; pick a dedicated folder.")
    system_root = os.environ.get("SYSTEMROOT")
    if system_root and resolved.is_relative_to(Path(system_root).resolve()):
        raise ConfigError(f"Refusing to use a Windows system folder as the root: {resolved}")
    if paths.home.is_relative_to(resolved):
        raise ConfigError(
            f"AutoShelf's data folder ({paths.home}) must not be inside the root ({resolved})."
        )
    return resolved


def validate_outside_folder(folder: Path, root: Path, paths: AppPaths) -> Path:
    """Check a folder outside the root that files may be taken from, and resolve it.

    Used for watched folders and for organizing another folder: it must be a real
    folder on the same drive as the root (moves are renames, never copy-and-delete), and
    must not be or contain the root, AutoShelf's data folder, the home folder, a whole
    drive or a Windows system folder.
    """
    try:
        resolved = Path(folder).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise ConfigError(f"Folder does not exist: {folder}") from None
    if not resolved.is_dir():
        raise ConfigError(f"Not a folder: {resolved}")
    if resolved.parent == resolved:
        raise ConfigError(f"Refusing to use a whole drive: {resolved}")
    if resolved == Path.home().resolve():
        raise ConfigError("Refusing to use your whole home folder; pick a folder inside it.")
    system_root = os.environ.get("SYSTEMROOT")
    if system_root and resolved.is_relative_to(Path(system_root).resolve()):
        raise ConfigError(f"Refusing to use a Windows system folder: {resolved}")
    if resolved.is_relative_to(root) or root.is_relative_to(resolved):
        raise ConfigError(f"{resolved} is inside the folder AutoShelf sorts, or contains it.")
    if paths.home.is_relative_to(resolved) or resolved.is_relative_to(paths.home):
        raise ConfigError(f"{resolved} holds AutoShelf's own data folder.")
    if os.stat(resolved).st_dev != os.stat(root).st_dev:
        raise ConfigError(
            f"{resolved} is on another drive than {root}; AutoShelf only moves files within "
            "one drive (it never copies and deletes them)."
        )
    return resolved


# New files with these extensions are taken from watched folders (documents only, so
# installers and archives in Downloads are left alone).
DEFAULT_WATCHED_EXTENSIONS = (
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".epub",
    ".txt",
    ".md",
    ".rtf",
    ".csv",
    ".ipynb",
)


@dataclass
class Settings:
    """User settings, persisted as JSON values in the ``settings`` table."""

    root: str | None = None
    stable_seconds: float = DEFAULT_STABLE_SECONDS
    ignore_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATTERNS))
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    auto_threshold: float = DEFAULT_AUTO_THRESHOLD
    auto_margin: float = DEFAULT_AUTO_MARGIN
    paused: bool = False  # automatic sorting paused (files wait until resumed)
    notifications: bool = True  # notify when a file needs a decision
    # Opt-in folders outside the root (e.g. Downloads): new files there with one of
    # ``watched_extensions`` are moved into the root and sorted. None by default.
    watched_folders: list[str] = field(default_factory=list)
    watched_extensions: list[str] = field(default_factory=lambda: list(DEFAULT_WATCHED_EXTENSIONS))


def load_settings(conn: sqlite3.Connection) -> Settings:
    """Load settings from the database, using defaults for anything not stored yet."""
    settings = Settings()
    for f in fields(Settings):
        stored = repository.get_setting(conn, f.name)
        if stored is not None:
            setattr(settings, f.name, stored)
    return settings


def save_settings(conn: sqlite3.Connection, settings: Settings) -> None:
    """Persist every setting to the database."""
    for f in fields(Settings):
        repository.set_setting(conn, f.name, getattr(settings, f.name))
