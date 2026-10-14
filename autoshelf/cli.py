"""Command-line interface. Without a command, the desktop app starts."""

import argparse
import contextlib
import getpass
import logging
import sqlite3
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from autoshelf import __version__
from autoshelf.actions import learning
from autoshelf.actions import rules as rule_engine
from autoshelf.actions.inbox import (
    AssignError,
    assign,
    inbox_items,
    resolve_folder,
    waiting_files,
)
from autoshelf.actions.mover import MoveResult
from autoshelf.actions.undo import UndoError, undo, undo_batch, undo_last
from autoshelf.app_service import AppService, library_factory, valid_watched, watch_lock
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
from autoshelf.extract import guard as read_guard
from autoshelf.features.duplicates import DuplicateError
from autoshelf.features.new_folders import FolderNameError
from autoshelf.features.rename import RenameError
from autoshelf.features.tags import TagError
from autoshelf.layout import INBOX_DIRNAME
from autoshelf.llm.base import LLMError
from autoshelf.llm.secrets import SecretError
from autoshelf.logging_setup import setup_logging
from autoshelf.ml import explain
from autoshelf.ml.embedder import EmbedderError
from autoshelf.startup import StartupError

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
    parser.add_argument(
        "--background",
        action="store_true",
        help="start the app in the tray without opening its window (used at sign-in)",
    )
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
    target.add_argument("--batch", metavar="ID", help="undo a whole batch (e.g. an organize run)")
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
    config.add_argument(
        "--watch", type=Path, metavar="FOLDER", help="also take new files from this folder"
    )
    config.add_argument("--unwatch", type=Path, metavar="FOLDER", help="stop watching a folder")
    config.add_argument(
        "--watch-extensions",
        metavar="EXTS",
        help='which new files to take from watched folders, e.g. ".pdf, .docx"',
    )
    config.add_argument(
        "--ai", choices=["off", "claude", "ollama"], help="optional AI provider (off by default)"
    )
    config.add_argument("--ai-model", metavar="MODEL", help="model name ('' for the default)")
    config.add_argument("--ollama-url", metavar="URL", help="Ollama server (default localhost)")
    config.add_argument(
        "--api-key",
        action="store_true",
        help="ask for the Claude API key and keep it in the Windows Credential Manager",
    )
    config.add_argument("--forget-api-key", action="store_true", help="remove the saved key")
    config.add_argument(
        "--startup", choices=["on", "off"], help="start AutoShelf when you sign in to Windows"
    )

    why = with_root(commands.add_parser("why", help="explain why a file went where it went"))
    why.add_argument("id", type=int, help="history entry (see: autoshelf history)")

    correct_cmd = with_root(
        commands.add_parser(
            "correct", help="move a file AutoShelf sorted into the right folder, and learn"
        )
    )
    correct_cmd.add_argument("id", type=int, help="history entry of the move to correct")
    correct_cmd.add_argument("folder", help='the right folder, e.g. "Finance"')

    commands.add_parser("learned", help="list the corrections AutoShelf learns from")

    rules = commands.add_parser("rules", help="list or change rules (they override the AI)")
    rule_commands = rules.add_subparsers(dest="rules_command", metavar="<action>")
    add = with_root(rule_commands.add_parser("add", help="add a rule (checked after the others)"))
    add.add_argument("name", help='a name for the rule, e.g. "Invoices"')
    condition = add.add_mutually_exclusive_group(required=True)
    condition.add_argument(
        "--name-matches", metavar="REGEX", help='file name matches, e.g. "invoice|receipt"'
    )
    condition.add_argument("--extension", metavar="EXT", help='extension(s), e.g. ".ipynb"')
    condition.add_argument("--text-contains", metavar="TEXT", help='e.g. "Syllabus"')
    add.add_argument("--to", required=True, metavar="FOLDER", help="an existing folder")
    for action, text in (
        ("remove", "delete a rule"),
        ("enable", "switch a rule on"),
        ("disable", "switch a rule off"),
        ("up", "check a rule earlier"),
        ("down", "check a rule later"),
    ):
        rule_commands.add_parser(action, help=text).add_argument("id", type=int)
    rule_commands.add_parser("test", help="show which rule would handle a file").add_argument(
        "file", type=Path
    )

    search = with_root(commands.add_parser("search", help="find files by meaning or words"))
    search.add_argument("query", help='e.g. "paper explaining k-means initialization"')
    search.add_argument("--limit", type=int, default=10, help="how many results (default 10)")

    related = with_root(commands.add_parser("related", help="files most similar to a file"))
    related.add_argument("file", type=Path)

    tag = with_root(commands.add_parser("tag", help="add tags to a file"))
    tag.add_argument("file", type=Path)
    tag.add_argument("tags", nargs="+", help="one or more tags")
    untag = with_root(commands.add_parser("untag", help="remove a tag from a file"))
    untag.add_argument("file", type=Path)
    untag.add_argument("tag")
    tags = with_root(commands.add_parser("tags", help="list tags, or the files with a tag"))
    tags.add_argument("tag", nargs="?", help="show the files with this tag")

    duplicates = with_root(
        commands.add_parser("duplicates", help="list identical files (nothing is deleted)")
    )
    duplicates.add_argument(
        "--keep",
        type=Path,
        metavar="FILE",
        help="keep this copy and move its identical copies into _Duplicates",
    )
    with_root(commands.add_parser("versions", help="list files that look like versions"))

    organize = with_root(
        commands.add_parser("organize", help="propose where every file in a folder belongs")
    )
    organize.add_argument("folder", type=Path, help="the root, _Inbox, a folder, or another one")
    organize.add_argument("--recursive", action="store_true", help="include subfolders")
    organize.add_argument(
        "--apply", action="store_true", help="move the confident proposals (one undoable batch)"
    )

    with_root(commands.add_parser("stats", help="numbers about your library"))

    # Optional AI (needs `config --ai claude|ollama`)
    summarize = with_root(commands.add_parser("summarize", help="summarize a file (AI)"))
    summarize.add_argument("file", type=Path)
    summarize.add_argument("--refresh", action="store_true", help="make a new summary")
    with_root(
        commands.add_parser("rename-suggestions", help="better names for files like scan_0034")
    )
    rename = with_root(commands.add_parser("rename", help="rename a file (undoable)"))
    rename.add_argument("file", type=Path)
    rename.add_argument("new_name", nargs="?", help="the new name (default: AutoShelf's idea)")
    rename.add_argument("--ai", action="store_true", help="let the AI propose the name")
    suggest = with_root(
        commands.add_parser("suggest-folders", help="topics in _Inbox that need a new folder")
    )
    suggest.add_argument(
        "--create", type=int, metavar="N", help="create suggestion N and move its files there"
    )
    ask_cmd = with_root(commands.add_parser("ask", help="ask a question about your files (AI)"))
    ask_cmd.add_argument("question")
    reading = with_root(
        commands.add_parser("reading-list", help="a folder's files, introductory first (AI)")
    )
    reading.add_argument("folder")
    cards = with_root(commands.add_parser("flashcards", help="make, list or export flashcards"))
    cards.add_argument("target", help="a file, or one of your folders")
    cards.add_argument("--count", type=int, default=10, help="how many cards to make")
    cards.add_argument("--list", action="store_true", help="show saved cards; make none")
    cards.add_argument(
        "--export", type=Path, metavar="CSV", help="write the folder's saved cards for Anki"
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
        from autoshelf.ui.app import run_app  # imports Qt; only needed for the app

        return run_app(verbose=args.verbose, show=not args.background)
    try:
        paths = get_app_paths()
        setup_logging(paths.log_dir, verbose=args.verbose)
        read_guard.start(paths.home, record=False)  # watch records; see _cmd_watch
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
            "why": _cmd_why,
            "correct": _cmd_correct,
            "learned": _cmd_learned,
            "rules": _cmd_rules,
            "search": _cmd_search,
            "related": _cmd_related,
            "tag": _cmd_tag,
            "untag": _cmd_untag,
            "tags": _cmd_tags,
            "duplicates": _cmd_duplicates,
            "versions": _cmd_versions,
            "organize": _cmd_organize,
            "stats": _cmd_stats,
            "summarize": _cmd_summarize,
            "rename-suggestions": _cmd_rename_suggestions,
            "rename": _cmd_rename,
            "suggest-folders": _cmd_suggest_folders,
            "ask": _cmd_ask,
            "reading-list": _cmd_reading_list,
            "flashcards": _cmd_flashcards,
        }[args.command]
        return handler(args, paths)
    except _NoRoot:
        print(
            "error: no root folder configured; pass --root PATH "
            '(or set one with: autoshelf config --root "PATH")',
            file=sys.stderr,
        )
        return EXIT_USAGE
    except (
        ConfigError,
        MigrationError,
        AssignError,
        EmbedderError,
        learning.CorrectionError,
        rule_engine.RuleError,
        TagError,
        DuplicateError,
        LLMError,
        SecretError,
        RenameError,
        FolderNameError,
        FileExistsError,
        StartupError,
    ) as exc:
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
    read_guard.start(paths.home, record=True)
    try:
        return _watch(args, paths, settings, root)
    finally:
        read_guard.stop()
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
        watched=valid_watched(paths, settings, root),
        watched_extensions=settings.watched_extensions,
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
            if args.batch is not None:
                results = undo_batch(conn, args.batch, force=args.force)
            elif args.last is not None:
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
        if args.watch_extensions is not None:
            settings.watched_extensions = sorted(rule_engine.extensions(args.watch_extensions))
            changed = True
        if args.ai is not None:
            settings.llm_provider = args.ai
            changed = True
        if args.ai_model is not None:
            settings.llm_model = args.ai_model.strip()
            changed = True
        if args.ollama_url is not None:
            settings.ollama_url = args.ollama_url.strip()
            changed = True
        if changed:
            save_settings(conn, settings)
    if args.startup is not None:
        from autoshelf import startup

        startup.set_enabled(args.startup == "on")
    if args.api_key:
        from autoshelf.llm.secrets import set_api_key

        set_api_key(getpass.getpass("Claude API key (input hidden): "))
        print("Saved the key in the Windows Credential Manager.")
    if args.forget_api_key:
        from autoshelf.llm.secrets import delete_api_key

        delete_api_key()
        print("Removed the saved key.")
    if args.watch is not None or args.unwatch is not None:
        service = AppService(paths)
        if args.watch is not None:
            print(f"Watching {service.add_watched(args.watch)} (restart the watcher to apply)")
        if args.unwatch is not None:
            service.remove_watched(args.unwatch)
        settings = service.settings()
    print(f"Root folder:      {settings.root or '(not set)'}")
    print(f"Auto threshold:   {settings.auto_threshold:.2f}")
    print(f"Auto margin:      {settings.auto_margin:.2f}")
    print(f"Embedding model:  {settings.embedding_model}")
    print(f"Stable seconds:   {settings.stable_seconds:g}")
    print(f"Watched folders:  {', '.join(settings.watched_folders) or '(none)'}")
    print(f"  taking:         {', '.join(settings.watched_extensions) or '(nothing)'}")
    from autoshelf.llm import PROVIDER_NAMES, default_model

    ai = PROVIDER_NAMES.get(settings.llm_provider, settings.llm_provider)
    if settings.llm_provider != "off":
        ai += f", model {settings.llm_model or default_model(settings.llm_provider)}"
    print(f"AI (optional):    {ai}")
    from autoshelf import startup

    print(f"Start with Windows: {'on' if startup.is_enabled() else 'off'}")
    print(f"Data folder:      {paths.home}")
    if args.model is not None and settings.embedding_model != DEFAULT_EMBEDDING_MODEL:
        print(
            "Note: the default threshold and margin are tuned for "
            f"{DEFAULT_EMBEDDING_MODEL}; other models score on a different scale."
        )
    return 0


def _cmd_why(args: argparse.Namespace, paths: AppPaths) -> int:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        entry = repository.get_history(conn, args.id)
    if entry is None:
        print(f"error: there is no history entry #{args.id}", file=sys.stderr)
        return 1
    print(format_history_entry(entry))
    root = Path(entry.root) if entry.root else None
    lines = explain.describe(entry.details, root)
    if entry.reason == "user" and not entry.details.get("corrects"):
        lines.insert(0, "You chose this folder.")
    print("\n".join(lines) if lines else "No explanation was stored for this entry.")
    return 0


def _cmd_correct(args: argparse.Namespace, paths: AppPaths) -> int:
    settings = _settings(paths)
    root = _root(args, settings, paths)
    key = resolve_folder(root, args.folder, settings.ignore_patterns)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        result = learning.correct(conn, args.id, key, root=root)
    print(
        f"Moved {result.src.name} -> {result.dst.relative_to(root)} and learned from it "
        f"(history #{result.history_id}; undo with: autoshelf undo {result.history_id})"
    )
    return 0


def _cmd_learned(args: argparse.Namespace, paths: AppPaths) -> int:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        rows = repository.list_corrections(conn)
    if not rows:
        print("Nothing learned yet. Answer the Inbox or move files between your folders.")
        return 0
    sources = {"inbox": "Inbox", "undo": "corrected", "external": "moved by you"}
    for row in rows:
        when = datetime.fromisoformat(row["ts"]).astimezone().strftime("%Y-%m-%d %H:%M")
        name = Path(row["path"]).name if row["path"] else "(a file)"
        origin = row["from_folder"] or "?"
        print(
            f"{when}  {name}: {origin} -> {row['to_folder']}  "
            f"[{sources.get(row['source'], row['source'])}]"
        )
    return 0


def _cmd_rules(args: argparse.Namespace, paths: AppPaths) -> int:
    action = args.rules_command
    if action == "test":
        return _rules_test(args, paths)
    with contextlib.closing(open_database(paths.db_path)) as conn:
        if action == "add":
            settings = _settings(paths)
            root = _root(args, settings, paths)
            kind, pattern = _rule_condition(args)
            rule_engine.check_rule(args.name, kind, pattern)
            key = resolve_folder(root, args.to, settings.ignore_patterns)
            rule_id = repository.insert_rule(
                conn, name=args.name.strip(), kind=kind, pattern=pattern.strip(), target_folder=key
            )
            print(f"Added rule #{rule_id}.")
        elif action is not None:
            rule = repository.get_rule(conn, args.id)
            if rule is None:
                print(f"error: there is no rule #{args.id}", file=sys.stderr)
                return 1
            _change_rule(conn, rule, action)
        _print_rules(repository.list_rules(conn))
    return 0


def _rule_condition(args: argparse.Namespace) -> tuple[str, str]:
    """(kind, pattern) from whichever of the condition options was given."""
    if args.name_matches is not None:
        return "name", args.name_matches
    if args.extension is not None:
        return "extension", args.extension
    return "text", args.text_contains


def _change_rule(conn: sqlite3.Connection, rule: repository.RuleRecord, action: str) -> None:
    if action == "remove":
        repository.delete_rule(conn, rule.id)
        print(f"Removed rule #{rule.id} {rule.name!r}.")
    elif action in ("enable", "disable"):
        repository.update_rule(
            conn,
            repository.RuleRecord(
                rule.id,
                rule.name,
                rule.priority,
                action == "enable",
                rule.kind,
                rule.pattern,
                rule.target_folder,
            ),
        )
    else:
        ids = [item.id for item in repository.list_rules(conn)]
        index = ids.index(rule.id)
        new_index = max(0, min(len(ids) - 1, index + (-1 if action == "up" else 1)))
        ids.insert(new_index, ids.pop(index))
        repository.reorder_rules(conn, ids)


def _print_rules(rules: list[repository.RuleRecord]) -> None:
    if not rules:
        print(
            "No rules yet. Add one, e.g.: autoshelf rules add Invoices "
            '--name-matches "invoice|receipt" --to Finance'
        )
        return
    print("Rules, checked in this order (the first match wins):")
    for number, rule in enumerate(rules, start=1):
        state = "" if rule.enabled else "  (off)"
        condition = rule_engine.describe_condition(rule.kind, rule.pattern)
        print(f"  {number}. #{rule.id} {rule.name}: {condition} -> {rule.target_folder}{state}")


def _rules_test(args: argparse.Namespace, paths: AppPaths) -> int:
    from autoshelf.extract import extract

    if not args.file.is_file():
        print(f"error: not a file: {args.file}", file=sys.stderr)
        return 1
    with contextlib.closing(open_database(paths.db_path)) as conn:
        rules = repository.list_rules(conn, enabled_only=True)
    match = rule_engine.first_match(rules, args.file, extract(args.file))
    if match is None:
        print("No rule matches; AutoShelf would decide by the file's content.")
    else:
        print(
            f"Rule {match.rule.name!r} matches ({match.match}): "
            f"it would go to {match.rule.target_folder}"
        )
    return 0


def _service(args: argparse.Namespace, paths: AppPaths) -> AppService:
    """The app's logic for commands that use it, honouring --root."""
    settings = _settings(paths)
    root = _root(args, settings, paths)
    return AppService(paths, root=root)


def _cmd_search(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    hits = service.search(args.query, limit=args.limit)
    if not hits:
        print("Nothing found.")
        return 0
    for hit in hits:
        where = hit.folder or "(root)"
        exact = "  [contains the words]" if hit.keyword else ""
        print(f"{hit.score:5.2f}  {hit.path.name}  in {where}{exact}")
        if hit.summary:
            print(f"       Summary: {hit.summary}")
        print(f"       {hit.snippet}")
    return 0


def _cmd_related(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    found = service.related(args.file.resolve())
    if not found:
        print("No related files (AutoShelf hasn't read this file yet).")
    for item in found:
        print(f"{item.similarity:5.2f}  {item.path.name}  in {item.folder or '(root)'}")
    return 0


def _cmd_tag(args: argparse.Namespace, paths: AppPaths) -> int:
    added = _service(args, paths).tag(args.file.resolve(), args.tags)
    print(f"Tagged {args.file.name}: {', '.join(added)}" if added else "Nothing new to tag.")
    return 0


def _cmd_untag(args: argparse.Namespace, paths: AppPaths) -> int:
    removed = _service(args, paths).untag(args.file.resolve(), args.tag)
    print(f"Removed the tag {args.tag!r}." if removed else f"{args.file.name} had no such tag.")
    return 0


def _cmd_tags(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    if args.tag:
        files = service.tagged(args.tag)
        print(f"{len(files)} file(s) tagged {args.tag!r}:")
        for path in files:
            print(f"  {path}")
        return 0
    tags = service.tags()
    if not tags:
        print('No tags yet. Add some with: autoshelf tag "<file>" <tag>')
    for name, count in tags:
        print(f"  {name}  ({count})")
    return 0


def _cmd_duplicates(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    groups = service.duplicates()
    if args.keep is not None:
        keep = args.keep.resolve()
        group = next((g for g in groups if keep in g.paths), None)
        if group is None:
            print(f"error: {args.keep} has no identical copies", file=sys.stderr)
            return 1
        moved = service.keep_one(keep, [path for path in group.paths if path != keep])
        for result in moved:
            print(f"Moved {result.src} -> {result.dst}")
        if moved:
            print(f"Undo with: autoshelf undo --batch {_batch_of(paths, moved[0].history_id)}")
        return 0
    if not groups:
        print("No identical files found.")
    for group in groups:
        print(f"{len(group.paths)} identical files ({group.size:,} bytes):")
        for path in group.paths:
            print(f"  {path}")
    if groups:
        print('Keep one and move the others to _Duplicates: autoshelf duplicates --keep "<file>"')
    return 0


def _batch_of(paths: AppPaths, history_id: int) -> str:
    with contextlib.closing(open_database(paths.db_path)) as conn:
        return repository.get_history(conn, history_id).batch_id


def _cmd_versions(args: argparse.Namespace, paths: AppPaths) -> int:
    groups = _service(args, paths).versions()
    if not groups:
        print("No possible versions found.")
    for group in groups:
        print(f"Possibly versions of one document (similarity {group.similarity:.2f}):")
        for path in group.paths:
            print(f"  {path}")
    return 0


def _cmd_organize(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    folder = service.check_organize_folder(args.folder)
    proposals = service.plan_organize(folder, recursive=args.recursive)
    if not proposals:
        print(f"Everything in {folder} is already where it belongs.")
        return 0
    print(f"Proposed moves for {folder} ([x] = confident, moved by --apply):")
    for item in proposals:
        mark = "[x]" if item.selected else "[ ]"
        score = f"{item.confidence:.2f}" if item.confidence is not None else "    "
        print(f"  {mark} {score}  {item.path.name}: {item.current} -> {item.target or '?'}")
        print(f"            {item.explanation}")
    if not args.apply:
        print("Nothing was moved. Add --apply to move the [x] files (as one undoable batch).")
        return 0
    result = service.apply_organize(folder, proposals)
    print(f"Moved {len(result.moved)} file(s).")
    for path, error in result.failed:
        print(f"  couldn't move {path.name}: {error}", file=sys.stderr)
    if result.moved:
        print(f"Undo all of them with: autoshelf undo --batch {result.batch_id}")
    return 1 if result.failed else 0


def _cmd_stats(args: argparse.Namespace, paths: AppPaths) -> int:
    stats = _service(args, paths).stats()
    print(f"Files in your folders: {stats.total_files}   waiting in _Inbox: {stats.inbox}")
    if stats.accuracy is not None:
        kept = stats.auto_moves - stats.auto_corrected
        print(
            f"Automatic moves: {stats.auto_moves}, kept {kept} ({stats.accuracy:.0%}); "
            f"moves by rules: {stats.rule_moves}"
        )
    print("Files per folder:")
    for folder, count in stats.files_per_folder:
        print(f"  {count:5}  {folder}")
    print("Added per month (last 12):")
    print("  " + "  ".join(f"{month}: {count}" for month, count in stats.added_per_month if count))
    if stats.corrected_pairs:
        print("Most corrected:")
        for source, target, count in stats.corrected_pairs:
            print(f"  {count:3}x  {source} -> {target}")
    if stats.stale_total:
        print(f"Not opened in 6+ months: {stats.stale_total} file(s), e.g.:")
        for item in stats.stale[:5]:
            print(f"  {item.last_used:%Y-%m-%d}  {item.path.name}")
    return 0


def _cmd_summarize(args: argparse.Namespace, paths: AppPaths) -> int:
    summary = _service(args, paths).summarize(args.file.resolve(), refresh=args.refresh)
    print(summary)
    return 0


def _cmd_rename_suggestions(args: argparse.Namespace, paths: AppPaths) -> int:
    proposals = _service(args, paths).rename_suggestions()
    if not proposals:
        print("No files with meaningless names (or none with a title or heading to use).")
    for proposal in proposals:
        print(f"{proposal.path.name}  ->  {proposal.new_name}   (from its {proposal.source})")
        print(f"    {proposal.path.parent}")
    if proposals:
        print('Rename one with: autoshelf rename "<file>" ["new name"]')
    return 0


def _cmd_rename(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    path = args.file.resolve()
    new_name = args.new_name
    if new_name is None:
        proposal = service.propose_name(path, use_ai=args.ai)
        if proposal is None:
            print(
                "error: no better name found; give one: autoshelf rename FILE NAME", file=sys.stderr
            )
            return 1
        new_name = proposal.new_name
    result = service.rename(path, new_name)
    print(
        f"Renamed {result.src.name} -> {result.dst.name} "
        f"(undo with: autoshelf undo {result.history_id})"
    )
    return 0


def _cmd_suggest_folders(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    suggestions = service.suggest_folders()
    if not suggestions:
        print("No new folder needed: no group of waiting files without a home.")
        return 0
    if args.create is not None:
        if not 1 <= args.create <= len(suggestions):
            print(f"error: choose a suggestion from 1 to {len(suggestions)}", file=sys.stderr)
            return 1
        chosen = suggestions[args.create - 1]
        key, moved = service.create_folder(chosen.name, list(chosen.paths))
        print(f"Created {key} and moved {len(moved)} file(s) there (each can be undone).")
        return 0
    for number, item in enumerate(suggestions, start=1):
        print(f"{number}. {len(item.paths)} files look like {item.name!r}: create a folder?")
        print(f"   words: {', '.join(item.keywords) or '-'}")
        for path in item.paths:
            print(f"   - {path.name}")
    print("Create one with: autoshelf suggest-folders --create N")
    return 0


def _cmd_ask(args: argparse.Namespace, paths: AppPaths) -> int:
    answer = _service(args, paths).ask(args.question)
    print(answer.text)
    shown = answer.cited or tuple(source.number for source in answer.sources)
    print("\nSources:")
    for source in answer.sources:
        if source.number in shown:
            print(f"  [{source.number}] {source.path}")
    return 0


def _cmd_reading_list(args: argparse.Namespace, paths: AppPaths) -> int:
    for number, item in enumerate(_service(args, paths).reading_list(args.folder), start=1):
        print(f"{number:2}. {item.path.name}" + (f"  - {item.reason}" if item.reason else ""))
    return 0


def _cmd_flashcards(args: argparse.Namespace, paths: AppPaths) -> int:
    service = _service(args, paths)
    target = Path(args.target)
    is_file = target.is_file()
    if args.list or args.export:
        folder = None if is_file else resolve_folder(service.root(), args.target)
        cards = service.flashcards(folder)
        if args.export:
            count = service.export_flashcards(cards, args.export)
            print(f"Wrote {count} card(s) to {args.export} (import it in Anki as CSV).")
            return 0
    elif is_file:
        cards = service.make_flashcards(path=target.resolve(), count=args.count)
    else:
        cards = service.make_flashcards(folder=args.target, count=args.count)
    for card in cards:
        print(f"Q: {card.question}\nA: {card.answer}\n")
    if not cards:
        print("No flashcards yet.")
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
