"""Ignore rules for temporary, partial and hidden files."""

import fnmatch
from collections.abc import Iterable
from pathlib import Path

from autoshelf.fsutil import is_hidden_or_system


def matching_pattern(name: str, patterns: Iterable[str]) -> str | None:
    """Return the first ignore pattern that matches ``name`` (case-insensitive), if any."""
    folded = name.casefold()
    for pattern in patterns:
        if fnmatch.fnmatchcase(folded, pattern.casefold()):
            return pattern
    return None


def ignore_reason(path: Path, patterns: Iterable[str]) -> str | None:
    """Return why ``path`` should be ignored, or None if it should be processed.

    Raises FileNotFoundError if the file vanished before its attributes could be read.
    """
    pattern = matching_pattern(path.name, patterns)
    if pattern is not None:
        return f"matches ignore pattern {pattern!r}"
    if is_hidden_or_system(path):
        return "hidden or system file"
    return None
