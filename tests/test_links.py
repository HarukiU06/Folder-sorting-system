"""Notification buttons (Phase 14): autoshelf: links, single-use offers, the registered link
and the notifications themselves."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeEmbedder, FakeRegistry
from helpers import make

from autoshelf import explorer, links, toasts
from autoshelf.actions.inbox import AssignError
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.links import Link, LinkError
from autoshelf.ml.classifier import Classification, FolderScore
from autoshelf.pipeline import Outcome, OutcomeKind
from autoshelf.reasons import LOW_SCORE, reason
from autoshelf.ui.app import notification_note

TOKEN = "A" * 24

# --- links ---------------------------------------------------------------------------------------


def test_links_and_back() -> None:
    url = links.link(links.ASSIGN, TOKEN, 2)
    assert url == f"autoshelf:assign?offer={TOKEN}&choice=2"
    assert links.parse(url) == Link(links.ASSIGN, TOKEN, 2)
    assert links.parse("autoshelf:inbox") == Link(links.INBOX)
    assert links.parse(f"AutoShelf:Inbox/?offer={TOKEN}") == Link(links.INBOX, TOKEN)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/?x=autoshelf:inbox",
        "autoshelf:delete?offer=" + TOKEN,
        f"autoshelf:assign?offer={TOKEN}",  # which folder?
        f"autoshelf:assign?offer={TOKEN}&choice=3",
        f"autoshelf:assign?offer={TOKEN}&choice=-1",
        f"autoshelf:assign?offer={TOKEN}&choice=0&choice=1",
        f"autoshelf:assign?offer={TOKEN}&choice=0&path=C:/x",
        "autoshelf:assign?offer=short&choice=0",
        "autoshelf:inbox?offer=../../etc",
        'autoshelf:x" --compress C:\\secret',
    ],
)
def test_strange_links_are_refused(url: str) -> None:
    with pytest.raises(LinkError):
        links.parse(url)


def test_offers_are_single_use_and_expire(conn, tmp_path: Path) -> None:
    file = tmp_path / "_Inbox" / "scan.pdf"
    token = links.make_offer(conn, file, ["Finance", "Recipes", "Travel", "Extra"])
    offer = links.get_offer(conn, token)
    assert (offer.path, offer.folders) == (file, ("Finance", "Recipes", "Travel"))
    links.use_offer(conn, token)
    with pytest.raises(LinkError, match="already been answered"):
        links.get_offer(conn, token)
    with pytest.raises(LinkError):
        links.get_offer(conn, "B" * 24)  # never made

    old = links.make_offer(conn, file, ["Finance"])
    later = datetime.now(UTC) + timedelta(days=links.OFFER_DAYS + 1)
    with pytest.raises(LinkError, match="out of date"):
        links.get_offer(conn, old, now=later)
    with pytest.raises(LinkError, match="already been answered"):
        links.get_offer(conn, old)  # forgotten


def test_old_offers_are_forgotten(conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "MAX_OFFERS", 3)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    tokens = [
        links.make_offer(conn, tmp_path / f"{n}.pdf", ["A"], now=start + timedelta(minutes=n))
        for n in range(5)
    ]
    kept = conn.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'offer:%'").fetchone()[0]
    assert kept == 3
    assert links.get_offer(conn, tokens[-1], now=start).path.name == "4.pdf"
    with pytest.raises(LinkError):
        links.get_offer(conn, tokens[0], now=start)


# --- answering from a notification --------------------------------------------------------------


@pytest.fixture
def service(paths: AppPaths, root: Path):
    (root / "Finance").mkdir()
    (root / "Recipes").mkdir()
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(root)
    yield app
    app.release()


def test_a_button_puts_the_file_away_once(service: AppService, root: Path) -> None:
    waiting = make(root / "_Inbox" / "receipt.txt", "receipt")
    token = service.offer(waiting, ["Recipes", "Finance"])
    assert service.offered_file(token) == waiting

    moved, folder = service.answer_offer(token, 1)

    assert (folder, moved.dst) == ("Finance", root / "Finance" / "receipt.txt")
    assert service.history()[0].reason == "user"
    with pytest.raises(LinkError):
        service.answer_offer(token, 1)  # a second click does nothing
    assert service.offered_file(token) is None


def test_a_button_for_a_file_already_put_away(service: AppService, root: Path) -> None:
    waiting = make(root / "_Inbox" / "receipt.txt", "receipt")
    token = service.offer(waiting, ["Finance"])
    service.assign(waiting, "Recipes")  # answered in the app meanwhile
    with pytest.raises(AssignError):
        service.answer_offer(token, 0)
    with pytest.raises(LinkError, match="offered no folder"):
        service.answer_offer(service.offer(waiting, ["Finance"]), 2)


# --- the notification -------------------------------------------------------------------------


def outcome(path: Path, folders: list[str]) -> Outcome:
    scores = tuple(FolderScore(name, 0.5 - n / 10, 0.5) for n, name in enumerate(folders))
    classification = Classification(scores, None, reason(LOW_SCORE))
    return Outcome(OutcomeKind.WAITING, path, "", dst=path, classification=classification)


def test_a_notification_offers_the_best_folders(tmp_path: Path) -> None:
    offered = []

    def offer(path: Path, folders: list[str]) -> str:
        offered.append((path, folders))
        return TOKEN

    file = tmp_path / "_Inbox" / "lecture.pdf"
    note = notification_note([outcome(file, ["ML", "Finance", "Recipes", "Travel"])], offer)

    assert offered == [(file, ["ML", "Finance", "Recipes"])]
    assert note.title == "Where should lecture.pdf go?"
    assert note.buttons == tuple(
        (name, links.link(links.ASSIGN, TOKEN, n))
        for n, name in enumerate(["ML", "Finance", "Recipes"])
    )
    assert note.launch == links.link(links.INBOX, TOKEN)
    assert note.dismiss == "Leave in Inbox"

    several = notification_note([outcome(file, ["ML"]), outcome(tmp_path / "b.pdf", [])], offer)
    assert several.buttons == () and several.launch == "autoshelf:inbox"
    no_guess = notification_note([outcome(file, [])], offer)
    assert no_guess.buttons == () and len(offered) == 1


def test_long_folder_names_are_shortened_on_buttons() -> None:
    assert toasts.short("Finance") == "Finance"
    label = toasts.short("University/Year 3/Machine Learning/Unsupervised methods")
    assert len(label) == toasts.MAX_LABEL and label.endswith("Unsupervised methods")


# --- registering the link ---------------------------------------------------------------------


def test_the_link_in_the_registry(fake_registry: FakeRegistry, tmp_path: Path) -> None:
    program = make(tmp_path / "Auto Shelf ★" / "AutoShelf.exe", "")
    assert not explorer.link_ready()
    assert toasts.default_toaster() is None  # no link: the tray's notifications

    explorer.add_link(command=f'"{program}"')

    assert fake_registry.get(explorer.LINK_KEY, "URL Protocol") == ""
    command = fake_registry.get(explorer.LINK_KEY + r"\shell\open\command")
    assert command == f'"{program}" --url "%1"'
    assert fake_registry.get(explorer.APP_ID_KEY, "DisplayName") == "AutoShelf"
    assert explorer.link_program() == program and explorer.link_ready()
    program.unlink()
    assert not explorer.link_ready()  # a link to a program that's gone doesn't count
    explorer.remove_link()
    assert fake_registry.keys == {}


def test_cli_turns_notification_buttons_on_and_off(
    fake_registry: FakeRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "--notification-buttons", "on"]) == 0
    assert "Notification buttons: on" in capsys.readouterr().out
    assert main(["config", "--notification-buttons", "off"]) == 0
    assert "Notification buttons: off" in capsys.readouterr().out
    assert fake_registry.keys == {}
