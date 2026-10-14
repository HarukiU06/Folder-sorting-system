"""Pick an extractor by file extension and run it safely.

Extractor modules are imported on first use, so commands that never read files stay
fast, and a broken or missing library only affects its own file types.
"""

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from autoshelf.extract import guard
from autoshelf.extract.base import MAX_TEXT_CHARS, Content, clean_inline
from autoshelf.extract.filename import filename_tokens
from autoshelf.extract.plaintext import looks_like_text

log = logging.getLogger(__name__)

FILENAME_ONLY = "filename only"

_TEXT_EXTENSIONS = """
    .txt .text .md .markdown .rst .org .adoc .tex .bib .log .csv .tsv .json .jsonl .yaml .yml
    .toml .ini .cfg .conf .xml .srt .vtt .py .pyw .r .m .jl .js .mjs .ts .tsx .jsx .java .kt
    .scala .c .h .cpp .hpp .cc .cs .go .rs .rb .php .pl .lua .swift .sh .bash .ps1 .bat .cmd
    .sql .css .scss .vue .svelte
""".split()
_AUDIO_VIDEO_EXTENSIONS = """
    .mp3 .m4a .m4b .aac .flac .ogg .oga .opus .wav .wma .aif .aiff .ape .wv .mpc
    .mp4 .m4v .mov .wmv .asf
""".split()
_IMAGE_EXTENSIONS = ".jpg .jpeg .png .tif .tiff .webp .gif .bmp".split()
# Types without an extractor that are never sniffed for plain text.
_KNOWN_BINARY = frozenset(
    """
    .doc .xls .ppt .odt .ods .odp .pages .numbers .key .heic .heif .avi .mkv .webm .7z .rar
    .gz .tgz .bz2 .xz .tar .iso .dmg .exe .msi .dll .bin .dat .db .sqlite
    """.split()
)

# extension -> (extractor name shown to the user, module in autoshelf.extract)
_REGISTRY: dict[str, tuple[str, str]] = {
    ".pdf": ("pdf", "pdf"),
    ".docx": ("word", "word"),
    ".docm": ("word", "word"),
    ".pptx": ("powerpoint", "slides"),
    ".pptm": ("powerpoint", "slides"),
    ".xlsx": ("excel", "spreadsheet"),
    ".xlsm": ("excel", "spreadsheet"),
    ".epub": ("epub", "ebook"),
    ".rtf": ("rtf", "rtf"),
    ".html": ("html", "webpage"),
    ".htm": ("html", "webpage"),
    ".xhtml": ("html", "webpage"),
    ".ipynb": ("notebook", "notebook"),
    ".zip": ("zip listing", "archive"),
    **dict.fromkeys(_TEXT_EXTENSIONS, ("text", "plaintext")),
    **dict.fromkeys(_AUDIO_VIDEO_EXTENSIONS, ("media tags", "media")),
    **dict.fromkeys(_IMAGE_EXTENSIONS, ("image metadata", "image")),
}


@dataclass(frozen=True)
class Extraction:
    """Everything AutoShelf reads from a file: name tokens, text and text metadata."""

    extractor: str
    filename_tokens: tuple[str, ...]
    text: str = ""
    title: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    error: str | None = None

    def classification_text(self) -> str:
        """Filename words, title, metadata and body text as one string for classification."""
        parts = [
            " ".join(self.filename_tokens),
            self.title or "",
            *self.metadata.values(),
            self.text,
        ]
        return "\n".join(part for part in parts if part)

    def summary(self) -> str:
        """One line describing what was read, for logs and the CLI."""
        if self.error:
            return f"couldn't read its text ({self.error}); using the filename"
        if self.extractor == FILENAME_ONLY:
            return "no readable text for this file type; using the filename"
        if not (self.text or self.title or self.metadata):
            return f"no text found ({self.extractor}); using the filename"
        if self.text:
            parts = [f"read {len(self.text):,} chars via {self.extractor}"]
            if self.truncated:
                parts[0] += " (truncated)"
        else:
            parts = [f"read metadata via {self.extractor}"]
        if self.title:
            parts.append(f"title {self.title!r}")
        if self.metadata:
            parts.append(", ".join(sorted(self.metadata)))
        return "; ".join(parts)


def extractor_for(path: Path) -> tuple[str, str | None]:
    """Return (extractor name, module name) for a path; module is None for filename-only."""
    entry = _REGISTRY.get(path.suffix.lower())
    if entry is not None:
        return entry
    if path.suffix.lower() not in _KNOWN_BINARY:
        try:
            if looks_like_text(path):
                return "text", "plaintext"
        except OSError:
            pass
    return FILENAME_ONLY, None


def extract(path: Path, *, max_chars: int = MAX_TEXT_CHARS) -> Extraction:
    """Extract text and metadata from ``path``. Never raises for unreadable content.

    If the file can't be parsed, the result carries ``error`` and only the filename
    tokens, which is still enough to classify it.
    """
    tokens = tuple(filename_tokens(path.name))
    name, module = extractor_for(path)
    if module is None:
        return Extraction(FILENAME_ONLY, tokens)
    if guard.crashed_before(path):
        return Extraction(name, tokens, error=guard.CRASHED)
    try:
        extractor = importlib.import_module(f"autoshelf.extract.{module}").extract
        # The registry owns the open file so its handle is always closed on return.
        with guard.reading(path), open(path, "rb") as file:
            content: Content = extractor(file, max_chars)
    except Exception as exc:
        log.debug("Couldn't extract %s with %s", path, name, exc_info=True)
        return Extraction(name, tokens, error=_describe(exc))
    title = clean_inline(content.title) or None
    metadata = {key: value for key, value in content.metadata.items() if value}
    return Extraction(
        extractor=name,
        filename_tokens=tokens,
        text=content.text[:max_chars],
        title=title,
        metadata=metadata,
        truncated=content.truncated or len(content.text) > max_chars,
    )


def _describe(exc: Exception) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message if len(message) <= 200 else message[:197] + "..."
