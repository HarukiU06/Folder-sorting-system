# Library editing and re-sorting into new folders — design

Agreed with the user on 2026-10-30. Ships in 0.2.0 together with the updater
(`2026-10-30-updater-design.md`). Items marked *(my call)* are defaults chosen while writing
this up, not things the user decided.

## Why

The Library looks like File Explorer but can't change anything; the user wants to tidy
their folders where they browse them. And when they make a new folder for a kind of file
(Folder > lots of notes, then a new "Notes" inside Folder), the files that belong there
should go there without dragging each one.

## Goals

1. Move, create, rename and delete in the Library, the way Explorer does it.
2. When a new folder appears (made in the Library, in Explorer, on the iPhone, or while
   AutoShelf was closed), sort the loose files of its parent again: clear matches move by
   themselves, likely matches are offered in a list to tick.
3. Everything stays undoable, and nothing is ever deleted.

## Non-goals

- Emptying `_Trash` (the user deletes from Explorer when sure).
- Dropping files from Windows Explorer into the Library.
- Re-sorting anything other than the loose files of the new folder's parent.
- Fixing every folder rename done outside AutoShelf while it was closed (see "Renames").

## Anti-goals (a failure even if it works)

- A file the user placed by hand moved somewhere wrong by a guess.
- A deleted file that can't be got back.
- Reshuffling the whole library on the first start of 0.2.0, or when a folder tree is
  pasted in.

## Constraints (CLAUDE.md, SPEC)

Never delete, never overwrite, history row before every change, only inside the root, the
user makes the folders, long work off the UI thread, core logic without Qt and tested with
`tmp_path` trees (spaces and Unicode in names).

SPEC §3 said files in category folders are never re-sorted; the user changed that on purpose
for new folders only. SPEC §3, `layout.py` and the Library's docstring are updated to say so.

---

## Part 1 — editing in the Library

### What the user can do

| Action | How | Notes |
|---|---|---|
| Move files | drag onto a folder (tree or list); right-click → Move to…; Ctrl+X then Ctrl+V | several at once (Ctrl/Shift-click) |
| Move folders | the same | a moved folder keeps its description, rules and what was learned |
| New folder | toolbar button; right-click on empty space; Ctrl+Shift+N | made inside the folder on view; you type the name |
| Rename | F2 or right-click → Rename… | files (exists) and folders |
| Delete | Del or right-click → Delete | moves to `_Trash` in the AutoShelf folder; only a folder asks first ("Move Notes and its 12 files to Trash?") |
| Re-sort | right-click a folder → "Sort loose files into subfolders" | only offered for a folder that has subfolders |
| Undo | an Undo button in the status line after each change; History as always | |

- Files can't be moved into the root itself (the root is where new files arrive and get
  sorted, so the file would be sorted again). The root isn't offered as a target for
  files; folders may be moved to the top level. *(my call)*
- The Inbox and `_` folders stay out of the Library, as now.
- Multi-select: the details pane shows the first selected item; with several selected it
  says "N items selected". *(my call)*

### Core (no Qt): `autoshelf/actions/folders.py` (new)

- `make_folder(root, parent_key, name) -> key`: wraps `features.new_folders.create_folder`
  (already validates the name). Also refuses Windows reserved names (CON, NUL, COM1...).
  Not recorded in History (as today); a mistake is undone by Delete. *(my call)*
- `move_files(conn, root, paths, dest_key, before_rename) -> Batch`: `mover.move_file`
  per file, reason `"user"`, one `batch_id` when several. Each move to a different folder
  records a correction like a move made in Explorer (`record_choice`, source `external`,
  from the old folder key). Moving into the root is refused.
- `rename_folder(conn, root, key, new_name, before_rename) -> new_key` and
  `move_folder(conn, root, key, dest_key, before_rename) -> new_key`: one directory
  rename (`os.rename`; refuse if the target exists, except a case-only rename; refuse moving
  a folder into itself or below itself). A move that collides gets ` (2)`; a rename that
  collides is refused (the user typed that name).
- `trash(conn, root, items, before_rename) -> Batch`: files via `move_file` into
  `_Trash/` (reason `"trash"`); folders by renaming the directory into `_Trash/<name>`
  (` (2)` on a collision). `_Trash` is created if missing (AutoShelf's own reserved folder,
  like `_Inbox`).
- Every folder change is one history row with the new action `'folder'`: `src` the old
  directory, `dst` the new one, `details.kind` = `rename` | `move` | `trash`. Pending
  before the rename, done after the database follows.

### The database follows a renamed or moved folder

In one transaction right after the directory rename (keys are "/"-separated, compared
case-insensitively, and updated for the folder and everything below it):

- `files.path` (prefix), `files.suggestion`
- `folders.path` (descriptions, and the new "known folder" rows)
- `corrections.from_folder`, `corrections.to_folder`
- `rules.target_folder`
- `flashcards.folder`

For Trash only `files.path` changes (status `ignored`); the keys stay, so an Undo brings the
folder back with everything attached. History rows are a log and are not rewritten: undoing
an older move inside a folder renamed since then is refused with a clear message (undo the
folder change first).

This must happen before any library rescan sees the new paths: otherwise the rescan takes
every file in the folder for a file the user moved and records a false correction for each
(today's behaviour for folder renames in Explorer). The directory rename is announced to
the watcher (`before_rename`), and the watcher's own handling is idempotent.

### Undo and crash recovery

- `repository.UNDOABLE_ACTIONS` gains `'folder'`. Undoing renames the directory back (if
  the old place is free and its parent exists; folders are never recreated), puts the
  database back the same way, writes an `undo` row (`details.folder = true`) and marks the
  original `undone`.
- `reconcile_pending`: a pending `'folder'` row (or folder undo) is settled by checking which
  of `src`/`dst` exists, and on "done" the database update is applied again (it is
  idempotent).
- History shows "Renamed folder A to B", "Moved folder A into C", "Deleted A (in Trash)",
  "Deleted x.pdf (in Trash)", "Sorted into Notes when Notes appeared" (`wording.py`).

### Migration V8 (a `Rebuild`, like V6)

The `history.action` CHECK gains `'folder'`. No other table changes: known folders are
rows of the existing `folders` table, and "seeded" is a setting.

### Watcher: folder renames made in Explorer while AutoShelf runs

A directory "moved" event inside the root is queued and handled on the worker before the
next rescan: if the old key was a known folder, the database follows (same function as
above) and the folder counts as renamed, not new. This also stops the false corrections
for live Explorer renames. *(my call: same code path, small, and needed for "a renamed
folder isn't new".)*

---

## Part 2 — re-sorting when a folder appears

### When

- AutoShelf keeps the folders it has seen (rows in `folders`). After every library rescan
  (the watcher already rescans ~2 s after folder changes, at start, and at each sweep), any
  folder not seen before is new.
- First start of 0.2.0 (setting `folders_seeded` missing): every existing folder is
  recorded silently; nothing is re-sorted.
- Not new: a folder renamed or moved by AutoShelf or seen as a live rename (its key moves
  over); a folder whose files turn out to be known files that moved (the rescan adopted
  files into it: a rename while AutoShelf was closed).
- Placeholder names never trigger and aren't recorded: "New folder", "New folder (2)",
  "新しいフォルダー", "untitled folder", "名称未設定フォルダ" (case-insensitive, with
  " (n)"/" n" suffixes). Renaming one to a real name makes it new then.
- A new folder whose parent is also new in the same rescan (a pasted tree) is recorded
  without re-sorting.
- Paused or dry run: nothing is recorded, so the folder is found again after Resume (Resume
  marks the library dirty) — paused means no automatic moves.

### What is re-sorted

The files directly in the new folder's parent `P`, among `P`'s subfolders (all of them, not
only the new one). For a new top-level folder `P` is the root, whose loose files are sorted
anyway, so the files waiting in the Inbox are checked instead, against all folders.

Only indexed files are considered (they already have vectors: nothing is read or
downloaded from iCloud).

### How a file is judged (measured with the real model, see below)

1. **Name match → moves automatically.** Every word of the subfolder's name (via
   `name_tokens`, length ≥ 2, casefolded, a trailing "s" dropped on words longer than 3
   letters) is among the file's name tokens or its stored title's tokens; non-ASCII words
   may match as a substring of the file name (Japanese has no spaces). If the file matches
   two subfolders by name, it is not moved. A rule whose target is inside `P`'s subtree and
   matches the file counts like a name match (rules override the AI).
2. **Content → offered as a maybe.** Otherwise the classifier scores `P` and its
   subfolders (`LibraryIndex.classify`, learned penalties included, `exclude` = the file).
   The best subfolder is a maybe if its score ≥ the threshold, it beats the other
   subfolders by the margin, and it beats `P`'s name-only profile by the margin. Maybes are
   never moved without the user.

Inbox files (new top-level folder) use the normal decision (`choose` over all folders): a
clear match moves, as the watcher would; nothing goes to a maybe list from the Inbox
(*my call*: the Inbox already lists suggestions).

Measured 2026-10-30 with `paraphrase-multilingual-MiniLM-L12-v2` (scratch fixtures: a
University folder with 6 notes and 8 other files, a generic Folder with 6 notes and 6 other
files, a topic folder "Linear Algebra"): content alone against an empty new folder made 1-3
wrong moves at Balanced and even at Very cautious for the topic folder; adding a description
did not help. Name matches made no wrong moves at any level and found about half the
notes. Hence: names move, content suggests.

### What the user sees

- Notification: "Moved 3 files from Folder into Notes. 4 more might belong there." Clicking
  opens the Library on Folder with the review.
- The review (a dialog *(my call)*): the maybes with name, target subfolder and a confidence
  bar; none ticked; Move / Close. Ticked files move as one batch and count as the user's
  choice (corrections teach AutoShelf).
- The Library, if open on that folder, refreshes and shows "Moved 3 files into Notes ·
  Undo · Review 4 more" in its status line.
- Maybes are kept in memory until reviewed or the app quits; "Sort loose files into
  subfolders" on the folder finds them again any time.
- Automatic re-sort moves: one batch, history reason `"resort"`, reason code
  `resorted` (`folder`, `trigger` = the new folder, `match` = name | rule), undoable in one
  click. Moving a file back out by hand is learned as a correction.

### Where it runs

In the watcher's worker (it owns the library index, the self-move registry, pause and dry
run): `features/resort.py` plans (pure, testable with a fake embedder), `WatchService`
detects new folders after each rescan and applies, and reports through a new
`on_resorted(ResortResult)` callback (`AppService` → the app's notification and the
Library). The manual "Sort loose files into subfolders" calls `AppService.resort(key)` off
the UI thread with the same planner, then shows the review at once.

CLI: `autoshelf resort FOLDER [--dry-run]` *(my call: useful to try it on a real library)*.

---

## Tests (pytest, `tmp_path`, fake embedder; names with spaces and Unicode)

- folders: make / rename (incl. case-only) / move (incl. into itself, collisions → (2)) /
  trash (file and folder, collisions) / undo of each / refusal into the root.
- database follows: paths, descriptions, corrections, rules, flashcards, suggestions; then a
  library refresh records **zero** corrections.
- reconcile: pending folder rows before and after the rename.
- migration V8 from a V7 database with history rows.
- resort planner: name match (plural, Japanese substring, two subfolders → no move), rule
  match, maybes need threshold + both margins, untouched files outside `P`, Inbox branch.
- detection: seeding on first run, placeholder names and renaming away from them, pasted
  tree, rename (event and adoption) not new, paused/dry run record nothing.
- UI (pytest-qt): new folder, move via the context-menu path and via a simulated drop,
  rename folder, delete with the folder confirmation, Undo button, review dialog ticks.

## Left to the builder

Widget choices for drag and drop (custom MIME type with the selected keys/paths), exact
wording, how the Undo button sits in the status line, and keyboard shortcut collisions with
existing ones (Backspace = Back stays).
