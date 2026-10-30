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


class FakeProvider:
    """An LLM stand-in: returns canned replies in turn and records what it was sent."""

    name = "Fake AI"
    model = "fake-llm"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or ["OK"]
        self.calls: list[tuple[str, list]] = []

    def complete(self, system, messages, *, max_tokens: int = 2048) -> str:
        self.calls.append((system, list(messages)))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


class FakeRegistry:
    """HKEY_CURRENT_USER in memory: {key: {value name: value}} (keys compared as written)."""

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, str]] = {}

    def get(self, key: str, name: str = "") -> str | None:
        return self.keys.get(key, {}).get(name)

    def set(self, key: str, name: str, value: str) -> None:
        self.keys.setdefault(key, {})[name] = value

    def delete_tree(self, key: str) -> None:
        prefix = key.casefold() + "\\"
        for existing in list(self.keys):
            if existing.casefold() == key.casefold() or existing.casefold().startswith(prefix):
                del self.keys[existing]


# --- GitHub releases (autoshelf.updates) -------------------------------------------------

GITHUB = "https://github.com/HarukiU06/Folder-sorting-system"


def github_release(version: str = "9.9.9", **changes) -> dict:
    """What the GitHub API says about a release with the app's usual assets."""
    base = f"{GITHUB}/releases/download/v{version}/"
    names = (
        f"AutoShelf-{version}-windows.zip",
        f"AutoShelf-{version}-setup.exe",
        f"AutoShelf-{version}-setup.exe.sha256",
    )
    data = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "body": f"What's new in {version}",
        "html_url": f"{GITHUB}/releases/tag/v{version}",
        "assets": [
            {"name": name, "size": 1000, "browser_download_url": base + name} for name in names
        ],
    }
    data.update(changes)
    return data


class FakeResponse:
    """A urllib response: ``read`` in small chunks, the final URL, a Content-Length."""

    def __init__(self, body: bytes, url: str, chunk: int = 4) -> None:
        self.body, self.url, self.chunk = body, url, chunk
        self.headers = {"Content-Length": str(len(body))}
        self._at = 0

    def read(self, size: int = -1) -> bytes:
        size = len(self.body) if size is None or size < 0 else min(size, self.chunk)
        data = self.body[self._at : self._at + size]
        self._at += len(data)
        return data

    def geturl(self) -> str:
        return self.url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeWeb:
    """Answers ``updates._urlopen`` from a dict of URL -> bytes; records what was asked."""

    def __init__(self, pages: dict[str, bytes], redirect: dict[str, str] | None = None) -> None:
        self.pages = pages
        self.redirect = redirect or {}
        self.asked: list[str] = []

    def __call__(self, request, timeout: float) -> FakeResponse:
        url = request.full_url
        self.asked.append(url)
        if url not in self.pages:
            import urllib.error

            raise urllib.error.URLError(f"no such page {url}")
        return FakeResponse(self.pages[url], self.redirect.get(url, url))
