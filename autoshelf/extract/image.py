"""Images: embedded text metadata only (EXIF and PNG text), no OCR, via Pillow."""

from typing import BinaryIO

from PIL import Image

from autoshelf.extract.base import Content, non_empty

# EXIF tag ids
_IMAGE_DESCRIPTION = 0x010E
_ARTIST = 0x013B
_XP_TITLE = 0x9C9B
_XP_COMMENT = 0x9C9C
_XP_AUTHOR = 0x9C9D
_XP_KEYWORDS = 0x9C9E
_XP_SUBJECT = 0x9C9F
_EXIF_IFD = 0x8769
_USER_COMMENT = 0x9286


def extract(file: BinaryIO, max_chars: int) -> Content:
    """Read title, description, keywords and comments that cameras or Windows embedded."""
    with Image.open(file) as image:
        exif = image.getexif()
        png_text = dict(getattr(image, "text", {}) or {})
        user_comment = exif.get_ifd(_EXIF_IFD).get(_USER_COMMENT)

    title = _xp(exif.get(_XP_TITLE)) or png_text.get("Title")
    return Content(
        title=title or None,
        metadata=non_empty(
            description=exif.get(_IMAGE_DESCRIPTION) or png_text.get("Description"),
            subject=_xp(exif.get(_XP_SUBJECT)),
            keywords=_xp(exif.get(_XP_KEYWORDS)) or png_text.get("Keywords"),
            comment=_xp(exif.get(_XP_COMMENT))
            or _user_comment(user_comment)
            or png_text.get("Comment"),
            author=_xp(exif.get(_XP_AUTHOR)) or exif.get(_ARTIST) or png_text.get("Author"),
        ),
    )


def _xp(value: object) -> str | None:
    """Decode a Windows "XP" tag, stored as UTF-16-LE bytes (or a tuple of byte values)."""
    if value is None:
        return None
    raw = bytes(value) if isinstance(value, (bytes, tuple, list)) else str(value).encode()
    return raw.decode("utf-16-le", errors="replace").rstrip("\x00") or None


def _user_comment(value: object) -> str | None:
    """Decode an EXIF UserComment, which starts with an 8-byte character-code header."""
    if not isinstance(value, bytes) or len(value) <= 8:
        return value if isinstance(value, str) and value.strip() else None
    header, body = value[:8], value[8:]
    if header.startswith(b"UNICODE"):
        text = body.decode("utf-16-le" if body[:1] != b"\x00" else "utf-16-be", errors="replace")
    else:
        text = body.decode("utf-8", errors="replace")
    return text.rstrip("\x00 ") or None
