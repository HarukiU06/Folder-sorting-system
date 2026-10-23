"""Commands for the running app (Phase 14): the wire format, batching, the command line, and
Explorer's right-click menu in the (fake) registry."""

import os
from pathlib import Path

import pytest
from fakes import FakeEmbedder, FakeRegistry
from helpers import make

from autoshelf import commands, config, explorer, wording
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.commands import Command, CommandError
from autoshelf.config import AppPaths, ConfigError
from autoshelf.db import repository
from autoshelf.ui import app as ui_app

# --- the wire format -----------------------------------------------------------------------------


def test_commands_survive_the_trip(tmp_path: Path) -> None:
    command = Command(commands.CONVERT, (str(tmp_path / "写真 1.png"), "C:\\a b\\c.jpg"), to="pdf")
    line = commands.encode(command)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert commands.decode(line[:-1]) == command

    reader = commands.LineReader()
    cut = line.index("真".encode()) + 1  # in the middle of a character
    assert reader.feed(line[:cut]) == []
    assert reader.feed(line[cut:] + commands.encode(Command(commands.SHOW))) == [
        line[:-1],
        b'{"action": "show"}',
    ]


@pytest.mark.parametrize(
    "line",
    [
        b"show",  # not JSON
        b"\xff\xfe",
        b"[1, 2]",
        b'{"action": "format_disk"}',
        b'{"action": "compress"}',  # no files
        b'{"action": "convert", "files": ["a.png"]}',  # no format
        b'{"action": "sort", "files": "a.txt"}',
        b'{"action": "url"}',
        b'{"action": 3}',
    ],
)
def test_nonsense_is_refused(line: bytes) -> None:
    with pytest.raises(CommandError):
        commands.decode(line)


def test_a_line_without_end_is_refused() -> None:
    reader = commands.LineReader(limit=10)
    with pytest.raises(CommandError, match="too long"):
        reader.feed(b"x" * 11)


def test_files_chosen_together_become_one_command() -> None:
    batch = commands.Batch()
    for name in ("a.png", "b.png", "a.png"):
        batch.add(Command(commands.COMPRESS, (name,)))
    batch.add(Command(commands.CONVERT, ("a.png",), to="pdf"))
    batch.add(Command(commands.CONVERT, ("b.png",), to="PDF"))
    batch.add(Command(commands.CONVERT, ("c.png",), to="jpg"))
    batch.add(Command(commands.URL, url="autoshelf:inbox"))
    batch.add(Command(commands.URL, url="autoshelf:inbox?x=1"))
    assert batch.take() == [
        Command(commands.COMPRESS, ("a.png", "b.png")),
        Command(commands.CONVERT, ("a.png", "b.png"), to="pdf"),
        Command(commands.CONVERT, ("c.png",), to="jpg"),
        Command(commands.URL, url="autoshelf:inbox"),
        Command(commands.URL, url="autoshelf:inbox?x=1"),
    ]
    assert batch.take() == []


# --- the command line ----------------------------------------------------------------------------


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(ui_app, "run_app", lambda **kwargs: calls.append(kwargs) or 0)
    return calls


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--sort", "a b.txt", "c.txt"], Command(commands.SORT, ("a b.txt", "c.txt"))),
        (["--why", "a.txt"], Command(commands.WHY, ("a.txt",))),
        (["--compress", "a.txt"], Command(commands.COMPRESS, ("a.txt",))),
        (["--convert", "pdf", "a.png"], Command(commands.CONVERT, ("a.png",), to="pdf")),
        (["--quit"], Command(commands.QUIT)),
        (["--url", "autoshelf:inbox"], Command(commands.URL, url="autoshelf:inbox")),
    ],
)
def test_the_app_takes_commands_on_its_command_line(
    started: list[dict], argv: list[str], expected: Command
) -> None:
    assert main(argv) == 0
    assert started[-1]["command"] == expected
    with pytest.raises(SystemExit) as exc:
        ui_app.main(argv)
    assert exc.value.code == 0
    assert started[-1]["command"] == expected


def test_a_link_cant_smuggle_in_other_options(started: list[dict]) -> None:
    # What Windows passes for a crafted link like  autoshelf:x" --compress "C:\secret
    argv = ["--url", 'autoshelf:x"', "--compress", "C:\\secret"]
    assert main(argv) == 0
    assert started[-1]["command"] == Command(commands.URL, url='autoshelf:x" --compress C:\\secret')


def test_convert_needs_a_format_and_a_file(
    started: list[dict], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        main(["--convert", "pdf"])
    assert "needs a format and at least one file" in capsys.readouterr().err
    assert started == []


# --- Explorer's right-click menu --------------------------------------------------------------

COMMAND = '"C:\\Program Files\\Auto Shelf ★\\AutoShelf.exe"'


def fake_targets(path: Path) -> list[str]:
    return {".png": ["jpg", "pdf"], ".pdf": ["docx"]}.get(path.suffix, [])


def test_the_menu_entries(fake_registry: FakeRegistry) -> None:
    explorer.add(command=COMMAND, targets=fake_targets)

    menu = fake_registry.keys[explorer.MENU_KEY]
    assert (menu["MUIVerb"], menu["SubCommands"]) == ("AutoShelf", "")
    sort = explorer.MENU_KEY + r"\shell\1sort"
    assert fake_registry.get(sort, "MUIVerb") == "Sort with AutoShelf"
    assert fake_registry.get(sort + r"\command") == f'{COMMAND} --sort "%1"'
    assert fake_registry.get(explorer.MENU_KEY + r"\shell\2why", "MultiSelectModel") == "Single"
    png = explorer.convert_key(".png")
    assert fake_registry.get(png, "MUIVerb") == "Convert with AutoShelf"
    assert fake_registry.get(png + r"\shell\1jpg\command") == f'{COMMAND} --convert jpg "%1"'
    assert fake_registry.get(png + r"\shell\2pdf", "MUIVerb") == "PDF"
    assert fake_registry.get(explorer.convert_key(".pdf") + r"\shell\1docx", "MUIVerb")
    assert explorer.convert_key(".docx") not in fake_registry.keys  # no Office: no entry
    assert explorer.is_added()
    assert explorer.program() == Path("C:\\Program Files\\Auto Shelf ★\\AutoShelf.exe")

    explorer.remove()
    assert fake_registry.keys == {}
    assert not explorer.is_added()


def test_a_menu_whose_program_is_gone_is_repaired(
    fake_registry: FakeRegistry, paths: AppPaths, tmp_path: Path
) -> None:
    service = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    assert not service.repair_integration()  # not added: left alone
    other_copy = make(tmp_path / "other copy" / "AutoShelf.exe", "")
    explorer.add(command=f'"{other_copy}"', targets=fake_targets)
    explorer.add_link(command=f'"{other_copy}"')
    assert not service.repair_integration()  # another copy that exists: left alone
    other_copy.unlink()
    assert service.repair_integration()
    assert explorer.program().exists()  # now this copy (here: the Python running the tests)
    assert explorer.link_ready()
    assert not service.repair_integration()
    service.set_explorer_menu(False)
    assert not service.explorer_menu()


def test_cli_turns_the_menu_on_and_off(
    fake_registry: FakeRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["config", "--explorer-menu", "on"]) == 0
    assert "Explorer menu:    on" in capsys.readouterr().out
    assert explorer.MENU_KEY in fake_registry.keys
    assert main(["config", "--explorer-menu", "off"]) == 0
    assert fake_registry.keys == {}


def test_the_real_registry_is_only_touched_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(explorer.sys, "platform", "linux")
    assert not explorer.is_added()
    with pytest.raises(explorer.ExplorerError):
        explorer.add()


# --- Sort with AutoShelf, Why is this here? ---------------------------------------------------


@pytest.fixture
def service(paths: AppPaths, root: Path):
    make(root / "Finance" / "invoice 2023.txt", "invoice total tax due bank")
    (root / "Recipes").mkdir()
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(root)
    app.describe("Finance", "invoice receipt tax bank")
    app.describe("Recipes", "recipe cooking baking oven")
    yield app
    app.release()


def test_sorting_files_chosen_in_explorer(service: AppService, root: Path, tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    bill = make(downloads / "bill.txt", "invoice tax due to the bank")
    cake = make(root / "cake.txt", "recipe: oven baking for a cake")
    placed = root / "Finance" / "invoice 2023.txt"
    kept = make(root / "_Trash" / "old.txt", "invoice")

    plan = service.plan_sort([bill, cake, placed, kept])

    assert {(item.path.name, item.target) for item in plan.proposals} == {
        ("bill.txt", "Finance"),
        ("cake.txt", "Recipes"),
    }
    assert plan.in_place == [placed]
    assert [path.name for path, _ in plan.refused] == ["old.txt"]
    for proposal in plan.proposals:
        proposal.selected = True  # a guess isn't ticked until you tick it
    result = service.apply_sort(plan.proposals)
    assert (root / "Finance" / "bill.txt").exists() and (root / "Recipes" / "cake.txt").exists()
    service.undo_batch(result.batch_id)
    assert bill.exists() and cake.exists()  # one batch, undone together


def test_a_file_on_another_drive_gets_a_clear_message(
    service: AppService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "USB stick"
    stick = make(elsewhere / "photo notes.txt", "x")

    class OtherDrive:
        def __getattr__(self, name):
            return getattr(os, name)

        def stat(self, path, *args, **kwargs):
            result = os.stat(path, *args, **kwargs)
            if Path(path) == elsewhere:
                return os.stat_result((*result[:2], result.st_dev + 1, *result[3:]))
            return result

    monkeypatch.setattr(config, "os", OtherDrive())
    with pytest.raises(ConfigError, match="on another drive"):
        service.plan_sort([stick])


def test_why_is_this_here(service: AppService, root: Path) -> None:
    mine = make(root / "Recipes" / "mine.txt", "soup")
    result = service.why_here(mine)
    assert result.entry is None
    assert "no record of moving mine.txt" in wording.why_here(result.path, None, False)

    zipped = service.compress([mine])
    result = service.why_here(zipped.dst)
    assert result.here and result.entry.action == "create"
    assert "AutoShelf made mine.zip" in wording.why_here(result.path, result.entry, result.here)

    (root / "_Inbox").mkdir(exist_ok=True)
    waiting = make(root / "_Inbox" / "receipt.txt", "receipt from the bank")
    done = service.assign(waiting, "Finance")
    result = service.why_here(done.dst)
    sentence = wording.why_here(result.path, result.entry, result.here)
    assert sentence.startswith("AutoShelf moved receipt.txt here on") and "You chose" in sentence

    # Moved by hand afterwards: AutoShelf's record no longer explains where it is.
    elsewhere = root / "Recipes" / "receipt.txt"
    done.dst.rename(elsewhere)
    with service._connect() as conn:
        record = repository.get_file_by_path(conn, done.dst)
        repository.set_file_location(conn, record.id, elsewhere, "sorted")
    result = service.why_here(elsewhere)
    assert not result.here
    assert "since then you or another program moved it here" in wording.why_here(
        result.path, result.entry, result.here
    )


def test_why_refuses_files_outside_the_library(service: AppService, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="outside the folder AutoShelf sorts"):
        service.why_here(make(tmp_path / "x.txt", "x"))
