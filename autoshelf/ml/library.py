"""The library: the user's category folders and the files already in them.

Files the user keeps in category folders are the classifier's labelled examples.
``LibraryIndex`` holds a vector for each of them, kept in step with the disk: every
``refresh`` rescans the folders, but only reads and embeds files that are new or
changed. Vectors are cached in the database, so a restart doesn't recompute them.
A new file that turns out to be a known file the user moved is adopted instead (see
``actions.learning``), which is how corrections made in Explorer or on the iPhone are
learned.

A rescan lists every folder, so the watcher doesn't rescan before every file: it marks
the index dirty when its folders change (``mark_dirty``) and calls ``ensure_current``,
which rescans only then, or when the library version in the database changed (another
process edited a folder description, which no file event shows).
"""

import logging
import os
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np

from autoshelf.config import DEFAULT_AUTO_MARGIN, DEFAULT_AUTO_THRESHOLD, DEFAULT_IGNORE_PATTERNS
from autoshelf.db import repository
from autoshelf.db.connection import transaction
from autoshelf.extract import FILENAME_ONLY, Extraction, extract, guard
from autoshelf.extract.filename import filename_tokens, name_tokens
from autoshelf.fsutil import is_online_only, sha256_file
from autoshelf.layout import RESERVED_PREFIX, Zone, zone_of
from autoshelf.ml import explain
from autoshelf.ml.classifier import Classification, FolderProfile, choose, score_folders
from autoshelf.ml.embedder import Embedder
from autoshelf.ml.learned import Example, adjustments
from autoshelf.ml.vectors import EmbeddedDocument, embed_document, from_blob, to_blob
from autoshelf.watcher.ignore import matching_pattern

log = logging.getLogger(__name__)

# How much extracted text is stored with each file for previews (the Inbox, Phase 4).
TEXT_EXCERPT_CHARS = 1000

CONTENT = "content"
NAME_ONLY = "name"

_HIDDEN_OR_SYSTEM = 0x2 | 0x4  # FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM
_MTIME_TOLERANCE = 1e-3


@dataclass(frozen=True)
class LibraryFile:
    """A file found in a category folder."""

    path: Path
    folder: str  # key of the folder it is in, e.g. "Machine Learning/Unsupervised"
    size: int
    mtime: float
    online_only: bool


@dataclass(frozen=True)
class LibraryScan:
    """Category folders (as "/"-separated keys relative to the root) and their files."""

    folders: tuple[str, ...]
    files: tuple[LibraryFile, ...]


def scan_library(
    root: Path, ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS
) -> LibraryScan:
    """List category folders and the files in them, without opening any file.

    Skips files directly in the root, ``_``-prefixed (reserved) folders such as
    ``_Inbox``, hidden or system entries, ignored names, links and junctions.
    """
    patterns = tuple(ignore_patterns)
    folders: list[str] = []
    files: list[LibraryFile] = []
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    while pending:
        directory, parts = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            log.warning("Couldn't list %s: %s", directory, exc)
            continue
        for entry in entries:
            try:
                if _skipped(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.startswith(RESERVED_PREFIX):
                        continue
                    key = (*parts, entry.name)
                    folders.append("/".join(key))
                    pending.append((Path(entry.path), key))
                elif parts and entry.is_file(follow_symlinks=False):
                    if matching_pattern(entry.name, patterns) is not None:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    files.append(
                        LibraryFile(
                            path=Path(entry.path),
                            folder="/".join(parts),
                            size=st.st_size,
                            mtime=st.st_mtime,
                            online_only=is_online_only(st),
                        )
                    )
            except OSError:
                continue  # vanished while scanning
    return LibraryScan(tuple(sorted(folders, key=str.casefold)), tuple(files))


def _skipped(entry: os.DirEntry) -> bool:
    if entry.name.startswith(".") or entry.is_symlink() or entry.is_junction():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & _HIDDEN_OR_SYSTEM)


def folder_label(key: str) -> str:
    """Readable folder path: "MachineLearning/ml_basics" becomes "Machine Learning / ml basics"."""
    return " / ".join(" ".join(name_tokens(part)) or part for part in key.split("/"))


def profile_text(key: str, description: str = "") -> str:
    """The text a folder is embedded from: its path, plus the user's description."""
    label = folder_label(key)
    return f"{label}: {description.strip()}" if description.strip() else label


@dataclass(frozen=True)
class IndexedFile:
    """A file in a category folder with its vector."""

    path: Path
    folder: str
    size: int
    mtime: float
    source: str
    vector: np.ndarray

    def matches(self, item: LibraryFile) -> bool:
        """True if the vector was made from the file at its current size and time."""
        return self.size == item.size and abs(self.mtime - item.mtime) <= _MTIME_TOLERANCE

    def is_current(self, item: LibraryFile) -> bool:
        """True if the vector still describes ``item`` as it is on disk."""
        if not self.matches(item):
            return False
        if self.source != NAME_ONLY or item.online_only:
            return True
        # A name-only vector is upgraded once the online-only file has been downloaded,
        # or once a file that crashed AutoShelf is cleared to be read again (Settings).
        return guard.crashed_before(item.path)


@dataclass(frozen=True)
class RefreshStats:
    """What a refresh found and did."""

    folders: int
    files: int
    embedded: int
    unreadable: int
    relocations: tuple = ()  # actions.learning.Relocation: files the user moved


def store_content(conn: sqlite3.Connection, file_id: int, extraction: Extraction) -> None:
    """Store a file's title and the start of its text (for previews)."""
    repository.set_file_content(
        conn,
        file_id,
        title=extraction.title,
        text_excerpt=extraction.text[:TEXT_EXCERPT_CHARS] or None,
    )


def vector_source(extraction: Extraction) -> str:
    """How a vector made from ``extraction`` is stored: from the content, or from the name
    only (a file that crashed AutoShelf before; it is read again once cleared)."""
    return NAME_ONLY if extraction.error == guard.CRASHED else CONTENT


def store_vector(
    conn: sqlite3.Connection,
    file_id: int,
    document: EmbeddedDocument,
    *,
    model: str,
    size: int,
    mtime: float,
    source: str = CONTENT,
) -> None:
    """Store a file's vector with the model and file version it was made from, and its
    text chunks with their vectors (for search)."""
    repository.set_vector(
        conn,
        file_id,
        model=model,
        size=size,
        mtime=mtime,
        source=source,
        vector=to_blob(document.vector),
    )
    repository.replace_chunks(
        conn, file_id, list(document.texts), [to_blob(row) for row in document.vectors]
    )


class LibraryIndex:
    """Vectors and profiles of the category folders under ``root``. Use from one thread."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        root: Path,
        embedder: Embedder,
        *,
        ignore_patterns: Iterable[str] = DEFAULT_IGNORE_PATTERNS,
        extractor: Callable[[Path], Extraction] = extract,
    ) -> None:
        self.conn = conn
        self.root = root
        self.embedder = embedder
        self.ignore_patterns = tuple(ignore_patterns)
        self.extractor = extractor
        self.folders: tuple[str, ...] = ()
        self._entries: dict[Path, IndexedFile] = {}
        self._profile_vectors: dict[str, np.ndarray] = {}
        self._examples: list[Example] = []
        self._documents: dict[str, str] | None = None
        self._dirty = True  # nothing scanned yet
        self._version: int | None = None  # the library version the last refresh saw

    @property
    def model(self) -> str:
        """The embedding model this index uses."""
        return self.embedder.model_id

    @property
    def dirty(self) -> bool:
        """True if the folders may have changed since the last ``refresh``."""
        return self._dirty

    def mark_dirty(self) -> None:
        """Note that the folders changed on disk: ``ensure_current`` will rescan them."""
        self._dirty = True

    def ensure_current(
        self, progress: Callable[[int, int], None] | None = None
    ) -> RefreshStats | None:
        """Rescan if the folders changed since the last refresh (``mark_dirty``) or the
        library version in the database did. Returns the refresh's stats, or None."""
        if not self._dirty and repository.library_version(self.conn) == self._version:
            return None
        return self.refresh(progress)

    def add_file(self, path: Path, document: EmbeddedDocument, *, size: int, mtime: float) -> None:
        """Count a file AutoShelf just sorted into a folder, without a rescan.

        Its vector is stored by the caller; this only keeps the index in step.
        """
        folder = _folder_key(path, self.root)
        if folder is None or folder not in self.folders:
            self._dirty = True  # a folder the index doesn't know yet: rescan
            return
        self._entries[path] = IndexedFile(path, folder, size, mtime, CONTENT, document.vector)
        self._documents = None

    def embed(self, extraction: Extraction, name: str) -> EmbeddedDocument:
        """Embed a file from what was extracted from it, keeping its chunks."""
        return embed_document(self.embedder, extraction, name)

    def vectorize(self, extraction: Extraction, name: str) -> np.ndarray:
        """Embed a file from what was extracted from it."""
        return self.embed(extraction, name).vector

    def stored_vector(self, path: Path) -> np.ndarray | None:
        """The vector stored for the file at ``path``, if it still matches the file."""
        try:
            st = path.stat()
        except OSError:
            return None
        record = repository.get_file_by_path(self.conn, path)
        stored = None if record is None else repository.get_vector(self.conn, record.id)
        if (
            stored is None
            or stored.model != self.model
            or stored.size != st.st_size
            or abs(stored.mtime - st.st_mtime) > _MTIME_TOLERANCE
        ):
            return None
        if stored.source == NAME_ONLY and not is_online_only(st) and not guard.crashed_before(path):
            return None  # its content can be read now: make a vector from it
        return from_blob(stored.vector)

    def classify(
        self,
        vector: np.ndarray,
        *,
        threshold: float = DEFAULT_AUTO_THRESHOLD,
        margin: float = DEFAULT_AUTO_MARGIN,
        exclude: Path | None = None,
    ) -> Classification:
        """Score every folder for ``vector`` and decide (see ``classifier.choose``).

        Folders that similar files were moved away from score lower (``ml.learned``).
        ``exclude`` keeps a file that is already in a folder from matching itself.
        """
        learned = adjustments(vector, self._examples)
        scores = score_folders(
            vector,
            self.profiles(),
            adjustments={key.casefold(): item.amount for key, item in learned.items()},
            exclude=exclude,
        )
        return replace(choose(scores, threshold, margin), learned=learned)

    def explain(
        self,
        classification: Classification,
        extraction: Extraction,
        name: str,
        folder: str | None = None,
    ) -> dict:
        """The stored "why" of a decision: scores, similar files, shared words, learning.

        Words are compared with ``folder``, else the chosen folder, else the best one.
        """
        focus = folder
        if focus is None and classification.choice is not None:
            focus = classification.choice.folder
        if focus is None and classification.scores:
            focus = classification.scores[0].folder
        keywords: list[str] = []
        if focus is not None:
            documents = self.folder_documents()
            file_text = explain.join_texts(
                [
                    " ".join(extraction.filename_tokens) or name,
                    extraction.title or "",
                    extraction.text,
                ]
            )
            keywords = explain.shared_keywords(
                file_text, documents.get(focus, ""), list(documents.values())
            )
        return explain.decision_details(
            classification, keywords=keywords, learned=classification.learned, folder=focus
        )

    def folder_documents(self) -> dict[str, str]:
        """Text describing each folder: name, description, and its files' names and text."""
        if self._documents is None:
            descriptions = repository.folder_descriptions(self.conn)
            contents = repository.file_contents(self.conn)  # one query for every file
            texts = {
                key: [profile_text(key, descriptions.get(key.casefold(), ""))]
                for key in self.folders
            }
            for entry in self._entries.values():
                title, excerpt = contents.get(str(entry.path).casefold(), (None, None))
                words = " ".join(filename_tokens(entry.path.name))
                for key in _self_and_parents(entry.folder):
                    if key in texts:
                        texts[key].extend(part for part in (words, title, excerpt) if part)
            self._documents = {key: explain.join_texts(parts) for key, parts in texts.items()}
        return self._documents

    def refresh(self, progress: Callable[[int, int], None] | None = None) -> RefreshStats:
        """Rescan the folders, embedding files that are new or changed since last time.

        ``progress(done, total)`` is called after each file that had to be looked at.
        """
        # Cleared before scanning, so a change noticed during the scan isn't lost.
        self._dirty = False
        version = repository.library_version(self.conn)
        scan = scan_library(self.root, self.ignore_patterns)
        entries: dict[Path, IndexedFile] = {}
        stale: list[LibraryFile] = []
        for item in scan.files:
            known = self._entries.get(item.path) or self._stored_entry(item)
            if known is not None and known.is_current(item):
                entries[item.path] = known
            else:
                stale.append(item)

        unreadable = embedded = 0
        relocations = []
        for done, item in enumerate(stale, start=1):
            digest = None if item.online_only else _hash_or_none(item.path)
            relocation = self._adopt(item, digest)
            entry = None
            if relocation is not None:
                relocations.append(relocation)
                entry = self._stored_entry(item)  # moved as it was: its vector still fits
            if entry is None or not entry.is_current(item):
                entry = self._embed(item, digest)
                embedded += entry is not None
            if entry is None:
                unreadable += 1
            else:
                entries[item.path] = entry
            if progress is not None:
                progress(done, len(stale))

        self.folders = scan.folders
        self._entries = entries
        self._examples = self._load_examples()
        self._documents = None
        self._version = version
        return RefreshStats(
            folders=len(scan.folders),
            files=len(entries),
            embedded=embedded,
            unreadable=unreadable,
            relocations=tuple(relocations),
        )

    def _adopt(self, item: LibraryFile, digest: str | None):
        """If ``item`` is a known file the user moved here, take over its record."""
        if repository.get_file_by_path(self.conn, item.path) is not None:
            return None  # known at this path already: it just changed
        from autoshelf.actions.learning import adopt_moved_file  # actions build on ml

        try:
            return adopt_moved_file(
                self.conn,
                self.root,
                item.path,
                size=item.size,
                online_only=item.online_only,
                digest=digest,
            )
        except sqlite3.Error:
            log.exception("Couldn't check whether %s was moved", item.path)
            return None

    def _load_examples(self) -> list[Example]:
        return [
            Example(
                item.from_folder,
                item.to_folder,
                from_blob(item.vector),
                Path(item.path).name if item.path else "",
            )
            for item in repository.learning_examples(self.conn, self.model)
        ]

    def files_in(self, folder: str) -> list[IndexedFile]:
        """Indexed files in ``folder`` and its subfolders."""
        prefix = folder + "/"
        return [
            entry
            for entry in self._entries.values()
            if entry.folder == folder or entry.folder.startswith(prefix)
        ]

    def direct_file_counts(self) -> dict[str, int]:
        """Number of indexed files directly in each folder."""
        counts = dict.fromkeys(self.folders, 0)
        for entry in self._entries.values():
            counts[entry.folder] = counts.get(entry.folder, 0) + 1
        return counts

    def profiles(self) -> list[FolderProfile]:
        """Profiles of every category folder found by the last ``refresh``."""
        descriptions = repository.folder_descriptions(self.conn)
        texts = {
            key: profile_text(key, descriptions.get(key.casefold(), "")) for key in self.folders
        }
        missing = sorted({text for text in texts.values() if text not in self._profile_vectors})
        if missing:
            for text, vector in zip(missing, self.embedder.embed(missing), strict=True):
                self._profile_vectors[text] = vector
        profiles = []
        for key in self.folders:
            vector = self._profile_vectors[texts[key]]
            members = self.files_in(key)
            pool = (
                np.stack([entry.vector for entry in members])
                if members
                else np.empty((0, vector.shape[0]), dtype=np.float32)
            )
            paths = tuple(entry.path for entry in members)
            profiles.append(FolderProfile(key, vector, pool, paths))
        return profiles

    def _stored_entry(self, item: LibraryFile) -> IndexedFile | None:
        record = repository.get_file_by_path(self.conn, item.path)
        if record is None:
            return None
        stored = repository.get_vector(self.conn, record.id)
        if stored is None or stored.model != self.model:
            return None
        if not repository.has_chunks(self.conn, record.id):
            return None  # indexed before search existed: read it once more
        return IndexedFile(
            item.path,
            item.folder,
            stored.size,
            stored.mtime,
            stored.source,
            from_blob(stored.vector),
        )

    def _embed(self, item: LibraryFile, digest: str | None = None) -> IndexedFile | None:
        """Read and embed one file (by name only if it is online-only) and store it."""
        try:
            if item.online_only:
                # Reading it would download it from iCloud; the name has to do for now.
                extraction = Extraction(FILENAME_ONLY, tuple(filename_tokens(item.path.name)))
                digest, source = None, NAME_ONLY
            else:
                digest = digest or sha256_file(item.path)
                extraction = self.extractor(item.path)
                source = vector_source(extraction)
        except OSError as exc:
            log.warning("Couldn't read %s for the index: %s", item.path, exc)
            return None
        document = self.embed(extraction, item.path.name)
        with transaction(self.conn):
            file_id = repository.upsert_file(
                self.conn, item.path, sha256=digest, size=item.size, mtime=item.mtime
            )
            repository.set_file_status(self.conn, file_id, "sorted")
            store_content(self.conn, file_id, extraction)
            store_vector(
                self.conn,
                file_id,
                document,
                model=self.model,
                size=item.size,
                mtime=item.mtime,
                source=source,
            )
        return IndexedFile(item.path, item.folder, item.size, item.mtime, source, document.vector)


def _folder_key(path: Path, root: Path) -> str | None:
    """Key of the category folder a file is in ("A/B"), or None outside category folders."""
    if zone_of(path, root) is not Zone.CATEGORY:
        return None
    return "/".join(path.relative_to(root).parts[:-1])


def _self_and_parents(key: str) -> list[str]:
    """ "A/B/C" -> ["A/B/C", "A/B", "A"]: a file also describes the folders above it."""
    parts = key.split("/")
    return ["/".join(parts[:size]) for size in range(len(parts), 0, -1)]


def _hash_or_none(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None
