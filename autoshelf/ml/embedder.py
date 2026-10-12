"""Text embeddings from a small local sentence-transformers model, loaded once per process.

Nothing leaves the computer: the model is downloaded once into AutoShelf's own cache
folder (``%APPDATA%\\AutoShelf\\cache\\models``) and runs on the CPU.
"""

import logging
import os
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

# Some models are trained to expect an instruction prefix. e5 models use "query: " for
# symmetric similarity, which is what comparing a file with a folder or another file is.
_PREFIXES = {
    "intfloat/multilingual-e5-small": "query: ",
    "intfloat/multilingual-e5-base": "query: ",
    "intfloat/multilingual-e5-large": "query: ",
}

# Long text is embedded in windows of at most this many tokens (less if the model's input
# is shorter); the file's vector is their mean.
CHUNK_TOKENS = 256
_RESERVED_TOKENS = 8  # room for the prefix and the special tokens the model adds itself
FALLBACK_CHUNK_CHARS = 500


class EmbedderError(Exception):
    """The embedding model couldn't be loaded."""


class Embedder(Protocol):
    """Turns texts into L2-normalised float32 vectors, one row per text."""

    model_id: str

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return an array of shape ``(len(texts), dim)``."""
        ...

    def chunk(self, text: str) -> list[str]:
        """Split long text into pieces short enough for the model to read whole."""
        ...


def split_chars(text: str, size: int) -> list[str]:
    """Split text into pieces of at most ``size`` characters, preferring line breaks."""
    pieces: list[str] = []
    while text:
        if len(text) <= size:
            pieces.append(text)
            break
        cut = text.rfind("\n", size // 2, size)
        if cut == -1:
            cut = text.rfind(" ", size // 2, size)
        if cut == -1:
            cut = size
        pieces.append(text[:cut])
        text = text[cut:].lstrip()
    return [piece.strip() for piece in pieces if piece.strip()]


class SentenceTransformerEmbedder:
    """An ``Embedder`` backed by a sentence-transformers model. Use ``load_embedder``."""

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self.model_id = model_name
        self._prefix = _PREFIXES.get(model_name, "")
        self._model = _load_model(model_name, cache_dir)
        self._tokenizer = getattr(self._model, "tokenizer", None)
        limit = int(getattr(self._model, "max_seq_length", None) or CHUNK_TOKENS)
        self.chunk_tokens = max(16, min(CHUNK_TOKENS, limit - _RESERVED_TOKENS))
        self._lock = threading.Lock()

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Return L2-normalised float32 embeddings of ``texts``."""
        with self._lock:
            vectors = self._model.encode(
                list(texts),
                prompt=self._prefix or None,
                batch_size=16,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        return np.asarray(vectors, dtype=np.float32)

    def chunk(self, text: str) -> list[str]:
        """Split text into windows that fit this model's input length, in its own tokens."""
        if not text.strip():
            return []
        try:
            # verbose=False: the whole text is longer than the model's input on purpose.
            encoding = self._tokenizer(
                text, add_special_tokens=False, return_offsets_mapping=True, verbose=False
            )
            offsets = encoding["offset_mapping"]
        except Exception:
            return split_chars(text, FALLBACK_CHUNK_CHARS)
        pieces = []
        for start in range(0, len(offsets), self.chunk_tokens):
            window = offsets[start : start + self.chunk_tokens]
            pieces.append(text[window[0][0] : window[-1][1]].strip())
        return [piece for piece in pieces if piece]


def _load_model(model_name: str, cache_dir: Path):
    # Without Windows Developer Mode the model cache uses copies instead of symlinks: fine.
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from sentence_transformers import SentenceTransformer
        from transformers.utils import logging as transformers_logging
    except ImportError as exc:
        raise EmbedderError(f"sentence-transformers is not installed ({exc})") from exc
    transformers_logging.disable_progress_bar()  # the per-load "Loading weights" bar
    # Silence deprecation notices that sentence-transformers raises about its own calls.
    logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    options = {"device": "cpu", "cache_folder": str(cache_dir)}
    try:
        # Use the downloaded copy without contacting the internet.
        return SentenceTransformer(model_name, local_files_only=True, **options)
    except Exception:
        log.info(
            "Downloading the language model %s into %s (first run only; a few hundred MB)...",
            model_name,
            cache_dir,
        )
    try:
        return SentenceTransformer(model_name, **options)
    except Exception as exc:
        raise EmbedderError(f"couldn't load the language model {model_name!r}: {exc}") from exc


def is_downloaded(model_name: str, cache_dir: Path) -> bool:
    """True if the model is already in the cache (so loading it needs no download)."""
    snapshots = cache_dir / ("models--" + model_name.replace("/", "--")) / "snapshots"
    return snapshots.is_dir() and any(snapshots.iterdir())


_loaded: dict[tuple[str, Path], SentenceTransformerEmbedder] = {}
_loaded_lock = threading.Lock()


def load_embedder(model_name: str, cache_dir: Path) -> SentenceTransformerEmbedder:
    """Return the shared embedder for ``model_name``, loading (and downloading) it once."""
    key = (model_name, cache_dir)
    with _loaded_lock:
        if key not in _loaded:
            log.info("Loading the language model %s...", model_name)
            _loaded[key] = SentenceTransformerEmbedder(model_name, cache_dir)
        return _loaded[key]
