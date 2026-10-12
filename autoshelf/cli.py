"""Command-line interface. The desktop app (Phase 4) will be the default command."""

import argparse
import contextlib
import sys
from collections.abc import Sequence

from autoshelf import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="autoshelf",
        description="Sorts new files in a root folder into your own subfolders.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def _make_console_safe() -> None:
    """Avoid crashes when a Unicode filename is printed to a non-UTF-8 console or pipe."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(errors="backslashreplace")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""
    _make_console_safe()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        print("\nThe desktop app is not built yet (Phase 4).")
        return 0
    return 0
