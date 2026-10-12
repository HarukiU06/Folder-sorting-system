from pathlib import Path

import pytest
import samples
from helpers import make

from autoshelf.actions.undo import undo
from autoshelf.db import repository
from autoshelf.pipeline import TEXT_EXCERPT_CHARS, OutcomeKind, Pipeline
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


def test_file_in_inbox_stays_put(conn, root: Path, pipeline: Pipeline) -> None:
    path = make(root / "_Inbox" / "from iPhone.pdf")
    outcome = pipeline.process(path)
    assert outcome.kind is OutcomeKind.SKIPPED
    assert path.exists()
    assert repository.list_history(conn) == []


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
