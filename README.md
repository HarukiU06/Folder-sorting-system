# AutoShelf

A personal Windows desktop app that watches one root folder (for example inside iCloud Drive),
reads each new file's **name and text**, and moves it into the right folder **that you created**.
When it isn't confident, it asks. It learns from your corrections. The full design is in
[SPEC.md](SPEC.md).

> **Status:** early development. Phase 0 (project skeleton) is done; nothing watches or moves
> files yet.

## Safety rules

AutoShelf moves real files, so it never deletes a file, never overwrites one (name clashes get
` (2)`, ` (3)`, …), only works inside the root you choose, records every move before doing it so
it can be undone, and never creates category folders on its own. Its database and logs live in
`%APPDATA%\AutoShelf\`, never inside the synced folder.

## Setup (Windows)

Requires Python 3.12 or newer. If `python --version` shows an older version, use the `py`
launcher as below.

```powershell
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

## Development

```powershell
pytest -q                          # run the tests
ruff check . ; ruff format .       # lint and format
```

Set `AUTOSHELF_HOME` to use a different data folder than `%APPDATA%\AutoShelf` (handy for
experiments).

## License

MIT
