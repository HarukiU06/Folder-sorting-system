from pathlib import Path

import pytest
import samples
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.undo import undo
from autoshelf.db import repository
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import (
    REASON_ASK,
    REASON_AUTO,
    TEXT_EXCERPT_CHARS,
    Outcome,
    OutcomeKind,
    Pipeline,
)
from autoshelf.watcher.selfmoves import SelfMoveRegistry


@pytest.fixture
def pipeline(conn, root: Path) -> Pipeline:
    return Pipeline(conn, root)


def test_dropped_file_goes_to_inbox_with_history(conn, root: Path, pipeline: Pipeline) -> None:
    src = make(root / "Unsupervised learning 教師なし学習.pdf", "clustering")

    outcome = pipeline.process(src)

    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == root / "_Inbox" / src.name
    assert outcome.dst.read_text(encoding="utf-8") == "clustering"
    assert not src.exists()
    entry = repository.get_history(conn, outcome.history_id)
    assert entry.state == "done"
    assert repository.get_file_by_path(conn, outcome.dst).status == "inbox"


def test_inbox_name_collision_gets_suffix(root: Path, pipeline: Pipeline) -> None:
    make(root / "_Inbox" / "scan.pdf", "older scan")
    outcome = pipeline.process(make(root / "scan.pdf", "newer scan"))
    assert outcome.dst == root / "_Inbox" / "scan (2).pdf"
    assert (root / "_Inbox" / "scan.pdf").read_text() == "older scan"


def test_dry_run_touches_nothing(conn, root: Path) -> None:
    src = make(root / "report.pdf")
    outcome = Pipeline(conn, root, dry_run=True).process(src)

    assert outcome.kind is OutcomeKind.WOULD_MOVE
    assert outcome.dst == root / "_Inbox" / "report.pdf"
    assert src.exists()
    assert not (root / "_Inbox").exists()
    assert repository.list_history(conn) == []
    assert repository.get_file_by_path(conn, src) is None


def test_file_in_inbox_without_a_classifier_waits(conn, root: Path, pipeline: Pipeline) -> None:
    path = make(root / "_Inbox" / "from iPhone.pdf")
    outcome = pipeline.process(path)
    assert outcome.kind is OutcomeKind.WAITING
    assert path.exists()
    assert repository.list_history(conn) == []
    assert repository.get_file_by_path(conn, path).status == "inbox"
    assert pipeline.process(path).kind is OutcomeKind.SKIPPED  # not reconsidered every time


def test_reprocessing_a_file_the_app_moved_to_inbox_is_a_no_op(
    conn, root: Path, pipeline: Pipeline
) -> None:
    moved = pipeline.process(make(root / "a.pdf"))
    again = pipeline.process(moved.dst)
    assert again.kind is OutcomeKind.SKIPPED
    assert moved.dst.exists()
    assert len(repository.list_history(conn)) == 1


@pytest.mark.parametrize("relative", ["Finance/invoice.pdf", "_Duplicates/invoice.pdf"])
def test_files_outside_drop_zones_are_never_moved(
    conn, root: Path, pipeline: Pipeline, relative: str
) -> None:
    path = make(root / relative)
    outcome = pipeline.process(path)
    assert outcome.kind is OutcomeKind.SKIPPED
    assert path.exists()
    assert repository.list_history(conn) == []


@pytest.mark.parametrize("name", ["~$draft.docx", "video.mp4.crdownload", "a.tmp", ".hidden"])
def test_ignored_files_are_left_alone(root: Path, pipeline: Pipeline, name: str) -> None:
    path = make(root / name)
    outcome = pipeline.process(path)
    assert outcome.kind is OutcomeKind.SKIPPED
    assert outcome.message.startswith("ignored")
    assert path.exists()


def test_undone_file_is_not_sorted_again(conn, root: Path, pipeline: Pipeline) -> None:
    src = make(root / "keep me here.pdf")
    moved = pipeline.process(src)
    undo(conn, moved.history_id)

    outcome = pipeline.process(src)

    assert outcome.kind is OutcomeKind.SKIPPED
    assert "undid" in outcome.message
    assert src.exists()


def test_exact_duplicate_is_reported_not_deleted(conn, root: Path, pipeline: Pipeline) -> None:
    first = pipeline.process(make(root / "paper.pdf", "same bytes"))
    outcome = pipeline.process(make(root / "paper copy.pdf", "same bytes"))
    assert outcome.duplicates == (first.dst,)
    assert first.dst.exists()
    assert outcome.dst.exists()


def test_vanished_file_is_skipped(root: Path, pipeline: Pipeline) -> None:
    outcome = pipeline.process(root / "gone.pdf")
    assert outcome.kind is OutcomeKind.SKIPPED


def test_own_move_destination_is_registered_for_the_watcher(conn, root: Path) -> None:
    registry = SelfMoveRegistry()
    outcome = Pipeline(conn, root, self_moves=registry).process(make(root / "a.pdf"))
    assert registry.is_expected(outcome.dst)
    assert not registry.is_expected(outcome.path)  # a new a.pdf may arrive there any time


def test_inbox_that_is_a_file_fails_cleanly(conn, root: Path, pipeline: Pipeline) -> None:
    make(root / "_Inbox", "not a folder")
    src = make(root / "a.pdf")
    outcome = pipeline.process(src)
    assert outcome.kind is OutcomeKind.FAILED
    assert src.exists()


def test_moved_file_stores_title_and_text_excerpt(conn, root: Path, pipeline: Pipeline) -> None:
    src = samples.docx(
        root / "report.docx",
        title="Quarterly report",
        heading="Budget",
        paragraph="spending " * 400,
        cells=["Revenue"],
    )
    outcome = pipeline.process(src)

    assert outcome.extraction.title == "Quarterly report"
    record = repository.get_file_by_path(conn, outcome.dst)
    title, excerpt = repository.get_file_content(conn, record.id)
    assert title == "Quarterly report"
    assert excerpt.startswith("Budget\nspending")
    assert len(excerpt) == TEXT_EXCERPT_CHARS


def test_dry_run_reports_extraction(conn, root: Path) -> None:
    outcome = Pipeline(conn, root, dry_run=True).process(make(root / "notes.md", "# PCA notes"))
    assert outcome.extraction.text == "# PCA notes"
    assert repository.get_file_by_path(conn, root / "notes.md") is None


def test_unreadable_pdf_is_still_moved(conn, root: Path, pipeline: Pipeline, caplog) -> None:
    # Debug logging keeps the parser's traceback alive; that once kept the file locked.
    caplog.set_level("DEBUG", logger="autoshelf")
    outcome = pipeline.process(make(root / "scan.pdf", "not really a PDF"))
    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.extraction.error
    assert outcome.extraction.filename_tokens == ("scan",)


# --- with the classifier ----------------------------------------------------------

THRESHOLD = 0.3
MARGIN = 0.1


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    """Two folders with descriptions; one already holds an example file."""
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank transfer")
    (root / "Recipes").mkdir()
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank")
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking oven")
    return root


@pytest.fixture
def smart(conn, shelf: Path, embedder: FakeEmbedder) -> Pipeline:
    library = LibraryIndex(conn, shelf, embedder)
    return Pipeline(conn, shelf, library=library, threshold=THRESHOLD, margin=MARGIN)


def test_clear_match_is_moved_into_its_folder(conn, shelf: Path, smart: Pipeline) -> None:
    src = make(shelf / "invoice 2025.txt", "invoice total tax due")

    outcome = smart.process(src)

    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == shelf / "Finance" / "invoice 2025.txt"
    entry = repository.get_history(conn, outcome.history_id)
    assert entry.reason == REASON_AUTO
    assert entry.confidence == pytest.approx(outcome.classification.choice.score)
    assert entry.details["candidates"][0]["folder"] == "Finance"
    assert repository.get_file_by_path(conn, outcome.dst).status == "sorted"


def test_unclear_file_goes_to_inbox_with_its_suggestions(
    conn, shelf: Path, smart: Pipeline
) -> None:
    outcome = smart.process(make(shelf / "notes.txt", "call mom buy milk"))

    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == shelf / "_Inbox" / "notes.txt"
    assert outcome.classification.choice is None
    entry = repository.get_history(conn, outcome.history_id)
    assert entry.reason == REASON_ASK
    assert {c["folder"] for c in entry.details["candidates"]} == {"Finance", "Recipes"}
    assert "not sure" in entry.details["explanation"]


def test_moved_file_keeps_its_vector_and_becomes_an_example(
    conn, shelf: Path, smart: Pipeline, embedder: FakeEmbedder
) -> None:
    outcome = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    embedded = embedder.embedded

    stats = smart.library.refresh()

    assert stats.files == 2
    assert embedder.embedded == embedded  # reused, not recomputed
    assert outcome.dst in {e.path for e in smart.library.files_in("Finance")}


def test_file_dropped_into_inbox_from_iphone_is_sorted(shelf: Path, smart: Pipeline) -> None:
    src = make(shelf / "_Inbox" / "banana bread recipe.txt", "recipe baking oven banana")
    outcome = smart.process(src)
    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == shelf / "Recipes" / src.name


def test_unclear_file_in_inbox_waits_and_is_not_reclassified(
    conn, shelf: Path, smart: Pipeline, embedder: FakeEmbedder
) -> None:
    src = make(shelf / "_Inbox" / "notes.txt", "call mom buy milk")

    first = smart.process(src)
    embedded = embedder.embedded
    second = smart.process(src)

    assert first.kind is OutcomeKind.WAITING
    assert "not sure" in first.message
    assert src.exists()
    assert repository.list_history(conn) == []
    assert second.kind is OutcomeKind.SKIPPED
    assert embedder.embedded == embedded


def test_dry_run_classifies_but_records_nothing(conn, shelf: Path, embedder) -> None:
    library = LibraryIndex(conn, shelf, embedder)
    dry = Pipeline(conn, shelf, dry_run=True, library=library, threshold=THRESHOLD, margin=MARGIN)
    src = make(shelf / "invoice 2025.txt", "invoice total tax due")

    outcome = dry.process(src)

    assert outcome.kind is OutcomeKind.WOULD_MOVE
    assert outcome.dst == shelf / "Finance" / "invoice 2025.txt"
    assert src.exists()
    assert repository.list_history(conn) == []
    assert repository.get_file_by_path(conn, src) is None


def test_folder_that_vanished_after_scoring_means_inbox(
    conn, shelf: Path, smart: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = make(shelf / "invoice 2025.txt", "invoice total tax due")
    original = smart._decide

    def decide_then_delete(zone, classification):
        decision = original(zone, classification)
        (shelf / "Finance" / "invoice 2024.txt").unlink()
        (shelf / "Finance").rmdir()  # the user deletes the folder at the worst moment
        return decision

    monkeypatch.setattr(smart, "_decide", decide_then_delete)
    outcome = smart.process(src)

    assert outcome.dst == shelf / "_Inbox" / "invoice 2025.txt"
    assert "no longer exists" in outcome.message
    assert not (shelf / "Finance").exists()  # never recreated


def test_classifier_failure_sends_the_file_to_inbox(
    shelf: Path, smart: Pipeline, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("model crashed")

    monkeypatch.setattr(smart.library, "embed", broken)
    outcome = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == shelf / "_Inbox" / "invoice 2025.txt"


def test_new_folder_is_picked_up_without_restarting(conn, shelf: Path, smart: Pipeline) -> None:
    smart.process(make(shelf / "warmup.txt", "anything"))  # first refresh happens here
    (shelf / "Travel").mkdir()
    repository.set_folder_description(conn, "Travel", "trip hotel flight itinerary")

    outcome = smart.process(make(shelf / "kyoto.txt", "trip itinerary hotel flight kyoto"))

    assert outcome.dst == shelf / "Travel" / "kyoto.txt"


@pytest.mark.parametrize(
    ("kind", "dst", "needs"),
    [
        (OutcomeKind.MOVED, Path("C:/shelf/_Inbox/a.pdf"), True),
        (OutcomeKind.MOVED, Path("C:/shelf/Finance/a.pdf"), False),
        (OutcomeKind.WAITING, None, True),
        (OutcomeKind.WOULD_MOVE, Path("C:/shelf/_Inbox/a.pdf"), False),
        (OutcomeKind.SKIPPED, None, False),
    ],
)
def test_needs_decision(kind: OutcomeKind, dst: Path | None, needs: bool) -> None:
    assert Outcome(kind, Path("a.pdf"), "", dst=dst).needs_decision is needs


# --- no rescan of every folder per file (Phase 9) ----------------------------------------------


def test_files_are_sorted_without_rescanning_the_library_each_time(
    conn, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoshelf.ml import library as library_module

    make(root / "Recipes" / "curry.txt", "recipe curry onion simmer")
    (root / "Finance").mkdir()
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking")
    scans: list[Path] = []
    real = library_module.scan_library
    monkeypatch.setattr(
        library_module, "scan_library", lambda *args, **kw: scans.append(1) or real(*args, **kw)
    )
    library = LibraryIndex(conn, root, FakeEmbedder())
    smart = Pipeline(conn, root, library=library, threshold=0.3, margin=0.1)

    first = smart.process(make(root / "banana bread recipe.txt", "recipe baking banana oven"))
    second = smart.process(make(root / "apple pie recipe.txt", "recipe baking apple oven"))

    assert first.dst.parent == second.dst.parent == root / "Recipes"
    assert len(scans) == 1  # the first decision; no rescan per file
    # The file sorted a moment ago already counts as an example of its folder.
    neighbours = {n.path.name for n in second.classification.score_of("Recipes").neighbors}
    assert "banana bread recipe.txt" in neighbours
