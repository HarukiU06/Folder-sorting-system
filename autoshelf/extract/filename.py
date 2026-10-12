"""Turn a filename into word tokens for classification."""

import re
import unicodedata
from pathlib import PurePath

_SEPARATORS = re.compile(r"[\W_]+")
# Split camelCase ("deepLearning"), acronyms ("PDFReader") and letter/digit runs ("lecture03").
_INNER_BOUNDARY = re.compile(
    r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
)


def filename_tokens(name: str) -> list[str]:
    """Split a filename (without its extension) into word tokens.

    >>> filename_tokens("UnsupervisedLearning_lecture-03 (2).pdf")
    ['Unsupervised', 'Learning', 'lecture', '03', '2']
    """
    stem = unicodedata.normalize("NFKC", PurePath(name).stem)
    tokens: list[str] = []
    for piece in _SEPARATORS.split(stem):
        tokens.extend(token for token in _INNER_BOUNDARY.split(piece) if token)
    return tokens
