"""Command-line interface. The desktop app (Phase 4) will be the default command."""

import argparse
import contextlib
import logging
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from autoshelf import __version__
from autoshelf.actions.mover import MoveResult
from autoshelf.actions.undo import UndoError, undo, undo_last
from autoshelf.config import AppPaths, ConfigError, get_app_paths, load_settings, validate_root
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.db.migrations import MigrationError
from autoshelf.logging_setup import setup_logging

log = logging.getLogger("autoshelf.cli")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="autoshelf",
        description="Sorts new files in a root folder into your own subfolders.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    watch = commands.add_parser("watch", help="watch the root folder and sort new files")
    watch.add_argument("--root", type=Path, help="the library root folder to watch")
    watch.add_argument(
        "--dry-run", action="store_true", help="only report what would happen; move nothing"
    )
    watch.add_argument(
        "--stable-seconds",
        type=float,
        help="how long a file must stay unchanged before it is processed (default 3)",
    )

    history = commands.add_parser("history", help="list recent moves")
    history.add_argument("--limit", type=int, default=20, help="how many entries to show")

    undo_cmd = commands.add_parser("undo", help="undo moves recorded in history")
    target = undo_cmd.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?", type=int, help="history entry to undo")
    target.add_argument("--last", type=int, metavar="N", help="undo the N most recent moves")
    undo_cmd.add_argument(
        "--force", action="store_true", help="undo even if the file changed after it was moved"
    )

    extract_cmd = commands.add_parser("extract", help="show the text AutoShelf reads from a file")
    extract_cmd.add_argument("file", type=Path, help="any file (it is only read)")
    extract_cmd.add_argument(
        "--chars", type=int, default=500, help="how much of the text to print (default 500)"
    )
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
    try:
        paths = get_app_paths()
        setup_logging(paths.log_dir, verbose=args.verbose)
        handler = {
            "watch": _cmd_watch,
            "history": _cmd_history,
            "undo": _cmd_undo,
            "extract": _cmd_extract,
        }[args.command]
        return handler(args, paths)
    except (ConfigError, MigrationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_watch(args: argparse.Namespace, paths: AppPaths) -> int:
    from autoshelf.watcher.service import WatchService  # imports watchdog; only needed here

    with contextlib.closing(open_database(paths.db_path)) as conn:
        settings = load_settings(conn)
    root_arg = args.root or settings.root
    if root_arg is None:
        print("error: no root folder configured; pass --root PATH", file=sys.stderr)
        return 2
    root = validate_root(Path(root_arg), paths)
    stable_seconds = (
        args.stable_seconds if args.stable_seconds is not None else settings.stable_seconds
    )

    service = WatchService(
        root,
        paths.db_path,
        dry_run=args.dry_run,
        stable_seconds=stable_seconds,
        ignore_patterns=settings.ignore_patterns,
    )
    mode = "DRY RUN: nothing will be moved" if args.dry_run else "files will be moved"
    log.info("Watching %s (%s). Data folder: %s", root, mode, paths.home)
    service.start()
    log.info("Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping...")
    finally:
        service.stop()
    return 0


def _cmd_history(args: argparse.Namespace, paths: AppPaths) -> int:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        entries = repository.list_history(conn, args.limit)
    if not entries:
        print("No history yet.")
        return 0
    for entry in entries:
        print(format_history_entry(entry))
    return 0


def _cmd_undo(args: argparse.Namespace, paths: AppPaths) -> int:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        try:
            if args.last is not None:
                results = undo_last(conn, args.last, force=args.force)
            else:
                results = [undo(conn, args.id, force=args.force)]
        except UndoError as exc:
            for result in exc.completed:
                print(_format_undo(result))
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if not results:
        print("Nothing to undo.")
    for result in results:
        print(_format_undo(result))
    return 0


def _cmd_extract(args: argparse.Namespace, paths: AppPaths) -> int:
    from autoshelf.extract import extract  # extractor libraries load on first use

    if not args.file.is_file():
        print(f"error: not a file: {args.file}", file=sys.stderr)
        return 1
    result = extract(args.file)
    print(f"File:      {args.file}")
    print(f"Extractor: {result.extractor}")
    print(f"Summary:   {result.summary()}")
    print(f"Filename:  {', '.join(result.filename_tokens) or '-'}")
    print(f"Title:     {result.title or '-'}")
    for key, value in sorted(result.metadata.items()):
        print(f"{key.capitalize() + ':':<10} {value}")
    if result.text:
        shown = result.text[: args.chars]
        more = len(result.text) - len(shown)
        print(f"--- text (first {len(shown):,} of {len(result.text):,} chars) ---")
        print(shown + ("\n..." if more else ""))
    return 0


def format_history_entry(entry: repository.HistoryEntry) -> str:
    """Render one history row as a single line."""
    when = datetime.fromisoformat(entry.ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    root = Path(entry.root) if entry.root else None
    src = _relative(entry.src, root)
    dst = _relative(entry.dst, root)
    line = f"#{entry.id:<4} {when}  {entry.action:<6} {entry.state:<7} {src} -> {dst}"
    if entry.reason:
        line += f"  [{entry.reason}]"
    note = entry.details.get("note")
    if note:
        line += f"  ({note})"
    return line


def _format_undo(result: MoveResult) -> str:
    return f"Undid: {result.src.name} -> {result.dst} (history #{result.history_id})"


def _relative(path: str | None, root: Path | None) -> str:
    if path is None:
        return "?"
    if root is not None:
        with contextlib.suppress(ValueError):
            return str(Path(path).relative_to(root))
    return path
