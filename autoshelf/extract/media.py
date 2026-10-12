"""Audio and video files: text tags only (no transcription) via mutagen."""

from typing import BinaryIO

import mutagen

from autoshelf.extract.base import Content, clean_inline

_TAGS = ("artist", "albumartist", "album", "composer", "genre", "date", "description", "comment")


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read title, artist, album and similar tags (ID3, MP4, Vorbis, ASF...)."""
    media = mutagen.File(file, easy=True)
    if media is None or not media.tags:
        return Content()
    tags = {key.lower(): _join(value) for key, value in media.tags.items()}
    metadata = {key: tags[key] for key in _TAGS if tags.get(key)}
    return Content(title=tags.get("title") or None, metadata=metadata)


def _join(value: object) -> str:
    values = value if isinstance(value, list) else [value]
    return clean_inline("; ".join(str(item) for item in values))
