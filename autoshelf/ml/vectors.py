"""Turn an extracted file into one embedding vector, and store vectors as bytes."""

import numpy as np

from autoshelf.extract import Extraction
from autoshelf.ml.embedder import Embedder

# Share of the file's vector that comes from its name, title and metadata. The rest comes
# from the body text, so a long text can't drown out an informative filename.
HEADER_WEIGHT = 0.4
# At most this many body chunks are embedded per file (the text is already capped).
MAX_CHUNKS = 48


def header_text(extraction: Extraction, name: str) -> str:
    """The short part of a file: its name words, title and text metadata."""
    parts = [
        " ".join(extraction.filename_tokens) or name,
        extraction.title or "",
        *extraction.metadata.values(),
    ]
    return "\n".join(part for part in parts if part)


def document_vector(embedder: Embedder, extraction: Extraction, name: str) -> np.ndarray:
    """Embed a file as a weighted mean of its header and its body text chunks."""
    header = header_text(extraction, name)
    chunks = embedder.chunk(extraction.text)[:MAX_CHUNKS]
    vectors = embedder.embed([header, *chunks])
    if not chunks:
        return normalize(vectors[0])
    body = vectors[1:].mean(axis=0)
    return normalize(HEADER_WEIGHT * vectors[0] + (1 - HEADER_WEIGHT) * normalize(body))


def normalize(vector: np.ndarray) -> np.ndarray:
    """Scale a vector to unit length (a zero vector stays zero)."""
    vector = np.asarray(vector, dtype=np.float32)
    length = float(np.linalg.norm(vector))
    return vector / length if length > 0 else vector


def to_blob(vector: np.ndarray) -> bytes:
    """Serialize a vector for the database."""
    return np.asarray(vector, dtype="<f4").tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Deserialize a vector stored with ``to_blob``."""
    return np.frombuffer(blob, dtype="<f4").astype(np.float32)
