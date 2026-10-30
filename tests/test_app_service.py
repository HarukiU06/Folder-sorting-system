"""The desktop app's logic, tested without Qt."""

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf import wording
from autoshelf.app_service import AppService, watch_lock
from autoshelf.cli import main
from autoshelf.config import AppPaths, ConfigError
from autoshelf.db import repository
from autoshelf.ml.embedder import EmbedderError
from autoshelf.pipeline import Outcome

TIMEOUT = 10.0


class Outcomes:
    """Collects outcomes reported by the watcher thread."""

    def __init__(self) -> None:
        self.items: list[Outcome] = []
        self._changed = threading.Condition()

    def __call__(self, outcome: Outcome) -> None:
        with self._changed:
            self.items.append(outcome)
            self._changed.notify_all()

    def wait_for(self, name: str) -> Outcome:
        with self._changed:
            assert self._changed.wait_for(
                lambda: any(o.path.name == name for o in self.items), timeout=TIMEOUT
            ), f"no outcome for {name}: {self.items}"
            return next(o for o in self.items if o.path.name == name)


@pytest.fixture
def shelf(root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank")
    (root / "Recipes").mkdir()
    return root


@pytest.fixture
def service(paths: AppPaths, shelf: Path) -> Iterator[AppService]:
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    settings = replace(
        app.settings(),
        root=str(shelf),
        auto_threshold=0.3,
        auto_margin=0.1,
        stable_seconds=0.3,
    )
    app.save_settings(settings)
    app.describe("Finance", "invoice receipt tax bank")
    app.describe("Recipes", "recipe cooking baking oven")
    yield app
    app.release()


@pytest.fixture
def outcomes(service: AppService) -> Outcomes:
    recorder = Outcomes()
    service.on_outcome = recorder
    return recorder


def test_start_needs_the_lock(service: AppService) -> None:
    with pytest.raises(RuntimeError, match="acquire"):
        service.start()


def test_app_and_cli_watch_exclude_each_other(
    service: AppService, shelf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert service.acquire()
    assert main(["watch", "--root", str(shelf)]) == 1
    assert "already sorting" in capsys.readouterr().err

    service.release()
    other = watch_lock(service.paths)
    assert other.acquire()
    assert not service.acquire()
    other.release()


def test_start_without_a_root_does_nothing(paths: AppPaths) -> None:
    app = AppService(paths)
    assert app.acquire()
    assert app.start() is False
    assert not app.watching
    app.release()


def test_watching_sorts_new_files(service: AppService, shelf: Path, outcomes) -> None:
    assert service.acquire() and service.start()
    make(shelf / "invoice 2025.txt", "invoice total tax due")

    outcome = outcomes.wait_for("invoice 2025.txt")

    assert outcome.dst == shelf / "Finance" / "invoice 2025.txt"
    assert not outcome.needs_decision


def test_unclear_file_needs_a_decision(service: AppService, shelf: Path, outcomes) -> None:
    assert service.acquire() and service.start()
    make(shelf / "notes.txt", "call mom buy milk")
    outcome = outcomes.wait_for("notes.txt")
    assert outcome.needs_decision
    assert outcome.dst == shelf / "_Inbox" / "notes.txt"


def test_pause_holds_files_until_resumed(service: AppService, shelf: Path, outcomes) -> None:
    assert service.acquire() and service.start()
    service.set_paused(True)
    src = make(shelf / "invoice 2025.txt", "invoice total tax due")
    time.sleep(1.5)
    assert src.exists()
    assert outcomes.items == []
    assert service.settings().paused

    service.set_paused(False)

    assert outcomes.wait_for("invoice 2025.txt").dst.parent == shelf / "Finance"
    assert not service.settings().paused


def test_restart_while_paused_stays_paused(service: AppService, shelf: Path, outcomes) -> None:
    assert service.acquire() and service.start()
    service.set_paused(True)
    assert service.restart()
    src = make(shelf / "invoice 2025.txt", "invoice total tax due")
    time.sleep(1.5)
    assert src.exists()
    service.set_paused(False)
    outcomes.wait_for("invoice 2025.txt")


def test_status_messages_are_reported(service: AppService) -> None:
    messages: list[str] = []
    ready = threading.Event()

    def status(message: str) -> None:
        messages.append(message)
        if message.startswith("Ready"):
            ready.set()

    service.on_status = status
    assert service.acquire() and service.start()
    assert ready.wait(TIMEOUT)
    assert messages[0] == "Loading the language model..."
    assert messages[1].startswith("Downloading the language model (about 470 MB")  # not cached
    assert messages[-1] == "Ready: 2 folders, 1 files in them"


def test_loose_files_counts_what_would_be_sorted(service: AppService, shelf: Path) -> None:
    make(shelf / "a.pdf")
    make(shelf / "~$temp.docx")
    make(shelf / "_Inbox" / "from iPhone.pdf")
    waiting = make(shelf / "_Inbox" / "already waiting.pdf")
    with repository_conn(service) as conn:
        file_id = repository.upsert_file(conn, waiting, sha256="x", size=1, mtime=1.0)
        repository.set_file_status(conn, file_id, "inbox")
    assert service.loose_files(shelf) == 2


def test_inbox_lists_suggestions(service: AppService, shelf: Path) -> None:
    make(shelf / "_Inbox" / "tax invoice.txt", "invoice tax")
    (item,) = service.inbox()
    assert item.path.name == "tax invoice.txt"
    assert item.excerpt == "invoice tax"
    assert item.classification.top(1)[0].folder == "Finance"


def test_inbox_without_a_model_still_lists_files(paths: AppPaths, shelf: Path) -> None:
    def broken(model: str, cache_dir: Path):
        raise EmbedderError("offline")

    app = AppService(paths, loader=broken)
    app.set_root(shelf)
    make(shelf / "_Inbox" / "notes.txt", "call mom")
    (item,) = app.inbox()
    assert item.classification is None
    assert item.excerpt == "call mom"
    assert "can't be sorted" in wording.reason_text(item.reason)


def test_assign_undo_and_history(service: AppService, shelf: Path) -> None:
    src = make(shelf / "_Inbox" / "bread.txt", "flour")

    moved = service.assign(src, "recipes")
    assert moved.dst == shelf / "Recipes" / "bread.txt"
    assert service.history()[0].reason == "user"

    service.undo(moved.history_id)
    assert src.exists()
    assert service.history()[0].action == "undo"

    again = service.assign(src, "Recipes")
    (undone,) = service.undo_last(1)
    assert undone.history_id != again.history_id
    assert src.exists()


def test_own_moves_are_announced_to_the_running_watcher(service: AppService) -> None:
    assert service._before_rename() is None
    assert service.acquire() and service.start()
    before_rename = service._before_rename()
    target = service.root() / "Recipes" / "x.txt"
    before_rename(Path("src"), target)
    assert service._service.self_moves.is_expected(target)


def test_folders_and_descriptions(service: AppService) -> None:
    folders = {folder.key: folder for folder in service.folders()}
    assert folders["Finance"].files == 1
    assert folders["Recipes"].description == "recipe cooking baking oven"
    assert service.describe("recipes", "  cakes ") == "Recipes"
    assert {f.key: f.description for f in service.folders()}["Recipes"] == "cakes"


def test_set_root_validates(service: AppService, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        service.set_root(tmp_path / "missing")
    new_root = tmp_path / "Other shelf"
    new_root.mkdir()
    assert service.set_root(new_root) == new_root.resolve()
    assert service.root() == new_root.resolve()


def test_actions_need_a_root(paths: AppPaths) -> None:
    with pytest.raises(ConfigError, match="Choose the folder"):
        AppService(paths).folders()


def repository_conn(service: AppService):
    return service._connect()


def test_a_folders_files_by_name(paths: AppPaths, root: Path) -> None:
    make(root / "Finance" / "a.txt", "a")
    make(root / "Finance" / "Taxes" / "b.txt", "b")
    service = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    service.set_root(root)
    assert [path.name for path in service.folder_files("Finance")] == ["a.txt"]
    assert [path.name for path in service.folder_files("Finance/Taxes")] == ["b.txt"]


# --- editing folders from the Library, and re-sorting -----------------------------------


def test_folder_changes_through_the_service(service: AppService, shelf: Path) -> None:
    invoice = shelf / "Finance" / "invoice 2024.txt"
    assert service.make_folder("Finance", "Taxes") == "Finance/Taxes"

    moved = service.move_items([invoice], [], "Finance/Taxes")
    assert [change.dst for change in moved.done] == [shelf / "Finance" / "Taxes" / invoice.name]
    renamed = service.rename_folder("Finance/Taxes", "Tax")
    assert renamed.dst == shelf / "Finance" / "Tax"
    service.move_items([], ["Finance/Tax"], "Recipes")
    assert (shelf / "Recipes" / "Tax" / invoice.name).exists()

    binned = service.trash_items([], ["Recipes/Tax"])
    assert (shelf / "_Trash" / "Tax" / invoice.name).exists()
    service.undo_changes(binned.history_ids)
    assert (shelf / "Recipes" / "Tax" / invoice.name).exists()


def test_resort_and_move_maybes(service: AppService, shelf: Path) -> None:
    make(shelf / "Recipes" / "pancake notes.txt", "flour eggs milk")
    make(shelf / "Recipes" / "loaf.txt", "bread dough yeast knead")
    (shelf / "Recipes" / "Notes").mkdir()
    (shelf / "Recipes" / "Breads").mkdir()
    service.describe("Recipes/Breads", "bread dough yeast knead")

    result = service.resort("Recipes")

    assert [item.dst.name for item in result.moved] == ["pancake notes.txt"]
    assert [item.path.name for item in result.plan.maybes] == ["loaf.txt"]
    chosen = service.move_maybes(result.plan, result.plan.maybes)
    assert (shelf / "Recipes" / "Breads" / "loaf.txt").exists()
    assert [item.dst.name for item in chosen.moved] == ["loaf.txt"]


# --- updates -------------------------------------------------------------------------------


def _github(version: str):
    from fakes import github_release

    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return json.dumps(github_release(version)).encode()

    fetch.asked = asked
    return fetch


def test_update_check_is_recorded_and_due_again_after_a_day(service: AppService) -> None:
    now = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    assert service.update_due(now=now)
    service.fetch = _github("9.9.9")

    found = service.check_for_update(now=now)

    assert found is not None and found.version == "9.9.9"
    assert service.settings().update_checked == now.isoformat(timespec="seconds")
    assert not service.update_due(now=now + timedelta(hours=23))
    assert service.update_due(now=now + timedelta(hours=25))
    service.save_settings(replace(service.settings(), check_updates=False))
    assert not service.update_due(now=now + timedelta(hours=25))
    service.fetch = _github("0.0.1")
    assert service.check_for_update(now=now) is None  # older than this AutoShelf


def test_each_new_version_is_announced_once(service: AppService) -> None:
    service.fetch = _github("9.9.9")
    release = service.check_for_update()
    assert release is not None

    assert service.announce_update(release)
    assert not service.announce_update(release)
    service.fetch = _github("10.0.0")
    newer = service.check_for_update()
    assert service.announce_update(newer)
    assert service.settings().update_notified == "10.0.0"


def test_updates_download_into_the_cache(service: AppService, monkeypatch) -> None:
    from autoshelf import updates

    service.fetch = _github("9.9.9")
    release = service.check_for_update()
    seen = []

    def download(found, folder, *, progress=None, cancelled=lambda: False):
        seen.append((found, folder))
        return folder / found.installer_name

    monkeypatch.setattr(updates, "download", download)
    path = service.download_update(release)

    assert seen == [(release, service.paths.cache_dir / "updates")]
    assert path == service.paths.cache_dir / "updates" / "AutoShelf-9.9.9-setup.exe"
    assert service.update_mode() == updates.SOURCE  # the tests run from source
