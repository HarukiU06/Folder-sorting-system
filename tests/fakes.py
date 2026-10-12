"""A deterministic stand-in for the language model, so tests never load torch or download.

Texts sharing words get similar vectors (hashed bag of words), which is enough to test
the scoring, decisions and caching around the model.
"""

import re
import zlib
from collections.abc import Sequence

import numpy as np

from autoshelf.ml.embedder import split_chars

DIM = 256
_WORDS = re.compile(r"\w+")


class FakeEmbedder:
    """Hashed bag-of-words vectors. ``embedded`` counts every text embedded so far."""

    def __init__(self, model_id: str = "fake-model") -> None:
        self.model_id = model_id
        self.embedded = 0

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        self.embedded += len(texts)
        vectors = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in _WORDS.findall(text.casefold()):
                vectors[row, zlib.crc32(word.encode()) % DIM] += 1.0
            length = np.linalg.norm(vectors[row])
            if length:
                vectors[row] /= length
        return vectors

    def chunk(self, text: str) -> list[str]:
        return split_chars(text, 200)
