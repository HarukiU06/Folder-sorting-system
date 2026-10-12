"""Shared fixtures. Every test runs against temporary folders, never real user data."""

import os

# Qt windows are drawn off screen, so tests never pop up on the desktop. This must happen
# before anything imports PySide6.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import logging
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


@pytest.fixture(autouse=True)
def no_real_model(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make loading the real language model fail, so no test downloads it by accident.

    Tests that need a model inject the fake one; the opt-in real-model test is marked.
    """
    if request.node.get_closest_marker("real_model"):
        return
    from autoshelf.ml import embedder

    def refuse(model: str, cache_dir: Path):
        raise embedder.EmbedderError("tests must not load the real model")

    monkeypatch.setattr(embedder, "load_embedder", refuse)


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """Close handlers that ``cli.main`` attaches, so log files in temp dirs aren't held open."""
    yield
    logger = logging.getLogger("autoshelf")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.NOTSET)


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
