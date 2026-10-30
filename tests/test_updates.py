"""Updates from GitHub releases: finding, downloading, verifying and starting the installer."""

import hashlib
import json
import urllib.error
from pathlib import Path

import pytest
from fakes import GITHUB, FakeWeb, github_release

from autoshelf import updates
from autoshelf.updates import Release, UpdateError

INSTALLER = b"MZ pretend this is an installer"


def release(version: str = "0.3.0") -> Release:
    found = updates.release_from(github_release(version))
    assert found is not None
    return found


def web_for(found: Release, body: bytes = INSTALLER, digest: str | None = None) -> FakeWeb:
    digest = digest or hashlib.sha256(body).hexdigest()
    checksum = f"{digest}  {found.installer_name}".encode("ascii")
    return FakeWeb({found.installer_url: body, found.checksum_url: checksum})


def test_parse_and_compare_versions() -> None:
    assert updates.parse_version("v0.10.0") == (0, 10, 0)
    assert updates.parse_version("0.2.0") == (0, 2, 0)
    for bad in ("v1.0", "latest", "", "v1.2.3-beta", "1.2.3.4"):
        assert updates.parse_version(bad) is None
    assert updates.is_newer("0.10.0", "0.9.9")
    assert not updates.is_newer("0.2.0", "0.2.0")
    assert not updates.is_newer("0.1.0", "0.2.0")
    assert not updates.is_newer("nightly", "0.2.0")


def test_release_from_picks_the_installer_and_checksum() -> None:
    found = updates.release_from(github_release("0.3.0"))

    base = f"{GITHUB}/releases/download/v0.3.0/"
    assert found == Release(
        version="0.3.0",
        notes="What's new in 0.3.0",
        page=f"{GITHUB}/releases/tag/v0.3.0",
        installer_url=base + "AutoShelf-0.3.0-setup.exe",
        installer_name="AutoShelf-0.3.0-setup.exe",
        size=1000,
        checksum_url=base + "AutoShelf-0.3.0-setup.exe.sha256",
    )


def test_release_from_refuses_foreign_urls_and_prereleases() -> None:
    elsewhere = github_release("0.3.0")
    elsewhere["assets"][1]["browser_download_url"] = (
        "https://example.com/HarukiU06/Folder-sorting-system/releases/download/v0.3.0/"
        "AutoShelf-0.3.0-setup.exe"
    )
    other_repo = github_release("0.3.0")
    other_repo["assets"][2]["browser_download_url"] = (
        "https://github.com/someone/fork/releases/download/v0.3.0/AutoShelf-0.3.0-setup.exe.sha256"
    )
    plain_http = github_release("0.3.0")
    plain_http["assets"][1]["browser_download_url"] = plain_http["assets"][1][
        "browser_download_url"
    ].replace("https://", "http://")
    for data in (
        elsewhere,
        other_repo,
        plain_http,
        github_release("0.3.0", prerelease=True),
        github_release("0.3.0", draft=True),
        github_release("0.3.0", tag_name="nightly"),
        {"message": "API rate limit exceeded"},
    ):
        assert updates.release_from(data) is None


def test_release_without_checksum_offers_nothing() -> None:
    data = github_release("0.3.0")
    data["assets"] = [asset for asset in data["assets"] if not asset["name"].endswith(".sha256")]
    assert updates.release_from(data) is None


def test_latest_release_uses_the_github_api() -> None:
    asked = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return json.dumps(github_release("0.3.0")).encode()

    assert updates.latest_release(fetch) == release("0.3.0")
    assert asked == [updates.LATEST_URL]
    assert updates.LATEST_URL == (
        "https://api.github.com/repos/HarukiU06/Folder-sorting-system/releases/latest"
    )


def test_latest_release_wraps_errors() -> None:
    def offline(url: str) -> bytes:
        raise urllib.error.URLError("no network")

    with pytest.raises(UpdateError):
        updates.latest_release(offline)
    with pytest.raises(UpdateError):
        updates.latest_release(lambda url: b"not json")


def test_http_get_goes_through_the_guarded_opener(monkeypatch) -> None:
    web = FakeWeb({updates.LATEST_URL: b"{}"})
    monkeypatch.setattr(updates, "_urlopen", web)
    assert updates.http_get(updates.LATEST_URL) == b"{}"
    assert web.asked == [updates.LATEST_URL]


@pytest.mark.parametrize(
    "text",
    [
        "{digest}  AutoShelf-0.3.0-setup.exe",  # what release.yml writes
        "﻿{digest}  AutoShelf-0.3.0-setup.exe\r\n",
        "{DIGEST} *AutoShelf-0.3.0-setup.exe\n",
        "{digest}",
    ],
)
def test_checksum_text_is_read_leniently(text: str) -> None:
    digest = hashlib.sha256(INSTALLER).hexdigest()
    raw = text.format(digest=digest, DIGEST=digest.upper()).encode("utf-8")
    assert updates.expected_digest(raw) == digest


def test_checksum_text_that_isnt_one_is_refused() -> None:
    for raw in (b"", b"not a checksum", b"abc123  file"):
        with pytest.raises(UpdateError):
            updates.expected_digest(raw)


def test_download_verifies_the_checksum(monkeypatch, tmp_path: Path) -> None:
    found = release()
    monkeypatch.setattr(updates, "_urlopen", web_for(found))
    seen: list[tuple[int, int]] = []

    path = updates.download(
        found, tmp_path, progress=lambda done, total: seen.append((done, total))
    )

    assert path == tmp_path / "AutoShelf-0.3.0-setup.exe"
    assert path.read_bytes() == INSTALLER
    assert seen[-1] == (len(INSTALLER), len(INSTALLER)) and len(seen) > 1
    assert sorted(item.name for item in tmp_path.iterdir()) == ["AutoShelf-0.3.0-setup.exe"]


def test_download_mismatch_removes_only_its_own_file(monkeypatch, tmp_path: Path) -> None:
    found = release()
    (tmp_path / "keep.txt").write_text("not AutoShelf's", encoding="utf-8")
    monkeypatch.setattr(updates, "_urlopen", web_for(found, digest="0" * 64))

    with pytest.raises(UpdateError, match="damaged"):
        updates.download(found, tmp_path)

    assert sorted(item.name for item in tmp_path.iterdir()) == ["keep.txt"]


def test_download_refuses_a_redirect_to_plain_http(monkeypatch, tmp_path: Path) -> None:
    found = release()
    web = web_for(found)
    web.redirect[found.installer_url] = "http://objects.example.com/setup.exe"
    monkeypatch.setattr(updates, "_urlopen", web)

    with pytest.raises(UpdateError):
        updates.download(found, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_can_be_cancelled(monkeypatch, tmp_path: Path) -> None:
    found = release()
    monkeypatch.setattr(updates, "_urlopen", web_for(found))
    seen: list[int] = []

    with pytest.raises(UpdateError, match="cancel"):
        updates.download(
            found,
            tmp_path,
            progress=lambda done, total: seen.append(done),
            cancelled=lambda: bool(seen),
        )

    assert list(tmp_path.iterdir()) == []


def test_download_replaces_older_installers(monkeypatch, tmp_path: Path) -> None:
    found = release()
    (tmp_path / "AutoShelf-0.2.0-setup.exe").write_bytes(b"old")
    (tmp_path / "AutoShelf-0.2.5-setup.exe.part").write_bytes(b"cut off")
    (tmp_path / "keep.txt").write_text("not AutoShelf's", encoding="utf-8")
    monkeypatch.setattr(updates, "_urlopen", web_for(found))

    updates.download(found, tmp_path)

    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "AutoShelf-0.3.0-setup.exe",
        "keep.txt",
    ]


def test_install_mode(tmp_path: Path) -> None:
    app = tmp_path / "AutoShelf.exe"
    app.write_bytes(b"")
    assert updates.install_mode(None) == updates.SOURCE
    assert updates.install_mode(app) == updates.PORTABLE
    (tmp_path / "unins001.exe").write_bytes(b"")
    assert updates.install_mode(app) == updates.INSTALLER


def test_start_installer_runs_it_detached_and_silent(tmp_path: Path) -> None:
    started = []

    def popen(args, **options):
        started.append((args, options))

    setup = tmp_path / "AutoShelf-0.3.0-setup.exe"
    updates.start_installer(setup, popen=popen)

    ((args, options),) = started
    assert args == [
        str(setup),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/AUTOSHELFUPDATE",
    ]
    assert options["close_fds"] is True
    assert updates.UPDATE_FLAG == "/AUTOSHELFUPDATE"


def test_tests_never_reach_github() -> None:
    with pytest.raises(AssertionError, match="network"):
        updates.http_get(updates.LATEST_URL)
