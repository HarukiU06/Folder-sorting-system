"""Shared fixtures. Every test runs against temporary folders, never real user data."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from autoshelf.config import HOME_ENV_VAR, AppPaths, get_app_paths
from autoshelf.db.connection import open_database


@pytest.fixture(autouse=True)
def app_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point AutoShelf's data folder at a temp dir so tests never touch %APPDATA%."""
    home = tmp_path / "app home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    return home


@pytest.fixture
def paths(app_home: Path) -> AppPaths:
    return get_app_paths(app_home)


@pytest.fixture
def conn(paths: AppPaths) -> Iterator[sqlite3.Connection]:
    connection = open_database(paths.db_path)
    yield connection
    connection.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A fake library root whose name has spaces and Unicode, like real iCloud folders."""
    folder = tmp_path / "My Shelf 本棚"
    folder.mkdir()
    return folder.resolve()
