"""Updates from the GitHub releases of AutoShelf's repository.

``latest_release`` asks the GitHub API for the newest published release (drafts and
pre-releases are never "latest") and picks its installer, ``AutoShelf-X.Y.Z-setup.exe``, and
the checksum next to it, ``AutoShelf-X.Y.Z-setup.exe.sha256``. Only download links of this
repository's releases, over HTTPS, are accepted.

Nothing is downloaded until the user clicks Update. ``download`` then saves the installer in
AutoShelf's cache, checks its SHA-256 against the published one (this catches a broken or
cut-off download; it can't protect against a compromised GitHub account, as both files come
from the same release) and ``start_installer`` runs it silently with ``UPDATE_FLAG``, which
makes the installer start AutoShelf again when it is done.

The only thing sent is the HTTP request itself: nothing about the user's files.
"""

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from autoshelf import __version__

log = logging.getLogger(__name__)

REPOSITORY = "HarukiU06/Folder-sorting-system"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
CHECK_HOURS = 24  # how often a running AutoShelf looks for a newer release
TIMEOUT = 15.0  # seconds, for each request
MAX_PAGE = 1_000_000  # bytes: the API's answer and the checksum file are small
CHUNK = 256 * 1024
UPDATE_FLAG = "/AUTOSHELFUPDATE"  # the installer starts AutoShelf again when it is done
INSTALL_ARGS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", UPDATE_FLAG)
INSTALLER, PORTABLE, SOURCE = "installer", "portable", "source"

_VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_OLD_DOWNLOAD = re.compile(r"AutoShelf-.+-setup\.exe(\.part)?", re.IGNORECASE)

Fetch = Callable[[str], bytes]


class UpdateError(Exception):
    """Checking for, downloading or verifying an update failed."""


@dataclass(frozen=True)
class Release:
    """A published release that can be installed."""

    version: str  # "0.3.0"
    notes: str  # the release's description, as plain text
    page: str  # the release's web page
    installer_url: str
    installer_name: str
    size: int  # bytes
    checksum_url: str


def parse_version(text: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) of "v0.3.0" or "0.3.0"; None for anything else."""
    match = _VERSION.fullmatch(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(version: str, installed: str = __version__) -> bool:
    """True if ``version`` is a later version than ``installed``."""
    new, old = parse_version(version), parse_version(installed)
    return new is not None and old is not None and new > old


def release_from(data: dict) -> Release | None:
    """The installable release described by the GitHub API's ``data``, or None (a draft, a
    pre-release, a tag that isn't a version, or no installer and checksum from this
    repository)."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    numbers = parse_version(str(data.get("tag_name", "")))
    if numbers is None:
        return None
    version = ".".join(str(number) for number in numbers)
    name = f"AutoShelf-{version}-setup.exe"
    assets = {
        asset.get("name"): asset for asset in data.get("assets") or [] if isinstance(asset, dict)
    }
    installer, checksum = assets.get(name), assets.get(f"{name}.sha256")
    if installer is None or checksum is None:
        return None
    urls = [str(item.get("browser_download_url", "")) for item in (installer, checksum)]
    if not all(url.startswith(DOWNLOAD_PREFIX) for url in urls):
        return None
    return Release(
        version=version,
        notes=str(data.get("body") or ""),
        page=str(data.get("html_url") or RELEASES_PAGE),
        installer_url=urls[0],
        installer_name=name,
        size=int(installer.get("size") or 0),
        checksum_url=urls[1],
    )


def latest_release(fetch: Fetch | None = None) -> Release | None:
    """The newest published release that can be installed (newer or not), or None.
    Raises UpdateError when GitHub can't be reached or answers nonsense."""
    try:
        data = json.loads((fetch or http_get)(LATEST_URL).decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise UpdateError(f"Couldn't check for updates: {_reason(exc)}") from exc
    return release_from(data)


def http_get(url: str, *, limit: int = MAX_PAGE) -> bytes:
    """GET ``url`` over HTTPS (at most ``limit`` bytes)."""
    data = bytearray()
    with _urlopen(_request(url), TIMEOUT) as response:
        _check_https(response.geturl())
        while chunk := response.read(limit + 1 - len(data)):
            data += chunk
            if len(data) > limit:
                raise UpdateError(f"{url} sent more than expected")
    return bytes(data)


def expected_digest(raw: bytes) -> str:
    """The SHA-256 in a ``.sha256`` file ("<hex>  <name>", as release.yml writes it; a BOM,
    upper case, a "*" before the name or a line break are fine)."""
    text = raw.decode("utf-8-sig", errors="replace").strip()
    digest = text.split()[0].lower() if text else ""
    if not _DIGEST.fullmatch(digest):
        raise UpdateError("The release's checksum file isn't readable")
    return digest


def download(
    release: Release,
    folder: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> Path:
    """Download the release's installer into ``folder`` and check it against the published
    checksum; returns its path. Raises UpdateError (then no installer is left behind)."""
    folder.mkdir(parents=True, exist_ok=True)
    _remove_old_downloads(folder)
    target = folder / release.installer_name
    part = folder / f"{release.installer_name}.part"
    try:
        expected = expected_digest(http_get(release.checksum_url, limit=4096))
        digest = hashlib.sha256()
        with (
            _urlopen(_request(release.installer_url), TIMEOUT) as response,
            open(part, "wb") as out,
        ):
            _check_https(response.geturl())
            total = int(response.headers.get("Content-Length") or release.size or 0)
            done = 0
            while chunk := response.read(CHUNK):
                if cancelled():
                    raise UpdateError("The download was cancelled")
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        if digest.hexdigest() != expected:
            raise UpdateError(
                "The download is damaged (its checksum doesn't match), so it wasn't installed. "
                "Try again."
            )
        os.replace(part, target)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"Couldn't download the update: {_reason(exc)}") from exc
    log.info("Downloaded and verified %s", target)
    return target


def install_mode(executable: Path | None) -> str:
    """How this copy of AutoShelf can be updated: SOURCE (``executable`` None: run from
    source, update with git), INSTALLER (installed by setup.exe: its uninstaller is next
    to it) or PORTABLE (an unzipped copy: download the new one by hand)."""
    if executable is None:
        return SOURCE
    return INSTALLER if any(executable.parent.glob("unins*.exe")) else PORTABLE


def current_install_mode() -> str:
    """``install_mode`` of the running AutoShelf."""
    from autoshelf import startup

    return install_mode(startup.app_executable())


def start_installer(path: Path, *, popen=subprocess.Popen) -> None:
    """Run the installer silently and on its own (it outlives AutoShelf, which quits)."""
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    popen([str(path), *INSTALL_ARGS], close_fds=True, creationflags=flags)
    log.info("Started the installer %s", path)


def _remove_old_downloads(folder: Path) -> None:
    """AutoShelf's own earlier installers (about 1 GB each) and cut-off downloads."""
    for item in folder.iterdir():
        if item.is_file() and _OLD_DOWNLOAD.fullmatch(item.name):
            try:
                item.unlink()
            except OSError as exc:
                log.warning("Couldn't remove the old download %s: %s", item, exc)


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "User-Agent": f"AutoShelf/{__version__}",
            "Accept": "application/vnd.github+json, application/octet-stream",
        },
    )


def _check_https(url: str) -> None:
    if not url.startswith("https://"):
        raise UpdateError(f"Refusing to download over plain HTTP: {url}")


def _reason(exc: Exception) -> str:
    return str(getattr(exc, "reason", None) or exc)


def _urlopen(request: urllib.request.Request, timeout: float):
    return urllib.request.urlopen(request, timeout=timeout)
