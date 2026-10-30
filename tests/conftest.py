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
def memory_keyring() -> Iterator[dict]:
    """Keep API keys in memory, so tests never touch the Windows Credential Manager."""
    import keyring
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    store: dict[tuple[str, str], str] = {}

    class MemoryKeyring(KeyringBackend):
        priority = 1

        def get_password(self, service, username):
            return store.get((service, username))

        def set_password(self, service, username, password):
            store[(service, username)] = password

        def delete_password(self, service, username):
            if store.pop((service, username), None) is None:
                raise PasswordDeleteError("not found")

    previous = keyring.get_keyring()
    keyring.set_keyring(MemoryKeyring())
    yield store
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def fake_run_key(monkeypatch: pytest.MonkeyPatch) -> dict:
    """A fake registry Run key, so tests never make AutoShelf start with Windows."""
    from autoshelf import startup

    values: dict[str, str] = {}

    class FakeRunKey:
        def get(self, name):
            return values.get(name)

        def set(self, name, command):
            values[name] = command

        def delete(self, name):
            values.pop(name, None)

    monkeypatch.setattr(startup, "default_run_key", FakeRunKey)
    return values


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch):
    """A fake HKEY_CURRENT_USER, so tests never change Explorer's menu or file types."""
    from fakes import FakeRegistry

    from autoshelf import explorer

    registry = FakeRegistry()
    monkeypatch.setattr(explorer, "default_registry", lambda: registry)
    return registry


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM providers and update checks must never reach the internet (or a local Ollama)
    from tests."""
    from autoshelf import updates
    from autoshelf.llm import base

    def refuse(request, timeout):
        raise AssertionError(f"tests must not use the network ({request.full_url})")

    monkeypatch.setattr(base, "_urlopen", refuse)
    monkeypatch.setattr(updates, "_urlopen", refuse)


@pytest.fixture(autouse=True)
def reset_read_guard() -> Iterator[None]:
    """No test inherits the crash guard (``extract.guard``) another test started."""
    from autoshelf.extract import guard

    yield
    guard.stop()


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
def fake_model(monkeypatch: pytest.MonkeyPatch):
    """Replace the language model with the fake one for code that loads it by name."""
    from fakes import FakeEmbedder

    from autoshelf.ml import embedder as embedder_module

    model = FakeEmbedder()
    monkeypatch.setattr(embedder_module, "load_embedder", lambda name, cache_dir: model)
    return model


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
