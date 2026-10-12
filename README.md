# AutoShelf

A personal Windows desktop app that watches one root folder (for example inside iCloud Drive),
reads each new file's **name and text**, and moves it into the right folder **that you created**.
When it isn't confident, it asks. It learns from your corrections. The full design is in
[SPEC.md](SPEC.md).

> **Status:** early development. Phase 1 is done: a command-line watcher that safely moves new
> files into `_Inbox/`, with full history and undo. There is no classifier yet (Phase 3), so
> every new file is treated as "not sure, ask the user" and waits in `_Inbox/`.

## Safety rules

AutoShelf moves real files, so:

- it **never deletes** a file and **never overwrites** one: name clashes get ` (2)`, ` (3)`, …;
- it only works inside the root you choose, and refuses paths that lead outside it;
- it records every move in its history **before** doing it, so every move can be undone;
- it never creates category folders (it only creates its own `_Inbox/` when first needed);
- it waits until a file has stopped changing for 3 seconds and no program is still writing it;
- it ignores temporary and partial files (`~$*`, `*.tmp`, `*.crdownload`, `*.part`,
  `*.download`, `desktop.ini`, `.DS_Store`, `Thumbs.db`) and hidden or system files;
- its database and logs live in `%APPDATA%\AutoShelf\`, never inside the synced folder.

## Folder layout

```
AutoShelf/                  <- the root you pass with --root
├── _Inbox/                 <- files waiting for your decision
├── Finance/                <- your own folders; files you put here are never moved
├── Machine Learning/
│   └── Unsupervised/
└── new file.pdf            <- files dropped here (or into _Inbox) get processed
```

Folders whose names start with `_` are reserved and are never treated as categories.

## Setup (Windows)

Requires Python 3.12 or newer. If `python --version` shows an older version, use the `py`
launcher as below.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Usage

Try it on a throwaway folder first, never on your real files:

```powershell
mkdir C:\workspace\AutoShelf-sandbox\Finance

# Dry run: watch and report what would happen, without moving anything
python -m autoshelf watch --root "C:\workspace\AutoShelf-sandbox" --dry-run

# Real run: new files in the root are moved into _Inbox\ (Ctrl+C to stop)
python -m autoshelf watch --root "C:\workspace\AutoShelf-sandbox"

# See what happened, and undo it
python -m autoshelf history
python -m autoshelf undo 3            # undo history entry #3
python -m autoshelf undo --last 5     # undo the five most recent moves, newest first
```

Options: `-v` shows debug output, `--stable-seconds N` changes how long a file must stay
unchanged before it is processed. A file you undo is put back where it was and left alone
from then on. Undo refuses (and changes nothing) if the file has been edited since it was
moved (`--force` overrides), was moved elsewhere, or if its original folder no longer exists.

The log file is `%APPDATA%\AutoShelf\logs\autoshelf.log`. Set `AUTOSHELF_HOME` to use a
different data folder (handy for experiments).

## Development

```powershell
pytest -q                          # run the tests
ruff check . ; ruff format .       # lint and format
```

Tests only use temporary folders, never your real files or `%APPDATA%`.

## License

MIT
