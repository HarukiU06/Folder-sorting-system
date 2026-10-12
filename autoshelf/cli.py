"""Command-line interface. The desktop app (Phase 4) will be the default command."""

import argparse
import contextlib
import logging
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from autoshelf import __version__
from autoshelf.actions.inbox import (
    AssignError,
    assign,
    inbox_items,
    resolve_folder,
    waiting_files,
)
from autoshelf.actions.mover import MoveResult
from autoshelf.actions.undo import UndoError, undo, undo_last
from autoshelf.app_service import library_factory, watch_lock
from autoshelf.config import (
    DEFAULT_EMBEDDING_MODEL,
    AppPaths,
    ConfigError,
    Settings,
    get_app_paths,
    load_settings,
    save_settings,
    validate_root,
)
from autoshelf.db import repository
from autoshelf.db.connection import open_database
from autoshelf.db.migrations import MigrationError
from autoshelf.layout import INBOX_DIRNAME
from autoshelf.logging_setup import setup_logging
from autoshelf.ml.embedder import EmbedderError

log = logging.getLogger("autoshelf.cli")

EXIT_USAGE = 2


class _NoRoot(Exception):
    """No root folder was given and none is configured."""


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for all subcommands."""
    parser = argparse.ArgumentParser(
        prog="autoshelf",
        description="Sorts new files in a root folder into your own subfolders.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    def with_root(command: argparse.ArgumentParser) -> argparse.ArgumentParser:
        command.add_argument(
            "--root", type=Path, help="the library root folder (default: the configured one)"
        )
        return command

    watch = with_root(commands.add_parser("watch", help="watch the root folder and sort new files"))
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

    classify_cmd = with_root(
        commands.add_parser("classify", help="show which folder a file would go to (moves nothing)")
    )
    classify_cmd.add_argument("file", type=Path, help="any file (it is only read)")

    with_root(commands.add_parser("folders", help="list your folders and their descriptions"))

    describe = with_root(
        commands.add_parser("describe", help="describe what belongs in a folder (helps sorting)")
    )
    describe.add_argument("folder", help='folder path inside the root, e.g. "Machine Learning/NLP"')
    describe.add_argument("text", nargs="?", help='the description ("" clears it)')

    with_root(commands.add_parser("index", help="read the files already in your folders"))
    with_root(commands.add_parser("inbox", help="list files waiting in _Inbox, with suggestions"))

    assign_cmd = with_root(
        commands.add_parser("assign", help="move a waiting file into the folder you choose")
    )
    assign_cmd.add_argument("file", help="a file in _Inbox (its name is enough) or in the root")
    assign_cmd.add_argument("folder", help='an existing folder, e.g. "Finance"')

    config = commands.add_parser("config", help="show or change settings")
    config.add_argument("--root", type=Path, help="set the library root folder")
    config.add_argument(
        "--threshold", type=float, help="minimum score to move a file automatically"
    )
    config.add_argument(
        "--margin", type=float, help="how far the best folder must beat the next one"
    )
    config.add_argument("--model", help="embedding model (sentence-transformers name)")
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
        from autoshelf.ui.app import run_app  # imports Qt; only needed for the app

        return run_app(verbose=args.verbose)
    try:
        paths = get_app_paths()
        setup_logging(paths.log_dir, verbose=args.verbose)
        handler = {
            "watch": _cmd_watch,
            "history": _cmd_history,
            "undo": _cmd_undo,
            "extract": _cmd_extract,
            "classify": _cmd_classify,
            "folders": _cmd_folders,
            "describe": _cmd_describe,
            "index": _cmd_index,
            "inbox": _cmd_inbox,
            "assign": _cmd_assign,
            "config": _cmd_config,
        }[args.command]
        return handler(args, paths)
    except _NoRoot:
        print(
            "error: no root folder configured; pass --root PATH "
            '(or set one with: autoshelf config --root "PATH")',
            file=sys.stderr,
        )
        return EXIT_USAGE
    except (ConfigError, MigrationError, AssignError, EmbedderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _root(args: argparse.Namespace, settings: Settings, paths: AppPaths) -> Path:
    root_arg = args.root or settings.root
    if root_arg is None:
        raise _NoRoot
    return validate_root(Path(root_arg), paths)


def _settings(paths: AppPaths) -> Settings:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        return load_settings(conn)


def _open_library(conn: sqlite3.Connection, root: Path, settings: Settings, paths: AppPaths):
    """Load the language model and index the folders under ``root``."""
    library = library_factory(paths, settings, root)(conn)

    def progress(done: int, total: int) -> None:
        if done == total or done % 25 == 0:
            print(f"Reading files already in your folders: {done}/{total}", file=sys.stderr)

    library.refresh(progress=progress)
    return library


def _cmd_watch(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    lock = watch_lock(paths)
    if not lock.acquire():
        holder = lock.holder_pid()
        print(
            "error: AutoShelf is already sorting files"
            + (f" (process {holder})" if holder else "")
            + "; only one can run at a time. Quit the app or the other watch first.",
            file=sys.stderr,
        )
        return 1
    try:
        return _watch(args, paths, settings, root)
    finally:
        lock.release()


def _watch(args: argparse.Namespace, paths: AppPaths, settings: Settings, root: Path) -> int:
    from autoshelf.watcher.service import WatchService  # imports watchdog; only needed here

    stable_seconds = (
        args.stable_seconds if args.stable_seconds is not None else settings.stable_seconds
    )
    service = WatchService(
        root,
        paths.db_path,
        dry_run=args.dry_run,
        stable_seconds=stable_seconds,
        ignore_patterns=settings.ignore_patterns,
        make_library=library_factory(paths, settings, root),
        threshold=settings.auto_threshold,
        margin=settings.auto_margin,
    )
    mode = "DRY RUN: nothing will be moved" if args.dry_run else "files will be moved"
    log.info("Watching %s (%s). Data folder: %s", root, mode, paths.home)
    log.info("Loading the language model; new files are sorted once it is ready.")
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


def _cmd_classify(args: argparse.Namespace, paths: AppPaths) -> int:
    from autoshelf.extract import extract

    if not args.file.is_file():
        print(f"error: not a file: {args.file}", file=sys.stderr)
        return 1
    path = args.file.resolve()
    settings = _settings(paths)
    root = _root(args, settings, paths)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        library = _open_library(conn, root, settings, paths)
        extraction = extract(path)
        classification = library.classify(
            library.vectorize(extraction, path.name),
            threshold=settings.auto_threshold,
            margin=settings.auto_margin,
            exclude=path,
        )
    print(f"File:      {path}")
    print(f"Read:      {extraction.summary()}")
    if classification.choice is not None:
        print(f"Decision:  move to {classification.choice.folder!r} automatically")
    else:
        print(f"Decision:  ask you (it would wait in {INBOX_DIRNAME})")
    print(f"Why:       {classification.explanation}")
    if classification.scores:
        print("Folders (score = similarity to the folder's name/description and its files):")
    for item in classification.top(5):
        detail = f"name {item.profile_similarity:.2f}"
        if item.neighbor_similarity is not None:
            nearest = item.neighbors[0]
            detail += (
                f", files {item.neighbor_similarity:.2f}; "
                f"closest: {nearest.path.name} ({nearest.similarity:.2f})"
            )
        print(f"  {item.score:5.2f}  {item.folder}   [{detail}]")
    return 0


def _cmd_folders(args: argparse.Namespace, paths: AppPaths) -> int:
    from autoshelf.ml.library import scan_library

    settings = _settings(paths)
    root = _root(args, settings, paths)
    scan = scan_library(root, settings.ignore_patterns)
    if not scan.folders:
        print(f"No folders yet. Create folders in {root} and AutoShelf will sort into them.")
        return 0
    counts = dict.fromkeys(scan.folders, 0)
    for item in scan.files:
        counts[item.folder] += 1
    with contextlib.closing(open_database(paths.db_path)) as conn:
        descriptions = repository.folder_descriptions(conn)
    print(f"Folders in {root}:")
    width = max(len(key) for key in scan.folders)
    for key in scan.folders:
        files = f"{counts[key]} file" + ("" if counts[key] == 1 else "s")
        description = descriptions.get(key.casefold(), "")
        line = f"  {key:<{width}}  {files:>9}"
        print(f"{line}  {description}" if description else line)
    return 0


def _cmd_describe(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    key = resolve_folder(root, args.folder, settings.ignore_patterns)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        if args.text is None:
            current = repository.folder_descriptions(conn).get(key.casefold())
            print(f"{key}: {current}" if current else f"{key} has no description yet.")
            return 0
        repository.set_folder_description(conn, key, args.text.strip())
    if args.text.strip():
        print(f"Described {key}: {args.text.strip()}")
    else:
        print(f"Cleared the description of {key}.")
    return 0


def _cmd_index(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        library = _open_library(conn, root, settings, paths)
        counts = library.direct_file_counts()
    total = sum(counts.values())
    print(f"Indexed {total} files in {len(counts)} folders under {root}.")
    return 0


def _cmd_inbox(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    if not waiting_files(root, settings.ignore_patterns):
        print(f"Nothing is waiting in {INBOX_DIRNAME}.")
        return 0
    with contextlib.closing(open_database(paths.db_path)) as conn:
        library = _open_library(conn, root, settings, paths)
        items = inbox_items(
            conn,
            root,
            library,
            threshold=settings.auto_threshold,
            margin=settings.auto_margin,
            ignore_patterns=settings.ignore_patterns,
        )
    print(f"{len(items)} file(s) waiting in {INBOX_DIRNAME}:")
    for item in items:
        top = item.classification.top(3) if item.classification else ()
        suggestions = "  ".join(f"{score.folder} ({score.score:.2f})" for score in top)
        print(f"  {item.path.name}")
        print(f"      suggestions: {suggestions or '-'}")
    print('Put one away with: autoshelf assign "<file>" "<folder>"')
    return 0


def _cmd_assign(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    path = _find_waiting_file(args.file, root)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        result = assign(
            conn, path, args.folder, root=root, ignore_patterns=settings.ignore_patterns
        )
    print(
        f"Moved {result.src.name} -> {result.dst.relative_to(root)} "
        f"(history #{result.history_id}; undo with: autoshelf undo {result.history_id})"
    )
    return 0


def _find_waiting_file(name: str, root: Path) -> Path:
    """Resolve a file given as a path, or as a bare name in ``_Inbox`` or the root."""
    given = Path(name)
    candidates = (
        [given] if given.is_absolute() else [root / INBOX_DIRNAME / name, root / name, given]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise AssignError(f"There is no file {name!r} in {root / INBOX_DIRNAME} or {root}")


def _cmd_config(args: argparse.Namespace, paths: AppPaths) -> int:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        settings = load_settings(conn)
        changed = False
        if args.root is not None:
            settings.root = str(validate_root(args.root, paths))
            changed = True
        if args.threshold is not None:
            settings.auto_threshold = _fraction(args.threshold, "--threshold")
            changed = True
        if args.margin is not None:
            settings.auto_margin = _fraction(args.margin, "--margin")
            changed = True
        if args.model is not None:
            settings.embedding_model = args.model.strip()
            changed = True
        if changed:
            save_settings(conn, settings)
    print(f"Root folder:      {settings.root or '(not set)'}")
    print(f"Auto threshold:   {settings.auto_threshold:.2f}")
    print(f"Auto margin:      {settings.auto_margin:.2f}")
    print(f"Embedding model:  {settings.embedding_model}")
    print(f"Stable seconds:   {settings.stable_seconds:g}")
    print(f"Data folder:      {paths.home}")
    if args.model is not None and settings.embedding_model != DEFAULT_EMBEDDING_MODEL:
        print(
            "Note: the default threshold and margin are tuned for "
            f"{DEFAULT_EMBEDDING_MODEL}; other models score on a different scale."
        )
    return 0


def _fraction(value: float, flag: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise ConfigError(f"{flag} must be between 0 and 1")
    return value


def format_history_entry(entry: repository.HistoryEntry) -> str:
    """Render one history row as a single line."""
    when = datetime.fromisoformat(entry.ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    root = Path(entry.root) if entry.root else None
    src = _relative(entry.src, root)
    dst = _relative(entry.dst, root)
    line = f"#{entry.id:<4} {when}  {entry.action:<6} {entry.state:<7} {src} -> {dst}"
    if entry.reason:
        confidence = f" {entry.confidence:.2f}" if entry.confidence is not None else ""
        line += f"  [{entry.reason}{confidence}]"
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
