"""Repository functions: all SQL used by the rest of the app lives here."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoshelf.db.connection import transaction


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string, the format stored in every ``ts`` column."""
    return datetime.now(UTC).isoformat(timespec="seconds")


# --- settings ---------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    """Return a JSON-decoded setting, or ``default`` if it isn't stored."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return default if row is None else json.loads(row["value"])


def set_setting(conn: sqlite3.Connection, key: str, value: Any) -> None:
    """Store a setting as JSON, replacing any previous value."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


# --- files ------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    """A row of the ``files`` table."""

    id: int
    path: str
    sha256: str | None
    size: int | None
    mtime: float | None
    ext: str | None
    status: str
    added_at: str
    suggestion: str | None = None  # the folder AutoShelf last chose or suggested


def _file_from_row(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=row["id"],
        path=row["path"],
        sha256=row["sha256"],
        size=row["size"],
        mtime=row["mtime"],
        ext=row["ext"],
        status=row["status"],
        added_at=row["added_at"],
        suggestion=row["suggestion"],
    )


def get_file(conn: sqlite3.Connection, file_id: int) -> FileRecord | None:
    """Return a file by id."""
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return None if row is None else _file_from_row(row)


def get_file_by_path(conn: sqlite3.Connection, path: str | Path) -> FileRecord | None:
    """Return the file recorded at ``path`` (case-insensitive), if any."""
    row = conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
    return None if row is None else _file_from_row(row)


def find_files_by_sha256(conn: sqlite3.Connection, sha256: str) -> list[FileRecord]:
    """Return every recorded file with this content hash."""
    rows = conn.execute("SELECT * FROM files WHERE sha256 = ? ORDER BY id", (sha256,))
    return [_file_from_row(row) for row in rows]


def upsert_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    sha256: str | None,
    size: int | None,
    mtime: float | None,
) -> int:
    """Record (or refresh) the file at ``path`` and return its id."""
    existing = get_file_by_path(conn, path)
    if existing is not None:
        changed = existing.sha256 is not None and sha256 is not None and existing.sha256 != sha256
        conn.execute(
            "UPDATE files SET sha256 = ?, size = ?, mtime = ?"
            + (", summary = NULL" if changed else "")  # the old summary no longer fits
            + " WHERE id = ?",
            (sha256, size, mtime, existing.id),
        )
        return existing.id
    cursor = conn.execute(
        "INSERT INTO files (path, sha256, size, mtime, ext, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (str(path), sha256, size, mtime, Path(path).suffix.lower(), now_iso()),
    )
    return cursor.lastrowid


def set_file_location(
    conn: sqlite3.Connection, file_id: int, path: str | Path, status: str
) -> None:
    """Point a file record at its new path and status after a move.

    A stale record still claiming ``path`` (a file that left without the app noticing)
    is dropped first. This only removes database metadata, never a file on disk.
    """
    with transaction(conn):
        conn.execute("DELETE FROM files WHERE path = ? AND id != ?", (str(path), file_id))
        conn.execute(
            "UPDATE files SET path = ?, status = ? WHERE id = ?", (str(path), status, file_id)
        )


def set_file_content(
    conn: sqlite3.Connection, file_id: int, *, title: str | None, text_excerpt: str | None
) -> None:
    """Store what extraction found: the title and the start of the text (for previews)."""
    conn.execute(
        "UPDATE files SET title = ?, text_excerpt = ? WHERE id = ?", (title, text_excerpt, file_id)
    )


def get_file_content(conn: sqlite3.Connection, file_id: int) -> tuple[str | None, str | None]:
    """Return the stored (title, text_excerpt) of a file."""
    row = conn.execute("SELECT title, text_excerpt FROM files WHERE id = ?", (file_id,)).fetchone()
    return (None, None) if row is None else (row["title"], row["text_excerpt"])


def file_contents(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """(title, text_excerpt) of every file that has either, keyed by casefolded path."""
    rows = conn.execute(
        "SELECT path, title, text_excerpt FROM files"
        " WHERE title IS NOT NULL OR text_excerpt IS NOT NULL"
    )
    return {row["path"].casefold(): (row["title"], row["text_excerpt"]) for row in rows}


def set_file_status(conn: sqlite3.Connection, file_id: int, status: str) -> None:
    """Set a file's status ('new', 'sorted', 'inbox' or 'ignored')."""
    conn.execute("UPDATE files SET status = ? WHERE id = ?", (status, file_id))


def set_suggestion(conn: sqlite3.Connection, file_id: int, folder: str | None) -> None:
    """Remember the folder AutoShelf chose or suggested for a file (None: no idea)."""
    conn.execute("UPDATE files SET suggestion = ? WHERE id = ?", (folder, file_id))


# --- file vectors -------------------------------------------------------------


@dataclass(frozen=True)
class StoredVector:
    """A row of ``file_vectors``: an embedding and what it was made from."""

    model: str
    size: int
    mtime: float
    source: str
    vector: bytes


def get_vector(conn: sqlite3.Connection, file_id: int) -> StoredVector | None:
    """Return the stored embedding of a file, if any."""
    row = conn.execute(
        "SELECT model, size, mtime, source, vector FROM file_vectors WHERE file_id = ?",
        (file_id,),
    ).fetchone()
    if row is None:
        return None
    return StoredVector(row["model"], row["size"], row["mtime"], row["source"], row["vector"])


def set_vector(
    conn: sqlite3.Connection,
    file_id: int,
    *,
    model: str,
    size: int,
    mtime: float,
    source: str,
    vector: bytes,
) -> None:
    """Store (or replace) a file's embedding."""
    conn.execute(
        "INSERT INTO file_vectors (file_id, model, size, mtime, source, vector)"
        " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (file_id) DO UPDATE SET model = excluded.model,"
        " size = excluded.size, mtime = excluded.mtime, source = excluded.source,"
        " vector = excluded.vector",
        (file_id, model, size, mtime, source, vector),
    )


# --- folders and corrections ----------------------------------------------------


def folder_descriptions(conn: sqlite3.Connection) -> dict[str, str]:
    """Return every non-empty folder description, keyed by casefolded folder key."""
    rows = conn.execute("SELECT path, description FROM folders WHERE description != ''")
    return {row["path"].casefold(): row["description"] for row in rows}


def set_folder_description(conn: sqlite3.Connection, folder: str, description: str) -> None:
    """Set the description of a folder (its key relative to the root, "/"-separated)."""
    with transaction(conn):
        conn.execute(
            "INSERT INTO folders (path, description) VALUES (?, ?)"
            " ON CONFLICT (path) DO UPDATE SET path = excluded.path,"
            " description = excluded.description",
            (folder, description),
        )
        bump_library_version(conn)


LIBRARY_VERSION = "library_version"


def library_version(conn: sqlite3.Connection) -> int:
    """A counter that changes whenever the library changes in a way no file event shows
    (e.g. a folder description), so every process's index knows to refresh."""
    return int(get_setting(conn, LIBRARY_VERSION, 0))


def bump_library_version(conn: sqlite3.Connection) -> None:
    """Tell every library index (in any process) to refresh before its next decision."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, '1')"
        " ON CONFLICT (key) DO UPDATE SET value = CAST(value AS INTEGER) + 1",
        (LIBRARY_VERSION,),
    )


def record_correction(
    conn: sqlite3.Connection,
    *,
    file_id: int | None,
    from_folder: str | None,
    to_folder: str,
    source: str,
    vector: bytes | None = None,
    model: str | None = None,
    history_id: int | None = None,
) -> int:
    """Record that the user put a file in ``to_folder`` (a learning example).

    ``from_folder`` is where AutoShelf had put (or suggested putting) the file, and
    ``vector`` the file's embedding by ``model``, kept so the example outlives the file.
    Library indexes reload their examples before their next decision.
    """
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO corrections (file_id, from_folder, to_folder, source, ts, vector, model,"
            " history_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, from_folder, to_folder, source, now_iso(), vector, model, history_id),
        )
        bump_library_version(conn)
    return cursor.lastrowid


def list_corrections(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return every correction, oldest first, with the file's last known path."""
    return conn.execute(
        "SELECT c.*, f.path AS path FROM corrections AS c LEFT JOIN files AS f ON f.id = c.file_id"
        " ORDER BY c.id"
    ).fetchall()


@dataclass(frozen=True)
class LearningExample:
    """A file the user moved away from a folder AutoShelf had chosen or suggested."""

    from_folder: str
    to_folder: str
    vector: bytes
    path: str | None


def learning_examples(conn: sqlite3.Connection, model: str) -> list[LearningExample]:
    """Corrections that moved a file away from a folder, with vectors made by ``model``."""
    rows = conn.execute(
        "SELECT c.from_folder, c.to_folder, c.vector, f.path FROM corrections AS c"
        " LEFT JOIN files AS f ON f.id = c.file_id"
        " WHERE c.model = ? AND c.vector IS NOT NULL AND c.from_folder IS NOT NULL"
        " AND c.from_folder != c.to_folder COLLATE NOCASE ORDER BY c.id",
        (model,),
    )
    return [
        LearningExample(row["from_folder"], row["to_folder"], row["vector"], row["path"])
        for row in rows
    ]


# --- rules --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleRecord:
    """A row of the ``rules`` table. Lower ``priority`` values are checked first."""

    id: int
    name: str
    priority: int
    enabled: bool
    kind: str
    pattern: str
    target_folder: str


def _rule_from_row(row: sqlite3.Row) -> RuleRecord:
    return RuleRecord(
        id=row["id"],
        name=row["name"],
        priority=row["priority"],
        enabled=bool(row["enabled"]),
        kind=row["kind"],
        pattern=row["pattern"],
        target_folder=row["target_folder"],
    )


def list_rules(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[RuleRecord]:
    """Return the rules in the order they are checked."""
    where = " WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(f"SELECT * FROM rules{where} ORDER BY priority, id")
    return [_rule_from_row(row) for row in rows]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> RuleRecord | None:
    """Return one rule."""
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return None if row is None else _rule_from_row(row)


def insert_rule(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    pattern: str,
    target_folder: str,
    enabled: bool = True,
) -> int:
    """Add a rule after all existing ones and return its id."""
    with transaction(conn):
        last = conn.execute("SELECT MAX(priority) FROM rules").fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO rules (name, priority, enabled, kind, pattern, target_folder)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, (last or 0) + 1, int(enabled), kind, pattern, target_folder),
        )
    return cursor.lastrowid


def update_rule(conn: sqlite3.Connection, rule: RuleRecord) -> None:
    """Store every field of ``rule`` (matched by id)."""
    conn.execute(
        "UPDATE rules SET name = ?, priority = ?, enabled = ?, kind = ?, pattern = ?,"
        " target_folder = ? WHERE id = ?",
        (
            rule.name,
            rule.priority,
            int(rule.enabled),
            rule.kind,
            rule.pattern,
            rule.target_folder,
            rule.id,
        ),
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    """Remove a rule (rules are settings, not user files)."""
    conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def reorder_rules(conn: sqlite3.Connection, rule_ids: list[int]) -> None:
    """Give the rules priorities 1, 2, 3, ... in the order of ``rule_ids``."""
    with transaction(conn):
        for priority, rule_id in enumerate(rule_ids, start=1):
            conn.execute("UPDATE rules SET priority = ? WHERE id = ?", (priority, rule_id))


# --- files that moved outside AutoShelf -------------------------------------------------


def find_files_by_size(conn: sqlite3.Connection, size: int) -> list[FileRecord]:
    """Return every recorded file of this size."""
    rows = conn.execute("SELECT * FROM files WHERE size = ? ORDER BY id", (size,))
    return [_file_from_row(row) for row in rows]


def path_of_version(conn: sqlite3.Connection, size: int, mtime_ns: int) -> str | None:
    """Where the recorded file of this size and modification time is now, if exactly one
    such file is known (moves keep both)."""
    matches = [
        record.path
        for record in find_files_by_size(conn, size)
        if record.mtime is not None and abs(record.mtime - mtime_ns / 1e9) <= 1e-3
    ]
    return matches[0] if len(matches) == 1 else None


def has_pending_history(conn: sqlite3.Connection, file_id: int) -> bool:
    """True if a move of this file is in progress (its history row isn't settled yet)."""
    row = conn.execute(
        "SELECT 1 FROM history WHERE file_id = ? AND state = 'pending' LIMIT 1", (file_id,)
    ).fetchone()
    return row is not None


def relocate_file(
    conn: sqlite3.Connection, file_id: int, old_path: str | Path, new_path: str | Path, status: str
) -> bool:
    """Point a record at ``new_path``, but only if it still says ``old_path``.

    Call inside a transaction. Returns False if another process got there first, so
    a move noticed twice is only acted on once. A stale record still claiming
    ``new_path`` is dropped (database metadata only, never a file).
    """
    cursor = conn.execute(
        "UPDATE files SET path = ?, status = ? WHERE id = ? AND path = ?",
        (str(new_path), status, file_id, str(old_path)),
    )
    if cursor.rowcount != 1:
        return False
    conn.execute("DELETE FROM files WHERE path = ? AND id != ?", (str(new_path), file_id))
    return True


def get_vector_blob(conn: sqlite3.Connection, file_id: int) -> tuple[str, bytes] | None:
    """The (model, vector) stored for a file, if any."""
    row = conn.execute(
        "SELECT model, vector FROM file_vectors WHERE file_id = ?", (file_id,)
    ).fetchone()
    return None if row is None else (row["model"], row["vector"])


# --- history ----------------------------------------------------------------

UNDOABLE_ACTIONS = ("move", "rename", "create")  # undoing a create sets it aside in _Trash


@dataclass(frozen=True)
class HistoryEntry:
    """A row of the ``history`` table."""

    id: int
    ts: str
    file_id: int | None
    action: str
    src: str | None
    dst: str | None
    reason: str
    confidence: float | None
    details: dict[str, Any]
    batch_id: str | None
    state: str
    root: str | None
    sha256: str | None
    undo_of: int | None


def _history_from_row(row: sqlite3.Row) -> HistoryEntry:
    return HistoryEntry(
        id=row["id"],
        ts=row["ts"],
        file_id=row["file_id"],
        action=row["action"],
        src=row["src"],
        dst=row["dst"],
        reason=row["reason"],
        confidence=row["confidence"],
        details=json.loads(row["details_json"]),
        batch_id=row["batch_id"],
        state=row["state"],
        root=row["root"],
        sha256=row["sha256"],
        undo_of=row["undo_of"],
    )


def insert_history(
    conn: sqlite3.Connection,
    *,
    action: str,
    src: str | Path,
    dst: str | Path,
    reason: str,
    root: str | Path,
    state: str = "pending",
    file_id: int | None = None,
    sha256: str | None = None,
    confidence: float | None = None,
    batch_id: str | None = None,
    undo_of: int | None = None,
    details: dict[str, Any] | None = None,
) -> int:
    """Insert a history row (``pending`` by default) and return its id.

    The connection is in autocommit mode, so the row is durable when this returns.
    """
    cursor = conn.execute(
        "INSERT INTO history (ts, file_id, action, src, dst, reason, confidence, details_json,"
        " batch_id, state, root, sha256, undo_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            file_id,
            action,
            str(src),
            str(dst),
            reason,
            confidence,
            json.dumps(details or {}),
            batch_id,
            state,
            str(root),
            sha256,
            undo_of,
        ),
    )
    return cursor.lastrowid


def update_history_dst(conn: sqlite3.Connection, history_id: int, dst: str | Path) -> None:
    """Change the planned destination of a pending row (used when a name was taken)."""
    conn.execute("UPDATE history SET dst = ? WHERE id = ?", (str(dst), history_id))


def set_history_file(conn: sqlite3.Connection, history_id: int, file_id: int) -> None:
    """Link a row to the file record it is about (a created file only gets one once made)."""
    conn.execute("UPDATE history SET file_id = ? WHERE id = ?", (file_id, history_id))


def set_history_state(
    conn: sqlite3.Connection, history_id: int, state: str, *, note: str | None = None
) -> None:
    """Set a row's state, optionally recording a note (e.g. an error) in its details."""
    if note is None:
        conn.execute("UPDATE history SET state = ? WHERE id = ?", (state, history_id))
    else:
        conn.execute(
            "UPDATE history SET state = ?, details_json = json_set(details_json, '$.note', ?)"
            " WHERE id = ?",
            (state, note, history_id),
        )


def get_history(conn: sqlite3.Connection, history_id: int) -> HistoryEntry | None:
    """Return one history row."""
    row = conn.execute("SELECT * FROM history WHERE id = ?", (history_id,)).fetchone()
    return None if row is None else _history_from_row(row)


def list_history(conn: sqlite3.Connection, limit: int = 50) -> list[HistoryEntry]:
    """Return the most recent history rows, newest first."""
    rows = conn.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))
    return [_history_from_row(row) for row in rows]


def list_history_in_state(conn: sqlite3.Connection, state: str) -> list[HistoryEntry]:
    """Return every row in ``state``, oldest first."""
    rows = conn.execute("SELECT * FROM history WHERE state = ? ORDER BY id", (state,))
    return [_history_from_row(row) for row in rows]


def last_undoable(conn: sqlite3.Connection, count: int) -> list[HistoryEntry]:
    """Return the ``count`` most recent completed moves, renames and creates, newest first."""
    marks = ", ".join("?" * len(UNDOABLE_ACTIONS))
    rows = conn.execute(
        f"SELECT * FROM history WHERE state = 'done' AND action IN ({marks})"
        " ORDER BY id DESC LIMIT ?",
        (*UNDOABLE_ACTIONS, count),
    )
    return [_history_from_row(row) for row in rows]


def top_suggestion(conn: sqlite3.Connection, file_id: int) -> str | None:
    """The best folder the classifier suggested in a file's most recent decision, if any."""
    row = conn.execute(
        "SELECT json_extract(details_json, '$.candidates[0].folder') AS folder FROM history"
        " WHERE file_id = ? AND json_extract(details_json, '$.candidates[0].folder') IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    return None if row is None else row["folder"]


def last_decision(conn: sqlite3.Connection, file_id: int) -> HistoryEntry | None:
    """The most recent move of a file made by AutoShelf's own decision (auto, ask or a rule)."""
    row = conn.execute(
        "SELECT * FROM history WHERE file_id = ? AND action = 'move'"
        " AND (reason IN ('auto', 'ask') OR reason LIKE 'Rule:%') ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    return None if row is None else _history_from_row(row)


def list_batch(conn: sqlite3.Connection, batch_id: str) -> list[HistoryEntry]:
    """Every row of one batch, oldest first."""
    rows = conn.execute("SELECT * FROM history WHERE batch_id = ? ORDER BY id", (batch_id,))
    return [_history_from_row(row) for row in rows]


def undos_with_stale_target(conn: sqlite3.Connection) -> list[int]:
    """Return ids of rows that a completed undo reversed but that aren't marked undone yet."""
    rows = conn.execute(
        "SELECT target.id FROM history AS u JOIN history AS target ON target.id = u.undo_of"
        " WHERE u.action = 'undo' AND u.state = 'done' AND target.state = 'done'"
    )
    return [row["id"] for row in rows]


# --- chunks (search) ------------------------------------------------------------------


def replace_chunks(
    conn: sqlite3.Connection, file_id: int, texts: list[str], vectors: list[bytes]
) -> None:
    """Store a file's text chunks and their vectors, replacing any older ones."""
    rows = [
        (file_id, idx, text, vector)
        for idx, (text, vector) in enumerate(zip(texts, vectors, strict=True))
    ]
    with transaction(conn):
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        conn.executemany(
            "INSERT INTO chunks (file_id, idx, text, embedding) VALUES (?, ?, ?, ?)", rows
        )


def has_chunks(conn: sqlite3.Connection, file_id: int) -> bool:
    """True if a file's text has been stored for search."""
    row = conn.execute("SELECT 1 FROM chunks WHERE file_id = ? LIMIT 1", (file_id,)).fetchone()
    return row is not None


def chunks_signature(conn: sqlite3.Connection) -> tuple[int, int]:
    """(number of chunks, highest chunk id): changes whenever chunks are added or removed."""
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM chunks").fetchone()
    return row[0], row[1]


def searchable_chunks(conn: sqlite3.Connection, model: str) -> list[sqlite3.Row]:
    """Every chunk whose file's vector was made by ``model``, with the file's path."""
    return conn.execute(
        "SELECT c.id, c.file_id, c.idx, c.text, c.embedding, f.path, f.status"
        " FROM chunks AS c JOIN files AS f ON f.id = c.file_id"
        " JOIN file_vectors AS v ON v.file_id = c.file_id"
        " WHERE v.model = ? AND c.embedding IS NOT NULL ORDER BY c.file_id, c.idx",
        (model,),
    ).fetchall()


def chunks_matching(conn: sqlite3.Connection, phrase: str) -> set[int]:
    """Ids of chunks containing ``phrase`` (any case; three characters or more)."""
    quoted = '"' + phrase.replace('"', '""') + '"'
    rows = conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (quoted,))
    return {row[0] for row in rows}


def vector_model(conn: sqlite3.Connection, path: str | Path) -> str | None:
    """The model that made the stored vector of the file at ``path``."""
    row = conn.execute(
        "SELECT v.model FROM file_vectors AS v JOIN files AS f ON f.id = v.file_id"
        " WHERE f.path = ?",
        (str(path),),
    ).fetchone()
    return None if row is None else row[0]


def file_vectors(conn: sqlite3.Connection, model: str) -> list[tuple[int, str, bytes]]:
    """(file id, path, vector) of every file with a vector made by ``model``."""
    rows = conn.execute(
        "SELECT f.id, f.path, v.vector FROM file_vectors AS v JOIN files AS f ON f.id = v.file_id"
        " WHERE v.model = ? ORDER BY f.id",
        (model,),
    )
    return [(row[0], row[1], row[2]) for row in rows]


def set_summary(conn: sqlite3.Connection, file_id: int, summary: str | None) -> None:
    """Cache a file's summary."""
    conn.execute("UPDATE files SET summary = ? WHERE id = ?", (summary, file_id))


def summaries(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, str]:
    """Cached summaries of these files (only those that have one)."""
    found: dict[int, str] = {}
    for start in range(0, len(file_ids), 500):
        part = file_ids[start : start + 500]
        marks = ", ".join("?" * len(part))
        rows = conn.execute(
            f"SELECT id, summary FROM files WHERE summary IS NOT NULL AND id IN ({marks})", part
        )
        found.update({row[0]: row[1] for row in rows})
    return found


def get_summary(conn: sqlite3.Connection, file_id: int) -> str | None:
    """A file's cached summary, if any."""
    row = conn.execute("SELECT summary FROM files WHERE id = ?", (file_id,)).fetchone()
    return None if row is None else row["summary"]


def mark_opened(conn: sqlite3.Connection, file_id: int) -> None:
    """Remember that the user opened a file from AutoShelf (for the stats)."""
    conn.execute("UPDATE files SET last_opened_at = ? WHERE id = ?", (now_iso(), file_id))


def last_opened(conn: sqlite3.Connection) -> dict[str, str]:
    """When files were last opened from AutoShelf, keyed by casefolded path."""
    rows = conn.execute("SELECT path, last_opened_at FROM files WHERE last_opened_at IS NOT NULL")
    return {row["path"].casefold(): row["last_opened_at"] for row in rows}


def duplicate_hashes(conn: sqlite3.Connection) -> list[list[FileRecord]]:
    """Recorded files grouped by content hash, for hashes recorded more than once."""
    rows = conn.execute(
        "SELECT * FROM files WHERE sha256 IN (SELECT sha256 FROM files WHERE sha256 IS NOT NULL"
        " GROUP BY sha256 HAVING COUNT(*) > 1) ORDER BY sha256, id"
    )
    groups: dict[str, list[FileRecord]] = {}
    for row in rows:
        groups.setdefault(row["sha256"], []).append(_file_from_row(row))
    return list(groups.values())


# --- tags ---------------------------------------------------------------------------


def add_tag(conn: sqlite3.Connection, file_id: int, name: str) -> str | None:
    """Tag a file. Returns the tag as stored (an existing tag keeps its spelling), or None
    if the file already had it."""
    with transaction(conn):
        conn.execute("INSERT INTO tags (name) VALUES (?) ON CONFLICT (name) DO NOTHING", (name,))
        tag_id, stored = conn.execute(
            "SELECT id, name FROM tags WHERE name = ?", (name,)
        ).fetchone()
        cursor = conn.execute(
            "INSERT INTO file_tags (file_id, tag_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
            (file_id, tag_id),
        )
    return stored if cursor.rowcount == 1 else None


def remove_tag(conn: sqlite3.Connection, file_id: int, name: str) -> bool:
    """Remove a tag from a file. Returns False if it didn't have it."""
    with transaction(conn):
        cursor = conn.execute(
            "DELETE FROM file_tags WHERE file_id = ?"
            " AND tag_id = (SELECT id FROM tags WHERE name = ?)",
            (file_id, name),
        )
        conn.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM file_tags)")
    return cursor.rowcount == 1


def tags_of(conn: sqlite3.Connection, file_id: int) -> list[str]:
    """A file's tags, alphabetically."""
    rows = conn.execute(
        "SELECT t.name FROM tags AS t JOIN file_tags AS ft ON ft.tag_id = t.id"
        " WHERE ft.file_id = ? ORDER BY t.name COLLATE NOCASE",
        (file_id,),
    )
    return [row[0] for row in rows]


def tag_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """Every tag in use with how many files have it, alphabetically."""
    rows = conn.execute(
        "SELECT t.name, COUNT(*) FROM tags AS t JOIN file_tags AS ft ON ft.tag_id = t.id"
        " GROUP BY t.id ORDER BY t.name COLLATE NOCASE"
    )
    return [(row[0], row[1]) for row in rows]


def files_tagged(conn: sqlite3.Connection, name: str) -> list[FileRecord]:
    """Every file with the tag ``name`` (any case)."""
    rows = conn.execute(
        "SELECT f.* FROM files AS f JOIN file_tags AS ft ON ft.file_id = f.id"
        " JOIN tags AS t ON t.id = ft.tag_id WHERE t.name = ? ORDER BY f.path COLLATE NOCASE",
        (name,),
    )
    return [_file_from_row(row) for row in rows]


# --- statistics ---------------------------------------------------------------------


def decision_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Moves AutoShelf decided itself: automatic ones, rule-based ones, and automatic ones
    the user reversed (undid, or corrected in History, Explorer or on the iPhone)."""
    auto = conn.execute(
        "SELECT COUNT(*) FROM history WHERE action = 'move' AND reason = 'auto'"
        " AND state IN ('done', 'undone')"
    ).fetchone()[0]
    rules = conn.execute(
        "SELECT COUNT(*) FROM history WHERE action = 'move' AND reason LIKE 'Rule:%'"
        " AND state IN ('done', 'undone')"
    ).fetchone()[0]
    corrected = conn.execute(
        "SELECT COUNT(*) FROM history AS h WHERE h.action = 'move' AND h.reason = 'auto'"
        " AND (h.state = 'undone' OR EXISTS (SELECT 1 FROM corrections AS c"
        " WHERE c.history_id = h.id AND c.source IN ('undo', 'external')"
        " AND (c.from_folder IS NULL OR c.from_folder != c.to_folder COLLATE NOCASE)))"
    ).fetchone()[0]
    return {"auto": auto, "rule": rules, "auto_corrected": corrected}


def corrected_pairs(conn: sqlite3.Connection, limit: int = 10) -> list[tuple[str, str, int]]:
    """(from folder, to folder, count) of corrections, most frequent first."""
    rows = conn.execute(
        "SELECT from_folder, to_folder, COUNT(*) AS n FROM corrections"
        " WHERE from_folder IS NOT NULL AND from_folder != to_folder COLLATE NOCASE"
        " GROUP BY from_folder COLLATE NOCASE, to_folder COLLATE NOCASE"
        " ORDER BY n DESC, from_folder LIMIT ?",
        (limit,),
    )
    return [(row[0], row[1], row[2]) for row in rows]


# --- flashcards ------------------------------------------------------------------------


@dataclass(frozen=True)
class Flashcard:
    """A question and its answer, made from a file (or a folder)."""

    id: int
    question: str
    answer: str
    folder: str | None
    path: str | None


def add_flashcards(
    conn: sqlite3.Connection,
    cards: list[tuple[str, str]],
    *,
    file_id: int | None,
    folder: str | None,
) -> list[int]:
    """Store (question, answer) cards and return their ids."""
    ids = []
    with transaction(conn):
        for question, answer in cards:
            cursor = conn.execute(
                "INSERT INTO flashcards (file_id, folder, question, answer, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (file_id, folder, question, answer, now_iso()),
            )
            ids.append(cursor.lastrowid)
    return ids


def list_flashcards(conn: sqlite3.Connection, folder: str | None = None) -> list[Flashcard]:
    """Cards of one folder (any case; subfolders included), or all cards."""
    rows = conn.execute(
        "SELECT c.id, c.question, c.answer, c.folder, f.path FROM flashcards AS c"
        " LEFT JOIN files AS f ON f.id = c.file_id ORDER BY c.id"
    )
    cards = [Flashcard(row[0], row[1], row[2], row[3], row[4]) for row in rows]
    if folder is None:
        return cards
    wanted = folder.casefold()
    return [
        card
        for card in cards
        if card.folder is not None
        and (card.folder.casefold() == wanted or card.folder.casefold().startswith(wanted + "/"))
    ]


def delete_flashcards(conn: sqlite3.Connection, card_ids: list[int]) -> None:
    """Remove cards (they are AutoShelf's own data, not user files)."""
    conn.executemany("DELETE FROM flashcards WHERE id = ?", [(card_id,) for card_id in card_ids])
