from pathlib import Path

import pytest
from helpers import make, set_windows_attributes, windows_only

from autoshelf.config import DEFAULT_IGNORE_PATTERNS
from autoshelf.layout import Zone, zone_of
from autoshelf.watcher.ignore import ignore_reason, matching_pattern

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4


@pytest.mark.parametrize(
    "name",
    [
        "~$Report.docx",
        "download.tmp",
        "FILE.TMP",
        "movie.mp4.crdownload",
        "big.iso.part",
        "paper.pdf.download",
        "desktop.ini",
        "Desktop.INI",
        ".DS_Store",
        "Thumbs.db",
    ],
)
def test_temp_and_partial_files_match(name: str) -> None:
    assert matching_pattern(name, DEFAULT_IGNORE_PATTERNS) is not None


@pytest.mark.parametrize(
    "name",
    [
        "report.pdf",
        "partial derivatives.pdf",
        "tmp notes.txt",
        "my.part.pdf",
        "講義ノート.docx",
        "$money.xlsx",
    ],
)
def test_normal_files_do_not_match(name: str) -> None:
    assert matching_pattern(name, DEFAULT_IGNORE_PATTERNS) is None


def test_ignore_reason_for_normal_file(tmp_path: Path) -> None:
    assert ignore_reason(make(tmp_path / "notes.txt"), DEFAULT_IGNORE_PATTERNS) is None


def test_dotfiles_are_hidden(tmp_path: Path) -> None:
    reason = ignore_reason(make(tmp_path / ".secret"), DEFAULT_IGNORE_PATTERNS)
    assert reason == "hidden or system file"


@windows_only
@pytest.mark.parametrize("attribute", [FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM])
def test_windows_hidden_and_system_files_are_ignored(tmp_path: Path, attribute: int) -> None:
    path = make(tmp_path / "looks normal.pdf")
    set_windows_attributes(path, attribute)
    assert ignore_reason(path, DEFAULT_IGNORE_PATTERNS) == "hidden or system file"


def test_vanished_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ignore_reason(tmp_path / "gone.pdf", DEFAULT_IGNORE_PATTERNS)


@pytest.mark.parametrize(
    ("relative", "zone"),
    [
        ("report.pdf", Zone.DROP),
        ("_notes.txt", Zone.DROP),
        ("_Inbox/report.pdf", Zone.INBOX),
        ("_inbox/report.pdf", Zone.INBOX),
        ("_Inbox/Sub/report.pdf", Zone.RESERVED),
        ("_Duplicates/report.pdf", Zone.RESERVED),
        ("Finance/_old/report.pdf", Zone.RESERVED),
        ("Finance/report.pdf", Zone.CATEGORY),
        ("Machine Learning/Unsupervised/k-means 法.pdf", Zone.CATEGORY),
    ],
)
def test_zone_of(root: Path, relative: str, zone: Zone) -> None:
    assert zone_of(root / relative, root) is zone


def test_zone_outside_root(root: Path, tmp_path: Path) -> None:
    assert zone_of(tmp_path / "elsewhere.pdf", root) is Zone.OUTSIDE
    assert zone_of(root, root) is Zone.OUTSIDE
