"""Compress and convert files (Phase 13): always a new file next to the original.

AutoShelf makes the new file in its own working folder (in its data folder, never in the
library), and ``actions.create`` then puts it next to the original, under a free name
(`` (2)`` and so on, never overwriting), recorded in history as a 'create' that undo moves
to ``_Trash``. The original is never changed.

- Compress: one PDF becomes a smaller PDF (unused objects dropped, streams compressed,
  large images resampled); one image becomes a smaller image (at most ``MAX_SIDE`` pixels
  wide or high, recompressed); anything else, or several files, becomes a ZIP.
- Convert: images between PNG, JPG, WEBP, BMP and TIFF (and from HEIC when the optional
  ``pillow-heif`` is installed), images to one PDF, PDF to Word (.docx, with pdf2docx:
  the layout is only approximated), and Word, PowerPoint or Excel files to PDF, but only
  when Microsoft Office or LibreOffice is installed to do it.
"""

import logging
import os
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

log = logging.getLogger(__name__)

IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP", "bmp": "BMP", "tiff": "TIFF"}
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})
HEIC_SUFFIXES = frozenset({".heic", ".heif"})
OFFICE_KINDS = {
    ".docx": "word",
    ".docm": "word",
    ".doc": "word",
    ".rtf": "word",
    ".pptx": "powerpoint",
    ".ppt": "powerpoint",
    ".xlsx": "excel",
    ".xlsm": "excel",
    ".xls": "excel",
}
MAX_SIDE = 2560  # a shrunk image is at most this many pixels wide or high
JPEG_QUALITY = 75
PDF_IMAGE_DPI = 150  # images in a shrunk PDF are resampled to this
OFFICE_TIMEOUT_SECONDS = 180

# Operations, as stored with the 'create' history row (wording.created puts them in words).
ZIP = "zip"
SHRINK_PDF = "shrink_pdf"
SHRINK_IMAGE = "shrink_image"
CONVERT = "convert"
IMAGES_TO_PDF = "images_to_pdf"


class ConvertError(Exception):
    """A file can't be compressed or converted as asked. Nothing was made."""


@dataclass(frozen=True)
class Job:
    """What to make: the operation, its sources, the new file's name and (to convert) the
    format it gets."""

    operation: str
    sources: tuple[Path, ...]
    name: str
    to: str | None = None


# What the formats are called in menus.
FORMAT_LABELS = {
    "pdf": "PDF",
    "docx": "Word document (.docx; the layout is only approximated)",
    "png": "PNG image",
    "jpg": "JPG image",
    "webp": "WEBP image",
    "bmp": "BMP image",
    "tiff": "TIFF image",
}


def label(to: str) -> str:
    """ "PDF", "JPG image"... for a format."""
    return FORMAT_LABELS.get(to, to.upper())


# --- what can be done with a file ----------------------------------------------------------------


def heic_supported() -> bool:
    """True if HEIC photos can be read (the optional pillow-heif package is installed)."""
    try:
        from pillow_heif import register_heif_opener
    except ImportError:
        return False
    register_heif_opener()
    return True


def is_image(path: Path) -> bool:
    """True for an image AutoShelf can convert or shrink."""
    suffix = path.suffix.casefold()
    return suffix in IMAGE_SUFFIXES or (suffix in HEIC_SUFFIXES and heic_supported())


def pdf_to_docx_available() -> bool:
    """True if pdf2docx is installed."""
    try:
        import pdf2docx  # noqa: F401
    except ImportError:
        return False
    return True


def image_format(path: Path) -> str:
    """ "jpg", "png"... for an image file."""
    suffix = path.suffix.casefold().lstrip(".")
    return {"jpeg": "jpg", "tif": "tiff"}.get(suffix, suffix)


def shrink_format(path: Path) -> str:
    """The format of a smaller copy of an image: its own, or JPG (for a HEIC photo)."""
    own = image_format(path)
    return own if own in IMAGE_FORMATS else "jpg"


def targets(path: Path) -> list[str]:
    """The formats a file can be converted to here (empty: none)."""
    suffix = path.suffix.casefold()
    if is_image(path):
        own = image_format(path)
        return [name for name in IMAGE_FORMATS if name != own] + ["pdf"]
    if suffix == ".pdf":
        return ["docx"] if pdf_to_docx_available() else []
    kind = OFFICE_KINDS.get(suffix)
    if kind is not None and office_converter(kind) is not None:
        return ["pdf"]
    return []


def compress_operation(paths: Sequence[Path]) -> str:
    """How files are compressed: one PDF or image is shrunk, anything else is zipped."""
    if len(paths) == 1:
        if paths[0].suffix.casefold() == ".pdf":
            return SHRINK_PDF
        if is_image(paths[0]):
            return SHRINK_IMAGE
    return ZIP


# --- planning --------------------------------------------------------------------------------


def _several(paths: Sequence[Path], suffix: str) -> str:
    stem = paths[0].stem
    return f"{stem}{suffix}" if len(paths) == 1 else f"{stem} and {len(paths) - 1} more{suffix}"


def plan_compress(paths: Sequence[Path], *, zip_always: bool = False) -> Job:
    """The job that compresses ``paths`` (always a ZIP with ``zip_always``)."""
    paths = tuple(paths)
    if not paths:
        raise ConvertError("Choose at least one file to compress")
    operation = ZIP if zip_always else compress_operation(paths)
    if operation == ZIP:
        return Job(ZIP, paths, _several(paths, ".zip"))
    path = paths[0]
    suffix = path.suffix
    if operation == SHRINK_IMAGE and shrink_format(path) != image_format(path):
        suffix = f".{shrink_format(path)}"
    return Job(operation, paths, f"{path.stem} (smaller){suffix}")


def plan_convert(paths: Sequence[Path], to: str) -> list[Job]:
    """The jobs that convert ``paths`` to ``to``: one per file, except that several images
    to PDF make one PDF. Raises ConvertError for a conversion that isn't offered."""
    paths = tuple(paths)
    to = to.casefold().lstrip(".")
    to = {"jpeg": "jpg", "tif": "tiff"}.get(to, to)
    if not paths:
        raise ConvertError("Choose at least one file to convert")
    for path in paths:
        if to not in targets(path):
            offered = ", ".join(targets(path)) or "nothing"
            raise ConvertError(
                f"{path.name} can't be converted to {to.upper()} here (it can be: {offered})"
            )
    if to == "pdf" and len(paths) > 1 and all(is_image(path) for path in paths):
        return [Job(IMAGES_TO_PDF, paths, _several(paths, ".pdf"), "pdf")]
    jobs = []
    for path in paths:
        operation = IMAGES_TO_PDF if to == "pdf" and is_image(path) else CONVERT
        jobs.append(Job(operation, (path,), f"{path.stem}.{to}", to))
    return jobs


# --- making the new file ---------------------------------------------------------------------


def run(job: Job, workdir: Path) -> Path:
    """Make the new file of ``job`` in ``workdir`` (AutoShelf's own folder) and return it.

    Reads the sources only. Raises ConvertError.
    """
    for source in job.sources:
        if not source.is_file():
            raise ConvertError(f"{source.name} is no longer there")
    out = workdir / job.name
    try:
        if job.operation == ZIP:
            _zip(job.sources, out)
        elif job.operation == SHRINK_PDF:
            _shrink_pdf(job.sources[0], out)
        elif job.operation == SHRINK_IMAGE:
            _shrink_image(job.sources[0], out)
        elif job.operation == IMAGES_TO_PDF:
            _images_to_pdf(job.sources, out)
        elif job.operation == CONVERT and job.to in IMAGE_FORMATS:
            _convert_image(job.sources[0], out, job.to)
        elif job.operation == CONVERT and job.to == "docx":
            _pdf_to_docx(job.sources[0], out)
        elif job.operation == CONVERT and job.to == "pdf":
            _office_to_pdf(job.sources[0], out)
        else:
            raise ConvertError(f"Unknown job {job.operation!r}")
    except ConvertError:
        raise
    except Exception as exc:
        log.debug("Couldn't make %s", job.name, exc_info=True)
        raise ConvertError(f"Couldn't make {job.name}: {exc}") from exc
    if not out.is_file():
        raise ConvertError(f"Couldn't make {job.name}")
    if job.operation in (SHRINK_PDF, SHRINK_IMAGE):
        source = job.sources[0]
        if out.stat().st_size >= source.stat().st_size:
            raise ConvertError(f"{source.name} is already as small as AutoShelf can make it")
    return out


def _zip(sources: Sequence[Path], out: Path) -> None:
    used: set[str] = set()
    with zipfile.ZipFile(out, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sources:
            name, number = source.name, 2
            while name.casefold() in used:  # two files with one name: keep both
                name = f"{source.stem} ({number}){source.suffix}"
                number += 1
            used.add(name.casefold())
            archive.write(source, arcname=name)


def _shrink_pdf(source: Path, out: Path) -> None:
    import pymupdf

    with pymupdf.open(source) as doc:
        if doc.needs_pass:
            raise ConvertError(f"{source.name} is password-protected")
        if hasattr(doc, "rewrite_images"):  # resample big images (scans)
            doc.rewrite_images(
                dpi_threshold=PDF_IMAGE_DPI + 50, dpi_target=PDF_IMAGE_DPI, quality=JPEG_QUALITY
            )
        doc.save(out, garbage=4, deflate=True, clean=True)


def _open_image(source: Path):
    from PIL import Image, ImageOps

    if source.suffix.casefold() in HEIC_SUFFIXES and not heic_supported():
        raise ConvertError("Reading HEIC photos needs the optional pillow-heif package")
    image = Image.open(source)
    return ImageOps.exif_transpose(image)


def _rgb(image):
    """``image`` without transparency (on white), for formats that have none."""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        background = Image.new("RGB", image.size, "white")
        background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
        return background
    return image.convert("RGB")


def _save_image(image, out: Path, kind: str, *, smaller: bool = False) -> None:
    fmt = IMAGE_FORMATS[kind]
    options: dict = {}
    if fmt in ("JPEG", "BMP"):
        image = _rgb(image)
    if fmt == "JPEG":
        options = {"quality": JPEG_QUALITY if smaller else 92, "optimize": True}
    elif fmt == "WEBP":
        options = {"quality": JPEG_QUALITY if smaller else 90}
    elif fmt == "PNG":
        options = {"optimize": True}
    elif fmt == "TIFF":
        options = {"compression": "tiff_deflate"}
    with open(out, "xb") as handle:
        image.save(handle, format=fmt, **options)


def _shrink_image(source: Path, out: Path) -> None:
    image = _open_image(source)
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    _save_image(image, out, shrink_format(source), smaller=True)


def _convert_image(source: Path, out: Path, to: str) -> None:
    _save_image(_open_image(source), out, to)


def _images_to_pdf(sources: Sequence[Path], out: Path) -> None:
    pages = [_rgb(_open_image(source)) for source in sources]
    with open(out, "xb") as handle:
        pages[0].save(handle, format="PDF", save_all=True, append_images=pages[1:], resolution=150)


def _pdf_to_docx(source: Path, out: Path) -> None:
    import pymupdf

    with pymupdf.open(source) as doc:
        if doc.needs_pass:
            raise ConvertError(f"{source.name} is password-protected")
    from pdf2docx import Converter

    logging.getLogger("pdf2docx").setLevel(logging.ERROR)  # it reports every page
    converter = Converter(str(source))
    try:
        converter.convert(str(out))
    finally:
        converter.close()


# --- Word, PowerPoint and Excel files to PDF (with Office or LibreOffice) ------------------------

_COM_PROGIDS = {
    "word": "Word.Application",
    "powerpoint": "PowerPoint.Application",
    "excel": "Excel.Application",
}
# Each opens the file read-only, saves a PDF to $env:AUTOSHELF_OUT and quits. The paths come
# in environment variables, so no file name is ever parsed as PowerShell code.
_COM_SCRIPTS = {
    "word": (
        "$app = New-Object -ComObject Word.Application; $app.Visible = $false; "
        "$app.DisplayAlerts = 0; try { "
        "$doc = $app.Documents.Open($env:AUTOSHELF_IN, $false, $true, $false); "
        "$doc.ExportAsFixedFormat($env:AUTOSHELF_OUT, 17); $doc.Close(0) "
        "} finally { $app.Quit() }"
    ),
    "powerpoint": (
        "$app = New-Object -ComObject PowerPoint.Application; try { "
        "$deck = $app.Presentations.Open($env:AUTOSHELF_IN, -1, 0, 0); "
        "$deck.SaveAs($env:AUTOSHELF_OUT, 32); $deck.Close() "
        "} finally { $app.Quit() }"
    ),
    "excel": (
        "$app = New-Object -ComObject Excel.Application; $app.Visible = $false; "
        "$app.DisplayAlerts = $false; try { "
        "$book = $app.Workbooks.Open($env:AUTOSHELF_IN, 0, $true); "
        "$book.ExportAsFixedFormat(0, $env:AUTOSHELF_OUT); $book.Close($false) "
        "} finally { $app.Quit() }"
    ),
}
_LIBREOFFICE_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
)


def _com_available(progid: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + r"\CLSID"):
            return True
    except OSError:
        return False


def _libreoffice() -> str | None:
    found = shutil.which("soffice")
    if found:
        return found
    return next((path for path in _LIBREOFFICE_PATHS if os.path.isfile(path)), None)


@cache
def office_converter(kind: str) -> str | None:
    """ "msoffice" or "libreoffice" if a Word/PowerPoint/Excel file of ``kind`` can be
    turned into a PDF here, else None (checked once per run)."""
    if _com_available(_COM_PROGIDS[kind]):
        return "msoffice"
    if _libreoffice() is not None:
        return "libreoffice"
    return None


def _office_to_pdf(source: Path, out: Path) -> None:
    kind = OFFICE_KINDS.get(source.suffix.casefold())
    converter = office_converter(kind) if kind else None
    if converter is None:
        raise ConvertError("Converting to PDF needs Microsoft Office or LibreOffice")
    if converter == "msoffice":
        environment = {**os.environ, "AUTOSHELF_IN": str(source), "AUTOSHELF_OUT": str(out)}
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _COM_SCRIPTS[kind],
        ]
    else:
        environment = dict(os.environ)
        command = [
            _libreoffice(),
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(out.parent),
            str(source),
        ]
    done = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        timeout=OFFICE_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if converter == "libreoffice":
        made = out.parent / f"{source.stem}.pdf"
        if made.is_file() and made != out:
            made.rename(out)  # within AutoShelf's own working folder
    if done.returncode != 0 or not out.is_file():
        detail = (done.stderr or done.stdout).strip().splitlines()[-1:] or ["no output"]
        raise ConvertError(f"Office couldn't convert {source.name}: {detail[0]}")
