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

V3 = """
-- A correction keeps the vector of the corrected file (made by ``model``), so it still
-- counts after the file is edited, renamed or deleted. ``history_id`` is the decision
-- that was corrected, if there was one.
-- ``suggestion`` is the folder AutoShelf last chose or suggested for a file: what a
-- correction corrects.
ALTER TABLE files ADD COLUMN suggestion TEXT;
ALTER TABLE corrections ADD COLUMN vector BLOB;
ALTER TABLE corrections ADD COLUMN model TEXT;
ALTER TABLE corrections ADD COLUMN history_id INTEGER REFERENCES history (id);
CREATE INDEX corrections_file ON corrections (file_id);
CREATE INDEX history_file ON history (file_id);
CREATE INDEX files_size ON files (size);
"""

V4 = """
-- Search (SPEC 5.3): each file's text in chunks, with their vectors (stored together with
-- the file's vector, by the same model). Keyword search uses trigrams, which also find
-- words in Japanese text; queries shorter than three characters are matched with LIKE.
DROP TRIGGER chunks_ai;
DROP TRIGGER chunks_ad;
DROP TRIGGER chunks_au;
DROP TABLE chunks_fts;
CREATE VIRTUAL TABLE chunks_fts USING fts5 (
    text, content = 'chunks', content_rowid = 'id', tokenize = 'trigram'
);
INSERT INTO chunks_fts (chunks_fts) VALUES ('rebuild');
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
CREATE INDEX file_tags_tag ON file_tags (tag_id);
"""

V5 = """
-- Study mode (SPEC 5.11): question-and-answer cards made from a file or a folder.
CREATE TABLE flashcards (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER REFERENCES files (id) ON DELETE SET NULL,
    folder     TEXT,
    question   TEXT NOT NULL,
    answer     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX flashcards_folder ON flashcards (folder);
"""

V6 = """
-- Files AutoShelf makes (a compressed or converted copy, next to the original) are recorded
-- with the action 'create': src is the (first) source file, dst the new file, sha256 the
-- new file's content, reason the operation, details the sources and the operation. SQLite
-- can't change a CHECK constraint, so the table is rebuilt with the same rows and ids.
-- (Runs with foreign keys off: corrections and undo rows refer to history rows.)
CREATE TABLE history_new (
    id           INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,
    file_id      INTEGER REFERENCES files (id) ON DELETE SET NULL,
    action       TEXT    NOT NULL
                 CHECK (action IN ('move', 'rename', 'tag', 'undo', 'create')),
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
INSERT INTO history_new (id, ts, file_id, action, src, dst, reason, confidence, details_json,
                         batch_id, state, root, sha256, undo_of)
    SELECT id, ts, file_id, action, src, dst, reason, confidence, details_json,
           batch_id, state, root, sha256, undo_of FROM history;
DROP TABLE history;
ALTER TABLE history_new RENAME TO history;
CREATE INDEX history_state ON history (state);
CREATE INDEX history_batch ON history (batch_id);
CREATE INDEX history_file ON history (file_id);
"""

V7 = """
-- Rules with several conditions (all of them, or any one with match_any) and several
-- actions (Phase 12): move (target_folder; '' to let AutoShelf decide), add tags, and
-- rename from a template such as '{date} {title}'. kind and pattern keep the first
-- condition. Existing rules become rules with one condition.
ALTER TABLE rules ADD COLUMN conditions_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE rules ADD COLUMN match_any INTEGER NOT NULL DEFAULT 0;
ALTER TABLE rules ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE rules ADD COLUMN rename_template TEXT NOT NULL DEFAULT '';
UPDATE rules SET conditions_json = json_array(json_object('kind', kind, 'pattern', pattern));
"""
