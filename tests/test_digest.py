"""The weekly digest (Phase 15)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf import wording
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.features import digest


@pytest.fixture
def service(paths: AppPaths, root: Path):
    (root / "Finance").mkdir()
    (root / "Recipes").mkdir()
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(root)
    yield app
    app.release()


def put_away(service: AppService, root: Path, name: str, folder: str):
    return service.assign(make(root / "_Inbox" / name, name), folder)


def test_what_a_week_counts(service: AppService, root: Path) -> None:
    put_away(service, root, "a.txt", "Finance")
    moved = put_away(service, root, "b.txt", "Finance")
    service.correct(moved.history_id, "Recipes")  # the same file again: counted once
    undone = put_away(service, root, "c.txt", "Finance")
    service.undo(undone.history_id)  # back in the Inbox: not sorted, but waiting
    old = put_away(service, root, "d.txt", "Recipes")
    with service._connect() as conn:
        long_ago = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        conn.execute("UPDATE history SET ts = ? WHERE id = ?", (long_ago, old.history_id))
    make(root / "_Inbox" / "e.txt", "waiting")

    week = service.digest()

    assert (week.sorted, week.waiting) == (2, 2)
    assert week.learned == len(service.corrections())
    assert week.learned >= 1  # the correction
    assert not week.empty


def test_the_sentence() -> None:
    assert (
        wording.digest(23, 3, 1) == "This week: 23 sorted, 3 waiting in Inbox, 1 correction learned"
    )
    assert wording.digest(1, 0, 2).endswith("2 corrections learned")


def test_due_after_seven_days() -> None:
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    assert digest.due("", now) and digest.due("nonsense", now)
    assert not digest.due((now - timedelta(days=6, hours=23)).isoformat(), now)
    assert digest.due((now - timedelta(days=7)).isoformat(), now)
    assert digest.due("2026-10-01T09:00:00", now)  # stored without a time zone


def test_once_a_week_and_never_on_the_first_start(service: AppService, root: Path) -> None:
    now = datetime.now(UTC)
    first_start = now - timedelta(days=7, minutes=1)
    assert service.digest_due(first_start) is None  # first start: only remember the date
    assert service.settings().digest_shown
    put_away(service, root, "a.txt", "Finance")
    assert service.digest_due(first_start + timedelta(days=6)) is None
    week = service.digest_due(now)
    assert week is not None and week.sorted == 1
    assert service.digest_due(now + timedelta(days=1)) is None  # shown: next week


def test_a_quiet_week_or_the_setting_off_shows_nothing(service: AppService) -> None:
    start = datetime.now(UTC)
    service.digest_due(start)
    later = start + timedelta(days=8)
    assert service.digest_due(later) is None  # nothing happened
    assert service.settings().digest_shown == later.isoformat(timespec="seconds")
    service.save_settings(replace(service.settings(), weekly_digest=False, digest_shown=""))
    assert service.digest_due(later) is None
    assert service.settings().digest_shown == ""


def test_cli_digest(service: AppService, root: Path, capsys: pytest.CaptureFixture[str]) -> None:
    put_away(service, root, "a.txt", "Finance")
    assert main(["digest"]) == 0
    assert (
        "This week: 1 sorted, 0 waiting in Inbox, 1 correction learned"  # an Inbox answer
        in capsys.readouterr().out
    )
