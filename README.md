# AutoShelf

A personal Windows desktop app that watches one root folder (for example inside iCloud Drive),
reads each new file's **name and text**, and moves it into the right folder **that you created**.
When it isn't confident, it asks. It learns from your corrections. The full design is in
[SPEC.md](SPEC.md).

> **Status:** early development. Phases 1–2 are done: a command-line watcher that safely moves
> new files into `_Inbox/` with full history and undo, and reads each file's text and metadata.
> There is no classifier yet (Phase 3), so every new file is treated as "not sure, ask the
> user" and waits in `_Inbox/`.

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
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The commands below call the venv's Python directly, so they work without activating it.
(If you do run `.venv\Scripts\Activate.ps1` and PowerShell blocks it, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once; after activating, plain `python`
works.)

## Usage

Try it on a throwaway folder first, never on your real files:

```powershell
New-Item -ItemType Directory -Force "C:\workspace\AutoShelf-sandbox\Finance"

# Dry run: watch and report what would happen, without moving anything
.venv\Scripts\python.exe -m autoshelf watch --root "C:\workspace\AutoShelf-sandbox" --dry-run

# Real run: new files in the root are moved into _Inbox\ (Ctrl+C to stop)
.venv\Scripts\python.exe -m autoshelf watch --root "C:\workspace\AutoShelf-sandbox"

# See what happened, and undo it
.venv\Scripts\python.exe -m autoshelf history
.venv\Scripts\python.exe -m autoshelf undo 3          # undo history entry #3
.venv\Scripts\python.exe -m autoshelf undo --last 5   # undo the 5 most recent moves
```

To see what AutoShelf reads from any file (it only reads, never changes it):

```powershell
.venv\Scripts\python.exe -m autoshelf extract "C:\workspace\AutoShelf-sandbox\Lecture notes.pdf"
```

Options: `-v` shows debug output, `--stable-seconds N` changes how long a file must stay
unchanged before it is processed. A file you undo is put back where it was and left alone
from then on. Undo refuses (and changes nothing) if the file has been edited since it was
moved (`--force` overrides), was moved elsewhere, or if its original folder no longer exists.

The log file is `%APPDATA%\AutoShelf\logs\autoshelf.log`. Set `AUTOSHELF_HOME` to use a
different data folder (handy for experiments).

## What AutoShelf reads

Only the filename and text, never images or audio content (no OCR, no transcription). Up to
20,000 characters of text are read per file.

| Files | What is read |
|---|---|
| PDF | page text, title, author, subject, keywords |
| Word `.docx`, PowerPoint `.pptx`, Excel `.xlsx` | paragraphs, tables, slides and notes, cells; document properties |
| EPUB, HTML, RTF, Jupyter `.ipynb` | chapter/page text, titles; notebook cells without outputs |
| Text, Markdown, CSV, JSON, code | the text (UTF-8, UTF-16, or the Windows code page such as Shift-JIS) |
| ZIP | the names of the files inside |
| Audio and video | tags such as title, artist, album |
| Images | embedded EXIF/PNG text such as title, description, keywords |
| Anything else | the filename only |

A file that can't be parsed (corrupt, password-protected) is still handled using its name.

## Development

```powershell
.venv\Scripts\python.exe -m pytest -q                                   # run the tests
.venv\Scripts\ruff.exe check . ; .venv\Scripts\ruff.exe format .       # lint and format
```

Tests only use temporary folders, never your real files or `%APPDATA%`.

## License

MIT
