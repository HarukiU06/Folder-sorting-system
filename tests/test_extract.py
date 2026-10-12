import os
from pathlib import Path

import pytest
import samples
from helpers import make, windows_only

from autoshelf.extract import FILENAME_ONLY, extract, plaintext
from autoshelf.extract.base import TextBudget, clean_text
from autoshelf.extract.filename import filename_tokens

FULLWIDTH_ABC = "".join(chr(0xFF21 + i) for i in range(3))  # 'ABC' as full-width letters


# --- filename tokens and text cleaning -----------------------------------------------


@pytest.mark.parametrize(
    ("name", "tokens"),
    [
        (
            "UnsupervisedLearning_lecture-03 (2).pdf",
            ["Unsupervised", "Learning", "lecture", "03", "2"],
        ),
        ("PDFReader.v2.txt", ["PDF", "Reader", "v", "2"]),
        ("scan_0034.jpg", ["scan", "0034"]),
        ("機械学習 ノート.docx", ["機械学習", "ノート"]),
        (f"{FULLWIDTH_ABC}_full-width.txt", ["ABC", "full", "width"]),
        ("README", ["README"]),
        ("___.txt", []),
    ],
)
def test_filename_tokens(name: str, tokens: list[str]) -> None:
    assert filename_tokens(name) == tokens


def test_clean_text_normalizes_and_collapses() -> None:
    zero_width, soft_hyphen, ideographic_space = chr(0x200B), chr(0xAD), chr(0x3000)
    full_width_ai = chr(0xFF21) + chr(0xFF29)
    raw = (
        f"  Deep{zero_width} Learn{soft_hyphen}ing \t\x07 {full_width_ai}{ideographic_space}notes"
        "\r\n\r\n\r\n\r\nNext  "
    )
    assert clean_text(raw) == "Deep Learning AI notes\n\nNext"


def test_text_budget_stops_and_marks_truncation() -> None:
    budget = TextBudget(10)
    assert budget.add("abcdef")
    assert not budget.add("ghijklmnop")
    assert budget.truncated
    assert len(budget.text) <= 10


# --- plain text ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("utf-8", "k-means と PCA の講義ノート"),
        ("utf-8-sig", "k-means と PCA の講義ノート"),
        ("utf-16", "k-means と PCA の講義ノート"),
    ],
)
def test_text_encodings_with_utf(tmp_path: Path, encoding: str, text: str) -> None:
    path = tmp_path / "notes.txt"
    path.write_bytes(text.encode(encoding))
    assert extract(path).text == text


def test_japanese_legacy_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plaintext, "LEGACY_ENCODINGS", ("cp932", "cp1252"))
    path = tmp_path / "memo.txt"
    path.write_bytes("会議のメモ".encode("cp932"))
    assert extract(path).text == "会議のメモ"


def test_western_legacy_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plaintext, "LEGACY_ENCODINGS", ("cp1252",))
    path = tmp_path / "menu.txt"
    path.write_bytes("café crème".encode("cp1252"))
    assert extract(path).text == "café crème"


def test_long_text_is_truncated(tmp_path: Path) -> None:
    path = make(tmp_path / "long.md", "word " * 20_000)
    result = extract(path, max_chars=5000)
    assert result.extractor == "text"
    assert len(result.text) == 5000
    assert result.truncated


def test_multibyte_character_cut_at_read_limit_is_dropped(tmp_path: Path) -> None:
    path = make(tmp_path / "jp.txt", "あ" * 100)  # 3 bytes each in UTF-8
    result = extract(path, max_chars=10)  # reads 44 bytes: 14 whole characters + 2 bytes
    assert result.text == "あ" * 10
    assert result.truncated


def test_extensionless_text_file_is_sniffed(tmp_path: Path) -> None:
    result = extract(make(tmp_path / "Makefile", "build:\n\tpython -m build"))
    assert result.extractor == "text"
    assert "python -m build" in result.text


def test_unknown_binary_is_filename_only(tmp_path: Path) -> None:
    path = tmp_path / "Holiday photo.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic" + bytes(100))
    result = extract(path)
    assert result.extractor == FILENAME_ONLY
    assert result.error is None
    assert result.filename_tokens == ("Holiday", "photo")


# --- documents --------------------------------------------------------------------------


def test_pdf(tmp_path: Path) -> None:
    path = samples.pdf(
        tmp_path / "lecture.pdf",
        ["Principal component analysis reduces dimensions.", "Page two: eigenvectors."],
        title="PCA Lecture",
        author="A. Teacher",
    )
    result = extract(path)
    assert result.extractor == "pdf"
    assert result.title == "PCA Lecture"
    assert result.metadata["author"] == "A. Teacher"
    assert "Principal component analysis" in result.text
    assert "eigenvectors" in result.text
    assert not result.truncated


def test_pdf_stops_reading_pages_at_the_limit(tmp_path: Path) -> None:
    path = samples.pdf(tmp_path / "book.pdf", [f"Page {n} " + "text " * 300 for n in range(30)])
    result = extract(path, max_chars=3000)
    assert len(result.text) <= 3000
    assert result.truncated
    assert "Page 29" not in result.text


def test_docx(tmp_path: Path) -> None:
    path = samples.docx(
        tmp_path / "report.docx",
        title="Quarterly Finance",
        heading="Budget 予算",
        paragraph="Invoices and receipts for Q3.",
        cells=["Revenue", "1200"],
    )
    result = extract(path)
    assert result.extractor == "word"
    assert result.title == "Quarterly Finance"
    assert result.metadata["keywords"] == "test, sample"
    assert result.text.splitlines() == [
        "Budget 予算",
        "Invoices and receipts for Q3.",
        "Revenue | 1200",
    ]


def test_pptx(tmp_path: Path) -> None:
    path = samples.pptx(
        tmp_path / "slides.pptx",
        title="Clustering Deck",
        slide_title="K-means",
        body="Choose k centroids",
        notes="Mention the elbow method",
    )
    result = extract(path)
    assert result.extractor == "powerpoint"
    assert result.title == "Clustering Deck"
    for expected in ("K-means", "Choose k centroids", "eigenvalue | variance", "elbow method"):
        assert expected in result.text


def test_xlsx(tmp_path: Path) -> None:
    path = samples.xlsx(
        tmp_path / "budget.xlsx",
        title="Household budget",
        sheets={
            "Rent": [["Month", "Amount"], ["January", 950]],
            "Food": [["Groceries", None, 210.5]],
        },
    )
    result = extract(path)
    assert result.extractor == "excel"
    assert result.title == "Household budget"
    assert result.text.splitlines() == [
        "Rent",
        "Month | Amount",
        "January | 950",
        "Food",
        "Groceries | 210.5",
    ]


def test_epub(tmp_path: Path) -> None:
    path = samples.epub(
        tmp_path / "book.epub",
        title="Pattern Recognition",
        author="C. Author",
        chapters=["Bayes decision theory", "Mixture models and EM"],
    )
    result = extract(path)
    assert result.extractor == "epub"
    assert result.title == "Pattern Recognition"
    assert result.metadata["author"] == "C. Author"
    assert result.text.index("Bayes decision theory") < result.text.index("Mixture models")


def test_rtf_with_declared_japanese_code_page(tmp_path: Path) -> None:
    escaped = "".join(rf"\'{byte:02x}" for byte in "日本語".encode("cp932"))
    path = tmp_path / "letter.rtf"
    path.write_text(r"{\rtf1\ansi\ansicpg932 Hello {\b bold} " + escaped + r"\par}", "ascii")
    result = extract(path)
    assert result.extractor == "rtf"
    assert result.text == "Hello bold 日本語"


def test_html(tmp_path: Path) -> None:
    path = make(
        tmp_path / "page.html",
        "<html><head><title>Kubernetes basics</title>"
        '<meta name="description" content="Pods and services">'
        "<script>var hidden = 1;</script><style>p {}</style></head>"
        "<body><h1>Pods</h1><p>A pod runs containers.</p></body></html>",
    )
    result = extract(path)
    assert result.extractor == "html"
    assert result.title == "Kubernetes basics"
    assert result.metadata == {"description": "Pods and services"}
    assert result.text == "Pods\nA pod runs containers."


def test_notebook(tmp_path: Path) -> None:
    path = samples.notebook(
        tmp_path / "analysis.ipynb",
        [("markdown", "# Customer churn\nExploration"), ("code", "import pandas as pd")],
    )
    result = extract(path)
    assert result.extractor == "notebook"
    assert result.title == "Customer churn"
    assert result.metadata == {"language": "python"}
    assert "import pandas as pd" in result.text


def test_zip_lists_member_names(tmp_path: Path) -> None:
    path = samples.zip_archive(
        tmp_path / "course.zip", ["Week1/PCA slides.pdf", "Week2/k-means notes.md"]
    )
    result = extract(path)
    assert result.extractor == "zip listing"
    assert "Week1 / PCA slides.pdf" in result.text
    assert "k-means notes.md" in result.text


# --- media: text metadata only -------------------------------------------------------


def test_mp3_tags(tmp_path: Path) -> None:
    result = extract(samples.mp3(tmp_path / "rec.mp3", title="Lecture 5: SVMs", artist="Prof. X"))
    assert result.extractor == "media tags"
    assert result.title == "Lecture 5: SVMs"
    assert result.metadata["artist"] == "Prof. X"
    assert result.text == ""


def test_audio_without_tags(tmp_path: Path) -> None:
    path = tmp_path / "voice memo.m4a"
    path.write_bytes(bytes(64))
    result = extract(path)
    assert result.title is None
    assert result.filename_tokens == ("voice", "memo")


def test_jpeg_exif(tmp_path: Path) -> None:
    path = samples.jpeg_with_exif(
        tmp_path / "IMG_0001.jpg", description="Whiteboard: gradient descent", xp_title="授業"
    )
    result = extract(path)
    assert result.extractor == "image metadata"
    assert result.title == "授業"
    assert result.metadata["description"] == "Whiteboard: gradient descent"


def test_png_text(tmp_path: Path) -> None:
    path = samples.png_with_text(tmp_path / "chart.png", Title="Sales chart", Keywords="finance")
    result = extract(path)
    assert result.title == "Sales chart"
    assert result.metadata["keywords"] == "finance"


# --- failures --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["broken.pdf", "broken.docx", "broken.pptx", "broken.xlsx", "broken.epub", "broken.zip"]
)
def test_corrupt_file_falls_back_to_filename(tmp_path: Path, name: str) -> None:
    path = make(tmp_path / name, "this is not really that format")
    result = extract(path)
    assert result.error
    assert result.text == ""
    assert result.filename_tokens == ("broken",)


@windows_only
@pytest.mark.parametrize("name", ["broken.pdf", "broken.docx", "broken.xlsx", "broken.epub"])
def test_failed_extraction_never_leaves_the_file_open(tmp_path: Path, name: str, caplog) -> None:
    # Debug logging keeps the exception traceback alive; the file must still be closed.
    caplog.set_level("DEBUG", logger="autoshelf")
    path = make(tmp_path / name, "not really that format")
    assert extract(path).error
    os.rename(path, tmp_path / f"moved {name}")  # fails on Windows if a handle is still open


def test_encrypted_pdf_reports_why(tmp_path: Path) -> None:
    import pymupdf

    path = tmp_path / "secret.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "classified")
    doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    result = extract(path)
    assert result.error == "the PDF is password-protected"


def test_classification_text_combines_everything(tmp_path: Path) -> None:
    path = samples.docx(
        tmp_path / "ml_notes.docx",
        title="ML",
        heading="Clustering",
        paragraph="k-means",
        cells=["a"],
    )
    combined = extract(path).classification_text()
    assert combined.splitlines()[:2] == ["ml notes", "ML"]
    assert "k-means" in combined


def test_summaries(tmp_path: Path) -> None:
    text = extract(make(tmp_path / "a.md", "hello"))
    assert text.summary() == "read 5 chars via text"
    image = extract(samples.png_with_text(tmp_path / "b.png", Title="Chart"))
    assert image.summary() == "read metadata via image metadata; title 'Chart'"
    broken = extract(make(tmp_path / "c.pdf", "nope"))
    assert broken.summary().startswith("couldn't read its text (")
    unknown = extract(make(tmp_path / "d.heic", "x"))
    assert unknown.summary() == "no readable text for this file type; using the filename"
