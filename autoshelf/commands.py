"""Commands for the running app, from later launches, Explorer's right-click menu and
notification buttons (SPEC Phase 14).

A launch with arguments (``AutoShelf.exe --compress FILE``) sends its command to the app
that is already running, over a local socket, as one line of JSON. Explorer starts one
process per selected file, so the app collects what arrives together and runs it as one
command: ten right-clicked images make one ZIP, not ten. No Qt here, so it is testable
without a GUI.
"""

import argparse
import json
from collections.abc import Iterable
from dataclasses import dataclass, field

SHOW = "show"
QUIT = "quit"
SORT = "sort"
WHY = "why"
COMPRESS = "compress"
CONVERT = "convert"
URL = "url"
ACTIONS = frozenset({SHOW, QUIT, SORT, WHY, COMPRESS, CONVERT, URL})
FILE_ACTIONS = frozenset({SORT, WHY, COMPRESS, CONVERT})

ACK = b"ok\n"
REFUSED = b"no\n"
MAX_LINE = 1 << 20  # 1 MB: thousands of paths; anything longer is not a command


class CommandError(ValueError):
    """A command that can't be understood."""


@dataclass(frozen=True)
class Command:
    """What to do: an action, the files it is about, the format to convert to, or the
    ``autoshelf:`` link a notification button opened."""

    action: str
    files: tuple[str, ...] = ()
    to: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise CommandError(f"Unknown command: {self.action!r}")
        if self.action in FILE_ACTIONS and not self.files:
            raise CommandError(f"{self.action} needs at least one file")
        if self.action == CONVERT and not self.to:
            raise CommandError("convert needs a format")
        if self.action == URL and not self.url:
            raise CommandError("url needs a link")


# --- on the wire -----------------------------------------------------------------------------


def encode(command: Command) -> bytes:
    """One line of UTF-8 JSON."""
    data = {"action": command.action}
    if command.files:
        data["files"] = list(command.files)
    if command.to:
        data["to"] = command.to
    if command.url:
        data["url"] = command.url
    return json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n"


def decode(line: bytes) -> Command:
    """The command in one line (without its newline). Raises CommandError."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommandError(f"Not a command: {exc}") from None
    if not isinstance(data, dict):
        raise CommandError("Not a command")
    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(item, str) for item in files):
        raise CommandError("files must be a list of paths")
    to, url = data.get("to"), data.get("url")
    if not isinstance(data.get("action"), str) or not all(
        value is None or isinstance(value, str) for value in (to, url)
    ):
        raise CommandError("Not a command")
    return Command(data["action"], tuple(files), to, url)


@dataclass
class LineReader:
    """Collects bytes until whole lines have arrived (a long path can come in pieces)."""

    buffer: bytes = b""
    limit: int = MAX_LINE

    def feed(self, data: bytes) -> list[bytes]:
        """Add ``data``; return the complete lines received so far. Raises CommandError if
        a line grows beyond the limit."""
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        if len(self.buffer) > self.limit:
            raise CommandError("The command is too long")
        return [line for line in lines if line.strip()]


# --- arriving together ---------------------------------------------------------------------


@dataclass
class Batch:
    """Commands waiting to run; ``merged`` joins those that arrived together."""

    commands: list[Command] = field(default_factory=list)

    def add(self, command: Command) -> None:
        self.commands.append(command)

    def take(self) -> list[Command]:
        """The waiting commands, merged, and an empty batch."""
        commands, self.commands = self.commands, []
        return merge(commands)


def merge(commands: Iterable[Command]) -> list[Command]:
    """One command per action (and format): the files of several right-clicked files are
    handled together, in the order they arrived, each once. Repeated "show"s become one;
    links stay separate."""
    merged: dict[tuple, Command] = {}
    for command in commands:
        if command.action == URL:
            key: tuple = (URL, command.url)
        else:
            key = (command.action, (command.to or "").casefold())
        earlier = merged.get(key)
        if earlier is None:
            merged[key] = command
        elif command.files:
            files = tuple(dict.fromkeys(earlier.files + command.files))
            merged[key] = Command(earlier.action, files, earlier.to, earlier.url)
    return list(merged.values())


# --- the command line of AutoShelf.exe -------------------------------------------------------


def add_app_arguments(parser: argparse.ArgumentParser) -> None:
    """The options that send a command to the app (Explorer's menu and notifications use
    them; they work whether or not the app is running)."""
    group = parser.add_argument_group("commands for the desktop app")
    choice = group.add_mutually_exclusive_group()
    choice.add_argument("--sort", nargs="+", metavar="FILE", help="preview where files would go")
    choice.add_argument("--why", nargs="+", metavar="FILE", help="show why a file is where it is")
    choice.add_argument("--compress", nargs="+", metavar="FILE", help="make a smaller copy or ZIP")
    choice.add_argument(
        "--convert",
        nargs="+",
        metavar=("FORMAT", "FILE"),
        help="make a copy of the files in another format",
    )
    choice.add_argument("--url", metavar="LINK", help="open an autoshelf: link")
    choice.add_argument("--quit", action="store_true", help="quit the running app")


def from_args(args: argparse.Namespace) -> Command | None:
    """The command given on the command line, or None (just start or show the app)."""
    if getattr(args, "url", None):
        return Command(URL, url=args.url)
    if getattr(args, "quit", False):
        return Command(QUIT)
    convert = getattr(args, "convert", None)
    if convert:
        if len(convert) < 2:
            raise CommandError("--convert needs a format and at least one file")
        return Command(CONVERT, tuple(convert[1:]), to=convert[0])
    for action in (SORT, WHY, COMPRESS):
        files = getattr(args, action, None)
        if files:
            return Command(action, tuple(files))
    return None


def safe_url_argv(argv: list[str]) -> list[str]:
    """Only the link, when the app was started for an ``autoshelf:`` link.

    Windows starts ``AutoShelf.exe --url "%1"`` with whatever link a web page or a
    notification opened. Everything after ``--url`` is that link, so a crafted link can't
    smuggle in other options (such as ``--compress``)."""
    if "--url" not in argv:
        return argv
    index = argv.index("--url")
    return ["--url", " ".join(argv[index + 1 :])]
