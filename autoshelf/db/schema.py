"""SQL for each schema version. Never edit a released version; add a new one instead."""

V1 = """
CREATE TABLE files (
    id             INTEGER PRIMARY KEY,
    path           TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    sha256         TEXT,
    size           INTEGER,
    mtime          REAL,
    ext            TEXT,
    title          TEXT,
    text_excerpt   TEXT,
    summary        TEXT,
    status         TEXT    NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'sorted', 'inbox', 'ignored')),
    added_at       TEXT    NOT NULL,
    last_opened_at TEXT
);
CREATE INDEX files_sha256 ON files (sha256);

CREATE TABLE chunks (
    id        INTEGER PRIMARY KEY,
    file_id   INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    text      TEXT    NOT NULL,
    embedding BLOB,
    UNIQUE (file_id, idx)
);

CREATE VIRTUAL TABLE chunks_fts USING fts5 (text, content = 'chunks', content_rowid = 'id');

CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER chunks_au AFTER UPDATE OF text ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts (rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE folders (
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description       TEXT NOT NULL DEFAULT '',
    profile_embedding BLOB
);

CREATE TABLE tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE file_tags (
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    tag_id  INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (file_id, tag_id)
);

CREATE TABLE rules (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    kind          TEXT    NOT NULL,
    pattern       TEXT    NOT NULL,
    target_folder TEXT    NOT NULL
);

-- Written *before* every move/rename (state 'pending') and marked 'done' after.
-- root and sha256 let undo verify it is moving the same file inside the same root.
CREATE TABLE history (
    id           INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    file_id      INTEGER REFERENCES files (id) ON DELETE SET NULL,
    action       TEXT    NOT NULL CHECK (action IN ('move', 'rename', 'tag', 'undo')),
    src          TEXT,
    dst          TEXT,
    reason       TEXT    NOT NULL DEFAULT '',
    confidence   REAL,
    details_json TEXT    NOT NULL DEFAULT '{}',
    batch_id     TEXT,
    state        TEXT    NOT NULL CHECK (state IN ('pending', 'done', 'undone', 'failed')),
    root         TEXT,
    sha256       TEXT,
    undo_of      INTEGER REFERENCES history (id)
);
CREATE INDEX history_state ON history (state);
CREATE INDEX history_batch ON history (batch_id);

CREATE TABLE corrections (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files (id) ON DELETE SET NULL,
    from_folder TEXT,
    to_folder   TEXT    NOT NULL,
    source      TEXT    NOT NULL CHECK (source IN ('inbox', 'undo', 'external')),
    ts          TEXT    NOT NULL
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

V2 = """
-- One embedding per file, made by the model named in ``model`` from the file as it was
-- at ``size``/``mtime``. A vector from another model or an older version of the file is
-- rebuilt. ``source`` 'name' means only the name was used (an iCloud online-only file).
CREATE TABLE file_vectors (
    file_id INTEGER PRIMARY KEY REFERENCES files (id) ON DELETE CASCADE,
    model   TEXT    NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL    NOT NULL,
    source  TEXT    NOT NULL CHECK (source IN ('content', 'name')),
    vector  BLOB    NOT NULL
);
"""
