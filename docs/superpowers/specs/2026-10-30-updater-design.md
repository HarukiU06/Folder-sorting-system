# Updates from GitHub Releases — design

Agreed with the user on 2026-10-30. Built after the Library changes
(`2026-10-30-library-editing-design.md`); both ship as 0.2.0. Items marked *(my call)* are
defaults chosen while writing this up.

## Why

Updating today means downloading `setup.exe` again by hand. The user wants an Update
button and a notification when a newer version exists, with the versions coming from the
GitHub repository.

## Goals

- Find out that a newer release exists on `HarukiU06/Folder-sorting-system`.
- Tell the user once per new version; offer Update in Settings for as long as it applies.
- Install only when the user clicks Update, then reopen AutoShelf on the new version.

## Non-goals / anti-goals

- No automatic installs, no background downloads before the click.
- No pre-releases or drafts.
- Never run a download that is incomplete or damaged; never download from anywhere but
  this repository's releases over HTTPS.
- Nothing about the user's files leaves the PC (the check sends only an HTTP request for
  the release list).

## How it works

### Checking (`autoshelf/updates.py`, no Qt)

- `GET https://api.github.com/repos/HarukiU06/Folder-sorting-system/releases/latest`
  (stdlib `urllib`, a timeout, a `User-Agent`). `latest` already skips drafts and
  pre-releases.
- Version: the tag `vX.Y.Z` compared with `autoshelf.__version__` as integer tuples; a tag
  that doesn't parse is ignored.
- The installer asset is the one named `AutoShelf-X.Y.Z-setup.exe`; its checksum is the
  asset `AutoShelf-X.Y.Z-setup.exe.sha256`. Both download URLs must start with
  `https://github.com/HarukiU06/Folder-sorting-system/releases/download/` (GitHub then
  redirects to its storage, over HTTPS).
- When: at start (after a short delay) and every 24 hours while running, if the setting
  `check_updates` is on (default on). Offline or any error: silent, logged, tried again
  next time. `update_checked` (time) and `update_notified` (version) are stored settings.

### Telling the user

- One notification per new version: "AutoShelf 0.3.0 is available" with an **Update**
  button (the `autoshelf:update` link; clicking the body opens Settings). *(my call: no
  repeat reminders)*
- Settings → a "Updates" group: the installed version, "AutoShelf 0.3.0 is available"
  with **Update…** when there is one, "Check now", and the on/off switch.

### Updating (only on click)

1. A dialog shows the release notes (the release body, plain text) and the download size:
   Update / Not now.
2. Download `setup.exe` into `%APPDATA%\AutoShelf\cache\updates\` off the UI thread, with a
   progress bar and Cancel.
3. Compare its SHA-256 with the published `.sha256`. This catches a broken or incomplete
   download; it is not protection against a compromised GitHub account (both files come
   from the same release; code signing would be, and stays optional). A mismatch deletes
   only AutoShelf's own downloaded file and reports it.
4. Start the installer detached:
   `setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /AUTOSHELFUPDATE`, then quit
   AutoShelf (the watcher stops, releasing the single-sorter lock).
5. The installer quits any AutoShelf still running (it already does, `PrepareToInstall`),
   keeps the earlier choices (Inno's `UsePreviousTasks` default: Explorer menu, start
   with Windows), replaces the files, and starts AutoShelf again through a new `[Run]`
   entry that only runs for `/AUTOSHELFUPDATE` (the normal entry is `skipifsilent`).
   The new instance takes the lock after the old one has gone; if it finds the lock still
   held it waits a few seconds as it does today for `--quit`.

Settings and history live in `%APPDATA%\AutoShelf` and aren't touched by installs.

### Where updating isn't possible

- Running from source (`not sys.frozen`): no Update button; Settings says "Running from
  source: update with git pull".
- A copy unzipped from the release zip (no `unins000.exe` next to `AutoShelf.exe`): Update
  opens the release page in the browser instead. *(my call)*

### Release workflow

- `release.yml` also writes `AutoShelf-X.Y.Z-setup.exe.sha256` (the SHA-256 in hex, then
  two spaces and the file name) and attaches it.
- README: "Publishing a release": bump the version in `pyproject.toml` and
  `autoshelf/__init__.py`, commit, push a tag `vX.Y.Z`. The user pushes; Claude never
  pushes tags. The first tag also tests the workflow, which has never run.

## Tests

- version parsing and comparison; picking assets; refusing foreign URLs; checksum match and
  mismatch (local files, no network: the HTTP fetch is injected).
- the once-per-version notification and the 24 h schedule (fake clock).
- from-source and portable detection.
- installer: `tests/test_installer.py` checks the new `[Run]` entry and its `Check`.
- UI: Settings shows the available version and Update; the dialog's buttons.

## Left to the builder

The exact Settings layout and texts, the progress dialog widget, how long to wait after
start before the first check.
