# CLAUDE.md — AutoShelf

## What this is
A personal Windows desktop app that watches one root folder (inside iCloud Drive), reads each new file's **filename and text**, and moves it into the correct **user-created** subfolder. When it isn't confident, it asks the user. It learns from corrections. The full spec is in `SPEC.md`; read it before starting any phase.

## Stack
- Python 3.12+, PySide6 (UI + system tray), watchdog, sentence-transformers (local embeddings), SQLite (stdlib `sqlite3`), numpy, pytest.
- Extractors: PyMuPDF, python-docx, python-pptx, openpyxl, ebooklib, striprtf, beautifulsoup4, mutagen, Pillow.
- Optional LLM behind a provider interface (Claude API / Ollama), **off by default**. Keys go in `keyring`, never in files.
- Dependencies are managed in `pyproject.toml`. Use a virtual env (`.venv`).

## Project layout
```
autoshelf/
  __main__.py          # entry: tray app or CLI
  config.py            # settings load/save, paths (%APPDATA%\AutoShelf)
  db/                  # schema, migrations, repository functions
  watcher/             # watchdog handlers, ignore rules, stable-file detection
  extract/             # one module per file family + registry by extension
  ml/                  # embeddings, folder profiles, classifier, explain, clustering
  actions/             # mover (safe move/rename), history, undo, learning, rules
  features/            # search, duplicates, tags, stats, organize-dry-run
  llm/                 # provider interface + implementations (optional)
  ui/                  # PySide6 windows: tray, inbox, history, search, settings, dashboard
tests/
```

## Non-negotiable safety rules (the app moves real files)
1. **Never delete user files.** Not even duplicates. They are only flagged, or moved to `_Duplicates/`.
2. **Never overwrite.** On a name collision, append ` (2)`, ` (3)`, and so on.
3. Only operate inside the configured root and explicitly opted-in watched folders.
4. Write the history row **before** moving and mark it done after. Every move and rename must be undoable.
5. Never create category folders without explicit user approval.
6. The SQLite DB, caches and logs live in `%APPDATA%\AutoShelf\`, **never** inside the iCloud folder.
7. Don't process a file until it is stable: its size is unchanged for 3s and it opens without a sharing violation. iCloud files arrive gradually.
8. Ignore temp/partial files (`~$*`, `*.tmp`, `*.crdownload`, `*.part`, `desktop.ini`, hidden/system files).

## Development rules for Claude Code
- Build **one phase at a time** from SPEC.md §9. Propose a short plan for the phase, then implement it.
- Every phase ends runnable, with pytest tests for the logic: mover collisions, undo, ignore rules, classifier thresholds, rules engine. Use `tmp_path` fixtures with fake folder trees; **never touch real user folders in tests.**
- Keep core logic UI-independent so it is testable without Qt. The UI calls into `actions/`, `ml/` and `features/`.
- Long work (extraction, embedding) must run off the UI thread (QThreadPool or a worker thread).
- The embedding model loads once and is cached. Files are truncated to ~20k chars for classification.
- Use type hints, small functions, and docstrings on public functions. Format with `ruff format` and lint with `ruff`.
- Use Windows paths via `pathlib`. Test with spaces and Unicode in names.
- `.gitignore`: `.venv/`, `__pycache__/`, `*.db`, `dist/`, `build/`, `.env`, model caches. **Never commit secrets or personal files.**
- Commit at the end of each phase with a clear message, and update the README with how to run.

## Commands
- Install: `python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"`
- Run the CLI watcher (dry run): `python -m autoshelf watch --root "<path>" --dry-run`
- Run the app: `python -m autoshelf`
- Tests: `pytest -q`
- Lint: `ruff check . && ruff format .`
