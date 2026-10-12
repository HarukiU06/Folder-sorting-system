"""Where a path sits inside the library root, and which locations get processed.

Layout (SPEC section 3):

- files directly in the root are *drops* and get sorted;
- files directly in ``_Inbox/`` are waiting for the user and get processed;
- files inside user-made category folders are never moved (they are training examples);
- anything inside another ``_``-prefixed folder (e.g. ``_Duplicates``) is reserved and ignored.
"""

from enum import Enum
from pathlib import Path

INBOX_DIRNAME = "_Inbox"
RESERVED_PREFIX = "_"


class Zone(Enum):
    """Where a path is relative to the root."""

    DROP = "drop"
    INBOX = "inbox"
    CATEGORY = "category"
    RESERVED = "reserved"
    OUTSIDE = "outside"


PROCESSED_ZONES = frozenset({Zone.DROP, Zone.INBOX})


def inbox_dir(root: Path) -> Path:
    """Return the path of the ``_Inbox`` folder (it may not exist yet)."""
    return root / INBOX_DIRNAME


def zone_of(path: Path, root: Path) -> Zone:
    """Classify an absolute file path relative to an absolute root, without touching disk."""
    if not path.is_relative_to(root) or path == root:
        return Zone.OUTSIDE
    parts = path.relative_to(root).parts
    folders = parts[:-1]
    if not folders:
        return Zone.DROP
    if len(folders) == 1 and folders[0].casefold() == INBOX_DIRNAME.casefold():
        return Zone.INBOX
    if any(folder.startswith(RESERVED_PREFIX) for folder in folders):
        return Zone.RESERVED
    return Zone.CATEGORY
