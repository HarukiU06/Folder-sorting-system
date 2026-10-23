# AutoShelf — Specification

*(Working name. Rename freely.)*

A Windows desktop app that watches one root folder, reads each new file's name and text, and moves the file into the right user-made subfolder. When it isn't confident, it asks. It learns from every correction. The root folder lives in iCloud Drive, so the sorted library is also browsable from an iPhone via the Files app.

---

## 1. Decisions already made

| Topic | Decision |
|---|---|
| Who creates folders | **The user.** The app never creates a folder without explicit user approval. Nested subfolders are supported (e.g. `Machine Learning/Unsupervised/`). |
| Low confidence or multiple matches | **Ask the user**, offering the top existing folders as choices. Never guess silently. |
| Learning | **Yes.** Every correction (in-app, in Explorer, or on the iPhone) improves future sorting. |
| File types | **All types.** |
| What is analyzed | **Filename + text content only.** No OCR, no image understanding, no audio transcription. Files with no extractable text (images, audio, video, archives) are classified from the filename plus any embedded text metadata (e.g. document title, ID3 title, EXIF description). |
| Scale | Hundreds of files, so no vector database is needed. Use SQLite + in-memory numpy. |
| Platform | Windows 10/11 desktop. |
| Sync / phone | The root folder lives inside **iCloud Drive for Windows**. The iPhone sees it via the Files app. |
| Purpose | Personal project, published on GitHub. |

## 2. Tech stack

- **Language:** Python 3.12+
- **UI:** PySide6 (Qt). It provides the main window, a system tray icon, and runs in the background.
- **File watching:** `watchdog`
- **Text extraction:** `PyMuPDF` (PDF), `python-docx`, `python-pptx`, `openpyxl`, `ebooklib` (EPUB), `striprtf`, `beautifulsoup4` (HTML), plain read for txt/md/code/csv/json. Metadata: `mutagen` (audio), `Pillow` (EXIF).
- **Embeddings (local, free, private):** `sentence-transformers` with a small model such as `all-MiniLM-L6-v2` or `bge-small-en-v1.5`. It runs on CPU.
- **Storage:** SQLite in `%APPDATA%\AutoShelf\`. **The DB must not be placed inside the iCloud folder**, because syncing a live SQLite file corrupts it.
- **Notifications:** Windows toast notifications via `windows-toasts` or the Qt tray `showMessage`. They need action buttons for the "ask" flow if feasible; otherwise clicking the toast opens the Inbox screen.
- **Optional LLM (off by default):** a provider interface with a Claude API implementation and an Ollama (local) implementation. The API key is stored with `keyring` (Windows Credential Manager) and never written to a file in the repo.
- **Packaging:** PyInstaller. A "Start with Windows" option is available in settings.
- **Tests:** pytest

## 3. Folder layout on disk

```
iCloud Drive/AutoShelf/          <- the root ("big folder"); user picks the location on first run
├── _Inbox/                      <- files waiting for a user decision (visible on iPhone too)
├── Machine Learning/
│   ├── Unsupervised/
│   └── Deep Learning/
├── Finance/
└── (any file dropped here directly gets sorted)
```

- Reserved names start with `_` and are never treated as categories.
- Files dropped into the root, **or** into `_Inbox/` from the iPhone, are processed.
- Files the user places directly into a category folder are **not** re-sorted. They are treated as labeled training examples.

## 4. Core behavior

### 4.1 Ingestion pipeline
1. **Detect:** watchdog fires on a create, move-in, or modify event in the root.
2. **Ignore** temp and partial files: `~$*`, `*.tmp`, `*.crdownload`, `*.part`, `*.download`, `desktop.ini`, `.DS_Store`, hidden or system files, and anything in `_`-prefixed folders except `_Inbox`.
3. **Wait until stable:** the file size is unchanged for N seconds (default 3) and it can be opened without a sharing violation. This matters for iCloud: files arriving from the iPhone sync in gradually, and placeholder files hydrate on first read.
4. **Hash** the file (SHA-256) and check for an exact duplicate (§5.6).
5. **Extract:** filename tokens (split on `_ - . camelCase`), plus text (first ~20k chars, cleaned), plus text metadata.
6. **Check rules:** if a user rule matches, the file goes straight to its target (§4.4).
7. **Classify** (§4.2).
8. **Act:** auto-move, or ask (§4.3).
9. **Log** every action to history *before* performing it (§4.5).

### 4.2 Classification
- Each category folder has a **profile**:
  - the folder path and name (e.g. "Machine Learning / Unsupervised"),
  - an optional user-written **description** set in the app (e.g. "lecture notes and papers about ML, clustering, neural nets"), which is crucial for empty folders at the start,
  - embeddings of the files already inside it.
- **Scoring:** embed the new file as filename + text chunks. Compare it with each folder using a weighted combination of:
  - similarity to the folder name + description embedding,
  - k-nearest-neighbour similarity to files in the folder (top-k mean),
  - a learned boost from past corrections (§4.6).
- **Nested folders:** score leaf folders and their parents. Prefer the deepest folder that clears the threshold, otherwise fall back to the parent.
- **Decision:**
  - **Auto-move** if `top_score ≥ HIGH_THRESHOLD` **and** `top_score − second_score ≥ MARGIN`.
  - **Otherwise, ask.** This also covers "fits two folders equally."
  - Thresholds are adjustable in settings with a "cautious ↔ automatic" slider.
- Everything is handled in-process with numpy. At hundreds of files this is instant.

### 4.3 Ask flow (low confidence)
- The file moves to `_Inbox/`, so the iPhone shows what's pending.
- A toast notification shows: "Where should *Unsupervised learning.pdf* go?" It offers the top 3 folders with their confidence, plus "Other…" and "Leave in Inbox."
- The **Inbox screen** in the app lists all pending files. For each one it shows the top suggestions, a text preview, and the "why" explanation.
- Choosing a folder moves the file and records a learning example.
- If the file fits two folders, the user picks one physical location; the other can be added as a **tag** (§5.1).
- **From the iPhone:** the user can resolve Inbox items by dragging them into a folder in the Files app. The PC detects the move and treats it as the user's answer (§4.6).

### 4.4 Rules engine (overrides AI)
- These are user-defined rules evaluated in priority order before classification, e.g.:
  - filename matches regex `invoice|receipt` → `Finance/`
  - extension is `.ipynb` → `Machine Learning/Notebooks/`
  - text contains "Syllabus" → `University/`
- Rules are edited in the app (create, reorder, enable/disable, test on a sample file).
- A rule-based move is logged with the reason "Rule: <name>".

### 4.5 Safety, history and undo
**The app moves your files, so these rules are non-negotiable:**
- **Never delete** a user file. Not ever, not even duplicates. Duplicates are only flagged.
- **Never overwrite.** On a name collision, append ` (2)`, ` (3)`, and so on.
- Moves use `os.replace` / `shutil.move`, always within the root.
- Only touch files inside the root, plus explicitly opted-in watched sources (§5.9).
- Every action is written to the `history` table *before* execution and marked done after. On startup, incomplete actions are reconciled.
- The **History screen** shows timestamp, file, from → to, reason (auto / rule / user / learned), and confidence, with **Undo** per row and "Undo last N."
- A **global pause** toggle in the tray menu stops all automatic moves.

### 4.6 Learning from corrections
A labeled example (`file → folder`) is recorded when:
- the user picks a folder in the Ask flow,
- the user undoes an auto-move and chooses a different folder,
- the user moves a file between category folders **outside the app** (Explorer or iPhone).
  - Detect these via watchdog move events. iCloud sync may surface them as a delete + create, so match by content hash to recognize them as moves.
  - Don't count moves the app itself made (track pending self-moves).

How the examples are used:
- The corrected file's embedding joins the target folder's kNN pool, which happens automatically because the file is now in that folder.
- Per-folder "confusion" pairs are tracked. If files keep being moved from A to B, A's score is penalized for similar content.
- Keep it simple and explainable. No model fine-tuning is needed at this scale.

### 4.7 "Why did it go here?"
For every decision, store and display:
- the top 3 candidate folders with scores,
- the most similar existing files in the chosen folder,
- the top matching keywords or passages (TF-IDF overlap between the file and the folder profile),
- or the rule that fired.

No LLM is needed for this.

## 5. Additional features

### 5.1 Tags and virtual views
- A file has one physical location but can have any number of **tags**, stored in SQLite.
- Tag suggestions come from the classifier's 2nd/3rd-best folders when they're close, and from the LLM if it's enabled.
- **Virtual views** in the app list every file tagged "Medicine," regardless of where it lives.
- Tags are app-only; the iPhone sees physical folders. This is an accepted limitation.

### 5.2 Smart renaming (with approval)
- Detect junk names (`scan_0034`, `document(3)`, `download`, UUID-like names).
- Propose a clean name built from the PDF title metadata, the first heading, or the LLM if enabled, e.g. `Andrew Ng - Unsupervised Learning Lecture Notes.pdf`.
- Renaming **always** requires user approval, either in the Inbox or via a "Rename suggestions" list. Renames are undoable.

### 5.3 Semantic search
- Search by meaning across all files, e.g. "paper explaining k-means initialization."
- Index text in chunks (~500 tokens) with embeddings, plus SQLite FTS5 for exact keyword search. Results combine both.
- Results show the file, folder, matching snippet, and an Open button.

### 5.4 Related files
- When a file is selected, show its 5 nearest neighbours across the whole library.

### 5.5 Auto-summaries (LLM, optional)
- 2–3 line summary per file, generated lazily and cached in the DB, shown in the file detail panel and in search results.

### 5.6 Duplicate and version detection
- **Exact duplicates:** same SHA-256. Flag them and offer "keep one", which moves the extra to `_Duplicates/` for the user to delete manually.
- **Near-duplicates / versions:** very high embedding similarity plus similar names (`report_v2`, `report_final`). They are grouped in a "Possible versions" view.

### 5.7 Organize existing mess (dry run)
- Point the app at any folder, or at the root itself on first setup. It shows a **preview table** of proposed moves with confidence. The user can untick rows or change targets, then click Apply.
- Applying runs one batch that can be undone as a unit.

### 5.8 Suggest new folders
- When `_Inbox` or low-confidence files cluster around a topic no folder covers, propose: "6 files look like **Kubernetes**. Create a folder?"
- Use simple clustering on Inbox embeddings (agglomerative or HDBSCAN). The name comes from top keywords, or from the LLM if enabled.
- The folder is created only if the user clicks Create. This respects the rule that the user makes folders.

### 5.9 Watch other locations
- Optional, opt-in per folder (e.g. `Downloads`, `Desktop`). New files there matching configured extensions are moved into the root and sorted.
- Off by default. Each watched source is listed in settings.

### 5.10 Ask your library (LLM, optional)
- A chat panel that answers questions using retrieved chunks from your files (RAG), with citations linking to the source files, e.g. "What do my notes say about PCA?"

### 5.11 Study mode (LLM, optional)
- For any folder:
  - **Reading list:** order the files from introductory to advanced.
  - **Flashcards:** generate Q&A cards from a file or folder, export them to CSV (Anki-compatible), and review them in the app.

### 5.12 Stats dashboard
- Files per folder, files added over time, Inbox size, auto-sort accuracy (auto-moves not later corrected), most-corrected folder pairs, and files not opened in 6+ months.

## 6. iCloud and iPhone notes
- The app runs **only on the PC**. The iPhone gets:
  - browsing the sorted library via Files → iCloud Drive → AutoShelf,
  - drop-to-sort: save any file into the root or `_Inbox` from the iPhone, and it is sorted when the PC is on and synced,
  - answering Inbox questions by moving files into folders.
- Handle iCloud placeholders (files with "online-only" status). Reading them triggers a download. Use timeouts and retries, and don't block the UI.
- Never write app-internal files (DB, embeddings cache, logs) into the iCloud folder.
- Watch out for iCloud conflict copies (e.g. `file 2.pdf`). Treat them as near-duplicates.

## 7. Settings
- Root folder location
- Confidence slider (thresholds and margin)
- Folder descriptions (per folder)
- Rules
- Watched extra locations
- Ignored extensions and patterns
- LLM provider: Off / Claude API / Ollama, plus model and API key (stored in keyring), with a clear privacy note that file text is sent to the provider
- Start with Windows; notifications on/off; pause sorting

## 8. Data model (SQLite, sketch)
- `files(id, path, sha256, size, mtime, ext, title, text_excerpt, summary, status[sorted|inbox|ignored], added_at, last_opened_at)`
- `chunks(id, file_id, idx, text, embedding BLOB)` + FTS5 virtual table on `text`
- `folders(id, path, description, profile_embedding BLOB)`
- `tags(id, name)`, `file_tags(file_id, tag_id)`
- `rules(id, name, priority, enabled, kind, pattern, target_folder)`
- `history(id, ts, file_id, action[move|rename|tag|undo], src, dst, reason, confidence, details_json, batch_id, state[pending|done|undone])`
- `corrections(id, file_id, from_folder, to_folder, source[inbox|undo|external], ts)`
- `settings(key, value)`

## 9. Build phases
Each phase ends with something runnable, tests passing, and a commit.

0. **Skeleton:** repo structure, pyproject, config, logging, SQLite schema plus migrations, a pytest setup, and a README stub.
1. **Safe mover + watcher:** watchdog, ignore list, stable-file detection, hashing, the mover with collision handling, history, and undo. CLI only: `autoshelf watch --dry-run`.
2. **Extraction:** extractors for PDF, DOCX, TXT/MD first, then all other types, with metadata fallback for binary media.
3. **Classification + Inbox:** embeddings, folder profiles and descriptions, scoring, thresholds, and the `_Inbox` flow. Still CLI-testable.
4. **Desktop UI:** tray app, Inbox screen, toast "ask" notifications, History with Undo, Settings, and folder descriptions.
5. **Learning + rules + explain:** corrections (including external and iPhone moves), the rules engine, and the "Why here?" panel.
6. **Library features:** duplicates and versions, dry-run organize, semantic search, related files, tags and virtual views, stats dashboard, and watched locations.
7. **LLM features (optional provider):** summaries, smart renaming, new-folder suggestions, Ask-your-library, and study mode.
8. **Ship:** PyInstaller build, start with Windows, README with screenshots, and a GitHub Actions workflow running tests.

# AutoShelf — next round of work (Phases 9–15)

Read CLAUDE.md and SPEC.md first. All safety rules in CLAUDE.md still apply. Build ONE phase
at a time, in this order: propose a short plan, implement, add pytest tests, update the README,
then commit. Don't work on several phases in parallel: app_service.py, cli.py and
db/repository.py are touched by almost every phase, and each migration must get the next
number (V6, V7, ...).

## Phase 9 — Reliability and speed
1. **Periodic sweep.** Today loose files in the root and _Inbox are only queued at startup;
   after that the watcher relies on file events. StabilityTracker drops a file after 600 s
   ("will retry on next change"), and Windows can drop events on busy iCloud folders, so
   files can sit in the root until restart. Every few minutes (setting, default 5 min),
   re-offer loose files in the root and _Inbox to the tracker and rescan the library. Known,
   unchanged files are already skipped.
2. **Refresh only when needed.** Pipeline._classify calls library.refresh() for EVERY file
   (a full scandir of the tree). Each refresh also clears folder_documents, so explain() then
   runs about 2 DB queries per library file. Refresh only when the library is dirty, or once
   per batch. TRAP: _EventHandler ignores directory events, so a newly created empty folder
   would never mark the library dirty. Folder create/rename/delete events and folder
   description edits must mark it dirty. Organize and the CLI must still see current folders.
3. **Small fixes.**
   - The ReadGuard crash list (reading.json) also catches power cuts and forced restarts, and
     there's no way to clear it. Add a Settings/CLI action to list and clear skipped files.
   - The "not opened in 6+ months" stat relies on st_atime, which Windows often doesn't
     update. Say so in the UI, or rely on AutoShelf's own last_opened plus mtime.
   - Mark the Windows-only tests in tests/test_config.py (is_in_icloud with C:\ paths,
     startup Run key, `config --startup`) with skipif(sys.platform != "win32").
   - Default Claude model for bulk tasks (summaries, names, flashcards) should be
     claude-haiku-5-5. Keep Opus/Sonnet selectable.

## Phase 10 — Measure accuracy
1. **Benchmark script** (scripts/benchmark.py plus a small sample corpus in tests/data or
   generated). Leave-one-out: classify every library file with exclude=its path, and report
   accuracy, the share sent to the Inbox, and the wrong-auto-move rate at each
   CONFIDENCE_LEVEL. Use it to check the hand-tuned constants: NEIGHBOR_WEIGHT,
   HEADER_WEIGHT, FULL_POOL, threshold/margin, and the SIMILAR_FROM/FULL learning curve.
2. **Feature: "How well would AutoShelf sort my library?"** The same leave-one-out on the
   user's own folders, run off the UI thread (TaskRunner). Show for example "At Balanced: 87%
   correct, 9% to Inbox, 4% wrong", and recommend a slider position. Offer it on first run
   once folders have files, in Settings, and as `autoshelf calibrate` in the CLI.

## Phase 11 — Structured history (foundation for Phases 13 and 14)
1. Explanations are currently built as English sentences in core code (classifier.choose,
   explain, learning, organize, rename) and saved in history details_json["explanation"].
   Store structured data instead (reason code, folders, scores, rule name, match), and build
   the sentence at display time in the UI/CLI. Keep reading old rows that only have text.
2. Migration: allow a new history action for files AutoShelf creates (e.g. 'create',
   recording source file, output file, operation, sha256). Undoing a create must NOT delete:
   it moves the created file into a reserved folder (e.g. _Trash/ or _Converted/; the name
   starts with "_" so it's never a category) for the user to delete themselves.

## Phase 12 — Rules
1. **Suggest rules from corrections.** When several corrections share a pattern (the same
   word in the name, the same extension, the same to_folder; data is in `corrections`),
   offer "Make this a rule?" in the Rules view and after an Inbox answer. CLI:
   `autoshelf rules suggest`.
2. **Richer rules.** Migration: rules get a list of conditions (JSON) combined with AND
   (optionally ANY). Conditions: name regex, extension, text contains, size range, file date
   range. Actions: move to a folder (as now), add tags, and apply a rename template (e.g.
   "{date} {title}"). DECISION NEEDED: SPEC 5.2 says renames always need approval. Either
   treat a user-written rule as approval (and log it in history as a rule rename, undoable),
   or send rule renames to the Inbox for approval. Keep old rules working; update the Rules
   view, the rule test-on-a-file feature, and the CLI.

## Phase 13 — Compress and convert (new files, never in place)
Follows the safety rules: never delete, never overwrite. The output is always a NEW file
next to the original (collision names " (2)" as usual), recorded with the 'create' history
action from Phase 11. Undo moves the output to the reserved folder, never deletes it.
Decide, and make it a setting, whether an output written into the root gets sorted by the
watcher (register it in SelfMoveRegistry if not).
- Compress: zip one or more files; shrink a PDF (PyMuPDF save with garbage/deflate); shrink
  images (Pillow). The original is kept.
- Convert: images png/jpg/webp/bmp/tiff (+heic if pillow-heif) via Pillow; images -> PDF;
  PDF -> DOCX via pdf2docx (MIT licensed; layout quality varies, so say so in the UI).
  DOCX/PPTX/XLSX -> PDF has no good pure-Python route: only offer it when Microsoft Word
  (COM automation) or LibreOffice (soffice --headless) is found, otherwise hide it.
- Note the licensing: PyMuPDF is AGPL, so the distributed app must follow AGPL terms.
  Check the license of any new library before adding it.
- Available from the Library view, the CLI (`autoshelf compress`, `autoshelf convert FILE
  --to docx`) and the right-click menu (Phase 14). Long work runs off the UI thread.

## Phase 14 — Releases, installer, notifications, right-click menu
1. **Release workflow** (GitHub Actions, on tag v*): build with packaging/build.ps1, zip
   dist\AutoShelf, attach to a GitHub Release.
2. **Installer:** Inno Setup, per-user install with no admin rights. Creates a Start-menu
   shortcut with an AppUserModelID. Start-with-Windows must point to the installed exe.
   Code signing is optional (it removes the SmartScreen warning).
3. **Notification buttons:** with the AUMID registered, show Inbox notifications with
   buttons (top 3 folders, "Leave in Inbox"), e.g. via windows-toasts. Handle the click
   when the app is running (through the existing QLocalServer command channel) and when it
   is closed (launch with arguments). Update the README sentence that says buttons are
   impossible.
4. **Explorer right-click menu** (per-user HKCU registry keys, added/removed from Settings
   and by the installer): "Sort with AutoShelf", "Why is this here?", "Compress",
   "Convert to ▸". The handler launches AutoShelf with arguments, which forwards the command
   to the running instance via the existing QLocalServer/QLocalSocket (app.py
   _serve_commands); start the app if it isn't running. Handle multi-select (many files at
   once). On Windows 11 these entries appear under "Show more options"; a top-level entry
   needs a packaged app (MSIX/sparse package + IExplorerCommand), which is out of scope for
   now. A file outside the root on another drive can't be sorted (moves are renames only):
   show a clear message instead of failing silently.

## Phase 15 — Weekly digest
A notification once a week: "This week: 23 sorted, 3 waiting in Inbox, 1 correction
learned", from the history and corrections tables. Clicking it opens the Dashboard. The app
isn't always running, so on start, show it if 7+ days have passed since the last one. Store
the last-shown date in settings. On/off in Settings.

## Phase 16 — Japanese interface (last, once UI text has stopped changing)
Wrap all UI strings (about 200, in ui/*.py) for Qt translation (self.tr / QCoreApplication
.translate). Add a Japanese .ts/.qm translation and a language setting (default: follow
Windows). Translate the display-time explanations from Phase 11. Make the PyInstaller spec
bundle the .qm files. The CLI can stay English or use the same catalog.