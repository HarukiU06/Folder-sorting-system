"""Search, related files and tags (SPEC 5.1, 5.3, 5.4)."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.features.related import related_files
from autoshelf.features.search import SearchIndex, query_words, snippet
from autoshelf.features.tags import (
    TagError,
    clean_tag,
    suggest_tags,
    tag_file,
    tagged_files,
    untag_file,
)
from autoshelf.ml.classifier import FolderScore, choose
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Pipeline


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def shelf(root: Path) -> Path:
    make(root / "Machine Learning" / "kmeans.md", "k-means clustering initialization centroids")
    make(root / "Machine Learning" / "backprop.md", "neural network gradient backpropagation")
    make(root / "Finance" / "給与明細.txt", "給与 明細 基本給 所得税 振込")
    make(root / "Finance" / "invoice.txt", "invoice total tax due bank transfer")
    return root


@pytest.fixture
def library(conn, shelf: Path, embedder: FakeEmbedder) -> LibraryIndex:
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()
    return index


# --- chunks -----------------------------------------------------------------------------


def test_indexing_stores_text_chunks_for_search(conn, shelf: Path, library: LibraryIndex) -> None:
    record = repository.get_file_by_path(conn, shelf / "Machine Learning" / "kmeans.md")
    rows = conn.execute(
        "SELECT idx, text FROM chunks WHERE file_id = ? ORDER BY idx", (record.id,)
    ).fetchall()
    assert rows[0]["text"].startswith("kmeans")  # the header: name words
    assert "k-means clustering" in rows[1]["text"]


def test_files_indexed_before_search_existed_are_read_again(
    conn, shelf: Path, library: LibraryIndex, embedder: FakeEmbedder
) -> None:
    conn.execute("DELETE FROM chunks")
    before = embedder.embedded
    LibraryIndex(conn, shelf, embedder).refresh()
    assert embedder.embedded > before
    assert repository.chunks_signature(conn)[0] > 0


def test_pipeline_stores_chunks_of_sorted_and_waiting_files(
    conn, shelf: Path, library: LibraryIndex
) -> None:
    pipeline = Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)
    moved = pipeline.process(make(shelf / "notes.txt", "call mom buy milk"))
    record = repository.get_file_by_path(conn, moved.dst)
    assert repository.has_chunks(conn, record.id)


# --- search -------------------------------------------------------------------------------


def test_search_by_meaning(conn, shelf: Path, library: LibraryIndex, embedder) -> None:
    hits = SearchIndex().search(conn, shelf, embedder, "clustering centroids")
    assert hits[0].path.name == "kmeans.md"
    assert hits[0].folder == "Machine Learning"
    assert hits[0].keyword  # both words appear in it
    assert "clustering" in hits[0].snippet


def test_search_finds_short_japanese_words(conn, shelf, library, embedder) -> None:
    hits = SearchIndex().search(conn, shelf, embedder, "給与")
    assert [hit.path.name for hit in hits if hit.keyword] == ["給与明細.txt"]


def test_exact_words_rank_higher(conn, shelf, library, embedder) -> None:
    hits = SearchIndex().search(conn, shelf, embedder, '"bank transfer"')
    assert hits[0].path.name == "invoice.txt"
    assert hits[0].score > hits[0].similarity


def test_search_skips_vanished_files_and_reloads_after_changes(
    conn, shelf: Path, library: LibraryIndex, embedder
) -> None:
    index = SearchIndex()
    assert index.search(conn, shelf, embedder, "invoice")[0].path.name == "invoice.txt"
    (shelf / "Finance" / "invoice.txt").unlink()
    assert all(
        hit.path.name != "invoice.txt" for hit in index.search(conn, shelf, embedder, "invoice")
    )
    make(shelf / "Finance" / "receipt.txt", "receipt invoice paid")
    library.refresh()
    assert index.search(conn, shelf, embedder, "receipt")[0].path.name == "receipt.txt"
    assert index.search(conn, shelf, embedder, "   ") == []


def test_query_words_and_snippets() -> None:
    assert query_words('k-means "bank transfer" 給与') == ["k-means", "bank transfer", "給与"]
    text = "x " * 200 + "the needle is here " + "y " * 200
    piece = snippet(text, ["needle"], size=60)
    assert "needle" in piece
    assert piece.startswith("...") and piece.endswith("...")


# --- related files ------------------------------------------------------------------------------


def test_related_files_are_the_nearest_neighbours(conn, shelf: Path, library) -> None:
    make(shelf / "Machine Learning" / "kmeans 2.md", "k-means clustering centroids elbow")
    library.refresh()
    found = related_files(conn, shelf, shelf / "Machine Learning" / "kmeans.md")
    assert found[0].path.name == "kmeans 2.md"
    assert len(found) == 4
    assert related_files(conn, shelf, shelf / "unknown.md") == []


# --- tags --------------------------------------------------------------------------------


def test_tags_are_virtual_folders(conn, shelf: Path, library) -> None:
    kmeans = shelf / "Machine Learning" / "kmeans.md"
    invoice = shelf / "Finance" / "invoice.txt"

    assert tag_file(conn, kmeans, ["Exam  prep", "Exam prep"], shelf) == ["Exam prep"]
    assert tag_file(conn, invoice, ["exam prep", "Taxes"], shelf) == ["Exam prep", "Taxes"]
    assert repository.tag_counts(conn) == [("Exam prep", 2), ("Taxes", 1)]
    assert tagged_files(conn, "exam prep") == sorted([invoice, kmeans], key=str)
    entry = repository.list_history(conn, 1)[0]
    assert (entry.action, entry.reason, entry.state) == ("tag", "tag", "done")
    assert entry.details == {"tag": "Taxes"}

    assert untag_file(conn, invoice, "Taxes", shelf)
    assert not untag_file(conn, invoice, "Taxes", shelf)
    assert repository.tag_counts(conn) == [("Exam prep", 2)]  # unused tags disappear


def test_tags_follow_a_file_that_is_moved(conn, shelf: Path, library) -> None:
    kmeans = shelf / "Machine Learning" / "kmeans.md"
    tag_file(conn, kmeans, ["Exam prep"], shelf)
    target = shelf / "Finance" / "kmeans.md"
    kmeans.rename(target)  # moved in Explorer
    library.refresh()
    assert tagged_files(conn, "Exam prep") == [target]


def test_bad_tags_are_refused(conn, shelf: Path) -> None:
    with pytest.raises(TagError):
        clean_tag("   ")
    with pytest.raises(TagError):
        clean_tag("x" * 41)
    with pytest.raises(TagError, match="no longer"):
        tag_file(conn, shelf / "gone.txt", ["A"], shelf)


def test_close_runner_up_folders_are_suggested_as_tags() -> None:
    def scores(**values: float) -> list[FolderScore]:
        return [FolderScore(key.replace("__", "/"), value, value) for key, value in values.items()]

    tie = choose(scores(Medicine=0.55, Biology=0.52, Finance=0.2), 0.4, 0.1)
    assert suggest_tags(tie) == ["Medicine", "Biology"]
    assert suggest_tags(tie, chosen="Medicine") == ["Biology"]
    clear = choose(scores(Study__Medicine=0.7, Study=0.68, Biology=0.3), 0.4, 0.1)
    assert suggest_tags(clear) == []  # the parent of the chosen folder isn't a tag
    assert suggest_tags(None) == []


# --- through the app and the command line ----------------------------------------------------


@pytest.fixture
def service(paths: AppPaths, shelf: Path):
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(shelf)
    yield app
    app.release()


def test_inbox_offers_tags_and_assign_applies_them(service: AppService, shelf: Path) -> None:
    repository_folder = shelf / "Machine Learning"
    assert repository_folder.is_dir()
    make(shelf / "_Inbox" / "lecture.md", "k-means clustering neural network gradient")
    (item,) = service.inbox()
    assert isinstance(item.tag_suggestions, tuple)

    moved = service.assign(item.path, "Machine Learning", tags=["Exam prep"])
    assert service.tagged("Exam prep") == [moved.dst]
    info = service.file_info(moved.dst)
    assert info.tags == ["Exam prep"]
    assert info.folder == "Machine Learning"


def test_cli_search_tags_and_related(
    service: AppService, shelf: Path, fake_model, capsys: pytest.CaptureFixture[str]
) -> None:
    service.folders()
    assert main(["index"]) == 0
    capsys.readouterr()

    assert main(["search", "clustering centroids"]) == 0
    assert "kmeans.md  in Machine Learning  [contains the words]" in capsys.readouterr().out

    kmeans = str(shelf / "Machine Learning" / "kmeans.md")
    assert main(["tag", kmeans, "Exam prep"]) == 0
    assert main(["tags"]) == 0
    assert "Exam prep  (1)" in capsys.readouterr().out
    assert main(["tags", "Exam prep"]) == 0
    assert kmeans in capsys.readouterr().out
    assert main(["untag", kmeans, "Exam prep"]) == 0
    assert "Removed the tag" in capsys.readouterr().out

    assert main(["related", kmeans]) == 0
    assert "backprop.md" in capsys.readouterr().out


def test_search_results_carry_cached_summaries(conn, shelf: Path, library, embedder) -> None:
    record = repository.get_file_by_path(conn, shelf / "Machine Learning" / "kmeans.md")
    repository.set_summary(conn, record.id, "Notes on k-means.")
    hits = SearchIndex().search(conn, shelf, embedder, "clustering centroids")
    assert hits[0].summary == "Notes on k-means."
    assert all(hit.summary is None for hit in hits[1:])
