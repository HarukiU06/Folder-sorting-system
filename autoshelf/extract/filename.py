"""Turn file and folder names into word tokens for classification."""

import re
import unicodedata
from pathlib import PurePath

_SEPARATORS = re.compile(r"[\W_]+")
# Split camelCase ("deepLearning"), acronyms ("PDFReader") and letter/digit runs ("lecture03").
_INNER_BOUNDARY = re.compile(
    r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])"
)


def name_tokens(name: str) -> list[str]:
    """Split a name (e.g. a folder name) into word tokens.

    >>> name_tokens("MachineLearning_notes v2")
    ['Machine', 'Learning', 'notes', 'v', '2']
    """
    tokens: list[str] = []
    for piece in _SEPARATORS.split(unicodedata.normalize("NFKC", name)):
        tokens.extend(token for token in _INNER_BOUNDARY.split(piece) if token)
    return tokens


def filename_tokens(name: str) -> list[str]:
    """Split a filename (without its extension) into word tokens.

    >>> filename_tokens("UnsupervisedLearning_lecture-03 (2).pdf")
    ['Unsupervised', 'Learning', 'lecture', '03', '2']
    """
    return name_tokens(PurePath(name).stem)
