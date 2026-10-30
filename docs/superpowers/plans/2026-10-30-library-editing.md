# Library editing and re-sorting into new folders — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move, create, rename and delete (to `_Trash`) from the Library, and re-sort a
parent's loose files when a new folder appears (name matches move, content guesses become
a "might belong" list).

**Architecture:** Core folder operations in a new `actions/folders.py` (history action
`'folder'`, migration V8); the database follows renamed folders in one repository call. A
pure planner in `features/resort.py`; the watcher detects new folders after each rescan and
applies the plan; `AppService` exposes everything to the Library (Qt) and the CLI.

**Tech Stack:** Python 3.13, sqlite3, PySide6 + qfluentwidgets, watchdog, pytest/pytest-qt.

**Spec:** `docs/superpowers/specs/2026-10-30-library-editing-design.md`

## Global Constraints

- Never delete a user file or folder; deleting moves into `_Trash/` (`actions.create.TRASH_DIRNAME`).
- Never overwrite: collisions get ` (2)`, ` (3)` (`mover.candidate_names`); a user-typed rename that collides is refused.
- History row `pending` before the change, `done` after; every change undoable.
- Only inside the root; `_`-prefixed folders are never categories; files are never moved into the root itself from the Library.
- Re-sort: name matches (and rules targeting inside the parent) move automatically; content guesses are only offered.
- Placeholder names never trigger: "New folder", "新しいフォルダー", "untitled folder", "名称未設定フォルダ" (any case, with " (n)" or " n").
- First 0.2.0 start seeds the known folders silently (setting `folders_seeded`).
- Paused or dry run: no automatic re-sort, nothing recorded.
- Core without Qt; slow work off the UI thread; tests use `tmp_path`, `FakeEmbedder`, names with spaces and Unicode.

## Review Focus

1. A folder renamed in the Library while the watcher runs: a rescan right after must record zero corrections (test in Task 1 and Task 5).
2. A name with `_` or `%` (LIKE wildcards) next to a similarly named folder: only the renamed folder's rows change (Task 1).
3. A folder created in Explorer as "New folder" then renamed "Notes" a few seconds later: one re-sort, for Notes (Task 4).
4. Undo of a moved folder when its old parent has been deleted meanwhile: refused, nothing changes (Task 3).
5. A file both name-matching two subfolders ("Notes" and "Meeting notes"): not moved automatically (Task 4).

---

### Task 1: Migration V8 and the database following a folder

**Files:**
- Modify: `autoshelf/db/schema.py` (add `V8`), `autoshelf/db/migrations.py` (`Rebuild(schema.V8)`), `autoshelf/db/repository.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `repository.UNDOABLE_ACTIONS == ("move", "rename", "create", "folder")`
  - `repository.follow_folder(conn, old_dir: Path, new_dir: Path, *, old_key: str | None, new_key: str | None, status: str | None) -> int` — rewrites `files.path` for the folder and below (case-insensitive prefix, computed in Python, not LIKE), sets `status` on them when given, and when both keys are given rewrites keys equal to or below `old_key` in `folders.path`, `files.suggestion`, `corrections.from_folder/to_folder`, `rules.target_folder`, `flashcards.folder`; bumps the library version; one transaction; returns files changed. Idempotent.
  - `repository.known_folders(conn) -> set[str]` (casefolded keys from `folders`)
  - `repository.add_known_folders(conn, keys: Iterable[str]) -> None` (insert-or-ignore, description '')

- [ ] **Step 1: Write failing tests** `test_migration_v8_allows_folder_rows` (a V7 database with history rows migrates, rows and ids kept, inserting `action='folder'` works), `test_follow_folder_moves_paths_and_keys` (files under `A_1` and `A_1/Sub`, description, correction, rule, flashcard and suggestion on `A_1` → `B 2`; a sibling `A%1` and `A_10` untouched; returns 2), `test_follow_folder_for_trash_changes_paths_only` (keys None, status 'ignored'), `test_known_folders_round_trip`.
- [ ] **Step 2: Run** `pytest tests/test_db.py -q` — the new tests fail.
- [ ] **Step 3: Implement** V8 as a copy of V6's rebuild with `CHECK (action IN ('move', 'rename', 'tag', 'undo', 'create', 'folder'))`, then the three repository functions.
- [ ] **Step 4: Run** `pytest tests/test_db.py -q` — pass.
- [ ] **Step 5: Commit** "Database: folder history rows, and records that follow a renamed folder".

### Task 2: Folder operations (`actions/folders.py`)

**Files:**
- Create: `autoshelf/actions/folders.py`
- Modify: `autoshelf/features/new_folders.py` (reserved Windows names in `clean_folder_name`/`create_folder`)
- Test: `tests/test_folders.py`

**Interfaces:**
- Consumes: Task 1; `mover.move_file`, `mover.candidate_names`, `learning.record_choice`, `learning.SOURCE_EXTERNAL`, `create.trash_dir`, `new_folders.create_folder`.
- Produces:
  - `class FolderError(Exception)`
  - `@dataclass(frozen=True) class Change: history_id: int; src: Path; dst: Path; kind: str` (kind: "file", "folder")
  - `@dataclass(frozen=True) class Changes: batch_id: str | None; done: list[Change]; failed: list[tuple[Path, str]]` with `history_ids` property
  - `REASON_TRASH = "trash"`, `KIND_RENAME = "rename"`, `KIND_MOVE = "move"`, `KIND_TRASH = "trash"`
  - `make_folder(root: Path, parent: str, name: str) -> str`
  - `move_files(conn, root, paths: list[Path], folder: str, *, before_rename=None) -> Changes` (reason "user"; a correction per file whose folder changed)
  - `rename_folder(conn, root, key: str, name: str, *, before_rename=None) -> str`
  - `move_folder(conn, root, key: str, parent: str, *, before_rename=None) -> str` ("" = top level)
  - `trash(conn, root, files: list[Path], folders: list[str], *, before_rename=None) -> Changes`
  - `folder_change(conn, root, src: Path, dst: Path, kind: str, *, undo_of: int | None = None, before_rename=None, batch_id=None) -> int` — the shared pending-row / `os.rename` / `follow_folder` / done sequence (used by Task 3's undo).

- [ ] **Step 1: Write failing tests** (all with names like `"Ünïcode notes"` and `"My Folder"`):
  `test_make_folder_refuses_reserved_and_existing` (`_x`, `CON`, existing name → FolderError; "Notes" → key "Folder/Notes");
  `test_move_files_records_history_and_learning` (two files A→B: one batch, 2 done rows reason "user", 2 corrections from "A" to "B");
  `test_move_files_refuses_the_root`;
  `test_rename_folder_follows_and_refuses_collision` (rename keeps description; to existing sibling → FolderError; "notes"→"Notes" case-only works);
  `test_move_folder_into_itself_refused_and_collision_gets_suffix`;
  `test_trash_files_and_folders` (file → `_Trash/x.pdf`, history action move reason "trash"; folder → `_Trash/Folder`, second one `_Trash/Folder (2)`, action folder kind trash, file records status ignored);
  `test_rename_then_refresh_records_no_corrections` (LibraryIndex with FakeEmbedder refreshed before and after `rename_folder`: `list_corrections` stays empty).
- [ ] **Step 2: Run** `pytest tests/test_folders.py -q` — fail (module missing).
- [ ] **Step 3: Implement** the module. Directory renames: refuse if the target exists unless only its case differs; check `is_within(root)` and not reserved for both ends (except `_Trash` as a trash target); call `before_rename(src, dst)` before `os.rename`.
- [ ] **Step 4: Run** `pytest tests/test_folders.py tests/test_mover.py -q` — pass.
- [ ] **Step 5: Commit** "Folders: make, rename, move and move to Trash, with history".

### Task 3: Undo, crash recovery and History wording for folder changes

**Files:**
- Modify: `autoshelf/actions/undo.py`, `autoshelf/actions/history.py`, `autoshelf/wording.py`, `autoshelf/reasons.py`
- Test: `tests/test_undo.py`, `tests/test_history.py`, `tests/test_folders.py`

**Interfaces:**
- Consumes: Task 2 `folder_change`, Task 1 `follow_folder`.
- Produces: `undo()` handles `action == "folder"` (renames back via `folder_change(..., undo_of=id)`, writes an `undo` row with `details.folder = True`, marks the original `undone`); `reconcile_pending` settles pending folder rows and re-applies `follow_folder` when done; `reasons.RESORTED = "resorted"` (folder, trigger, match); `wording.history_sentence` for folder kinds, trash and resort.

- [ ] **Step 1: Write failing tests** `test_undo_folder_rename_restores_keys`, `test_undo_trashed_folder_restores_status_sorted`, `test_undo_folder_refused_when_old_parent_gone` (state stays done, nothing moves), `test_undo_trashed_file` (back in its folder), `test_reconcile_pending_folder_row_done_and_failed`, `test_history_wording_for_folder_changes` (e.g. "Renamed folder A to B", "Moved to Trash").
- [ ] **Step 2: Run** them — fail.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests/test_undo.py tests/test_history.py tests/test_folders.py -q` — pass.
- [ ] **Step 5: Commit** "Undo and History for folder changes and Trash".

### Task 4: The re-sort planner (`features/resort.py`)

**Files:**
- Create: `autoshelf/features/resort.py`
- Test: `tests/test_resort.py`

**Interfaces:**
- Consumes: `LibraryIndex` (`entries()`, `folders`, `classify`, `profiles()`), `repository.list_rules`, `rules.first_match`, `repository.get_file_content`, `organize.apply`-style moving via `move_file`, `actions.inbox.waiting_files`.
- Produces:
  - `is_placeholder(name: str) -> bool`
  - `name_matches(folder_name: str, file_name: str, title: str | None = None) -> bool`
  - `@dataclass(frozen=True) class Candidate: path: Path; target: str; match: str; score: float | None` (match: "name", "rule", "content", "inbox")
  - `@dataclass(frozen=True) class ResortPlan: parent: str; trigger: str | None; moves: list[Candidate]; maybes: list[Candidate]`
  - `plan_resort(conn, root, library, parent: str, *, trigger: str | None = None, threshold, margin, ignore_patterns) -> ResortPlan` (parent "" = the Inbox branch)
  - `@dataclass(frozen=True) class ResortResult: plan: ResortPlan; batch_id: str | None; moved: list[MoveResult]; failed: list[tuple[Path, str]]`
  - `apply_moves(conn, root, plan: ResortPlan, candidates: list[Candidate], *, chosen_by_user: bool, before_rename=None) -> ResortResult` (reason "resort" with `reasons.RESORTED`, or "user" + correction from the parent when the user ticked maybes)
  - `@dataclass(frozen=True) class FolderNews: seeded: bool; new: list[str]; renamed: list[str]; parents: list[str]`
  - `find_new_folders(conn, folders: Sequence[str], relocations: Sequence = ()) -> FolderNews` — pure decision, records nothing
  - `remember_folders(conn, keys: Iterable[str]) -> None` (and sets `folders_seeded`)

- [ ] **Step 1: Write failing tests:**
  `test_is_placeholder` ("New folder", "new folder (3)", "新しいフォルダー (2)", "untitled folder 2", "名称未設定フォルダ" true; "Notes", "New folders of 2024" false);
  `test_name_matches` ("Notes" ~ "Calculus notes.md", "note_physics.txt"; "Meeting notes" !~ "meeting 2025.md"; "請求書" ~ "請求書_0412.pdf"; title used);
  `test_plan_moves_name_matches_only_inside_parent` (Folder with notes + invoice + a file in Other; new Folder/Notes: moves = the name matches, Other untouched);
  `test_two_name_matches_is_not_moved` (Notes and Meeting notes, file "meeting notes oct.md" → neither moves; it may be a maybe);
  `test_rule_inside_parent_moves` (rule name→Folder/Invoices);
  `test_maybes_need_threshold_and_both_margins` (FakeEmbedder vectors: content match listed in maybes, never in moves; a weak one excluded);
  `test_inbox_branch_moves_clear_matches` (parent "");
  `test_find_new_folders` (first call: seeded=True, new=[]; then a new key → new + parent; placeholder ignored; a renamed placeholder → new; pasted tree child skipped; folder holding a relocation → renamed);
  `test_apply_user_choice_records_correction`.
- [ ] **Step 2: Run** `pytest tests/test_resort.py -q` — fail.
- [ ] **Step 3: Implement.** Content maybes: `classify(vector, exclude=path)` scores restricted to `parent` and its subfolders; best subfolder qualifies when `score >= threshold`, `score - best other subfolder >= margin`, and `score - parent name-only profile similarity >= margin` (parent profile vector from `library.profiles()`).
- [ ] **Step 4: Run** `pytest tests/test_resort.py -q` — pass.
- [ ] **Step 5: Commit** "Re-sort: name matches move, content guesses are offered".

### Task 5: The watcher finds new folders and re-sorts

**Files:**
- Modify: `autoshelf/watcher/service.py`
- Test: `tests/test_watch_service.py`

**Interfaces:**
- Consumes: Task 4 (`find_new_folders`, `remember_folders`, `plan_resort`, `apply_moves`), Task 1 `follow_folder`.
- Produces: `WatchService(..., on_resorted: Callable[[ResortResult], None] | None = None)`; `WatchService.folder_moved(src: Path, dst: Path)` (queued from the observer thread, applied on the worker before each rescan); `resume()` marks the library dirty.

- [ ] **Step 1: Write failing tests** (existing service test patterns, FakeEmbedder library): `test_new_folder_triggers_resort` (start with Folder holding "lecture notes 1.txt" and "invoice.txt"; mkdir Folder/Notes; wait → notes moved, `on_resorted` called once, history reason "resort"); `test_first_start_seeds_without_moving`; `test_paused_defers_resort_until_resume`; `test_live_folder_rename_is_not_new_and_learns_nothing`; `test_dry_run_moves_nothing`.
- [ ] **Step 2: Run** them — fail.
- [ ] **Step 3: Implement**: a `_check_folders(conn, library, relocations)` called after `_open_library`, `_relearn` and `_sync_library` refreshes; directory moved events → `folder_moved`.
- [ ] **Step 4: Run** `pytest tests/test_watch_service.py -q` — pass.
- [ ] **Step 5: Commit** "Watcher: a new folder re-sorts its parent's loose files".

### Task 6: AppService and CLI

**Files:**
- Modify: `autoshelf/app_service.py`, `autoshelf/cli.py`
- Test: `tests/test_app_service.py`, `tests/test_cli.py`

**Interfaces:**
- Produces on `AppService`: `on_resorted` callback attr; `make_folder(parent: str, name: str) -> str`; `move_items(files: list[Path], folders: list[str], to: str) -> Changes`; `rename_folder(key, name) -> str`; `trash_items(files, folders) -> Changes`; `resort(folder: str) -> ResortResult`; `move_maybes(plan: ResortPlan, chosen: list[Candidate]) -> ResortResult`; `undo_changes(history_ids: list[int]) -> list[MoveResult]` (newest first). CLI: `autoshelf resort FOLDER [--dry-run]`.

- [ ] **Step 1: Write failing tests** for each method against a `tmp_path` root with the fake model, and `test_cli_resort_dry_run_prints_moves_and_maybes`.
- [ ] **Step 2: Run** — fail. **Step 3: Implement.** **Step 4: Run** `pytest tests/test_app_service.py tests/test_cli.py -q` — pass.
- [ ] **Step 5: Commit** "App service and CLI: folder changes and re-sort".

### Task 7: The Library UI, the review dialog, notifications

**Files:**
- Modify: `autoshelf/ui/library_view.py`, `autoshelf/ui/app.py`, `autoshelf/ui/main_window.py`, `autoshelf/links.py`
- Create: `autoshelf/ui/folder_dialogs.py` (Move to… picker, the maybes review)
- Test: `tests/test_ui.py`, `tests/test_links.py`

**Interfaces:**
- Consumes: Task 6.
- Produces: `LibraryView.new_folder()`, `.rename_selected()`, `.move_selected_to(key)`, `.cut()`, `.paste()`, `.delete_selected()`, `.resort_folder(key)`, `.undo_last_change()`; drag MIME type `application/x-autoshelf-items` (JSON list of `[kind, value]`); `ReviewDialog(plan)` with `.chosen()`; `links.REVIEW = "review"`; `AutoShelfApp` shows "Moved 3 files from Folder into Notes. 4 more might belong there." and keeps the last plans with maybes.

- [ ] **Step 1: Write failing UI tests**: `test_library_makes_and_renames_folders` (no_dialogs answers), `test_library_moves_files_by_drop_and_undo`, `test_library_deletes_to_trash_with_folder_confirmation`, `test_library_cut_and_paste`, `test_review_dialog_moves_ticked`, `test_resort_notification_text`.
- [ ] **Step 2: Run** — fail. **Step 3: Implement** (multi-select `ExtendedSelection`; shortcuts Del, F2, Ctrl+X, Ctrl+V, Ctrl+Shift+N; root not a file target). **Step 4: Run** `pytest tests/test_ui.py tests/test_links.py -q` — pass.
- [ ] **Step 5: Commit** "Library: move, new folder, rename, delete to Trash, re-sort with a review".

### Task 8: Docs and the full check

**Files:** `SPEC.md` §3, `autoshelf/layout.py` docstring, `README.md` (Library section, Trash, re-sort), CLAUDE.md untouched.

- [ ] **Step 1:** Update the texts. **Step 2:** `ruff check . && ruff format --check .` clean. **Step 3:** `pytest -q` — all pass. **Step 4:** Commit "Docs: editing in the Library and re-sorting".
