"""Answering the Inbox: listing waiting files and putting them into a chosen folder."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.inbox import (
    REASON_USER,
    AssignError,
    assign,
    resolve_folder,
    waiting_files,
)
from autoshelf.actions.undo import undo
from autoshelf.db import repository
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Pipeline


def test_waiting_files_lists_inbox_files_by_name(root: Path) -> None:
    make(root / "_Inbox" / "b.pdf")
    make(root / "_Inbox" / "A note.txt")
    make(root / "_Inbox" / "~$temp.docx")
    (root / "_Inbox" / "subfolder").mkdir()
    assert [p.name for p in waiting_files(root)] == ["A note.txt", "b.pdf"]


def test_waiting_files_without_an_inbox(root: Path) -> None:
    assert waiting_files(root) == []


@pytest.mark.parametrize(
    "given", ["machine learning/unsupervised", "Machine Learning\\Unsupervised"]
)
def test_resolve_folder_matches_any_case_and_separator(root: Path, given: str) -> None:
    (root / "Machine Learning" / "Unsupervised").mkdir(parents=True)
    assert resolve_folder(root, given) == "Machine Learning/Unsupervised"


@pytest.mark.parametrize("folder", ["Taxes", "_Duplicates", "_Inbox"])
def test_resolve_folder_refuses_missing_and_reserved_folders(root: Path, folder: str) -> None:
    (root / "_Duplicates").mkdir()
    (root / "_Inbox").mkdir()
    with pytest.raises(AssignError, match="never creates folders"):
        resolve_folder(root, folder)
    assert not (root / "Taxes").exists()


def test_assign_moves_the_file_and_records_a_correction(conn, root: Path) -> None:
    (root / "Finance").mkdir()
    src = make(root / "_Inbox" / "receipt 領収書.pdf", "receipt")

    result = assign(conn, src, "finance", root=root)

    assert result.dst == root / "Finance" / "receipt 領収書.pdf"
    assert not src.exists()
    entry = repository.get_history(conn, result.history_id)
    assert entry.reason == REASON_USER
    (correction,) = repository.list_corrections(conn)
    assert correction["to_folder"] == "Finance"
    assert correction["source"] == "inbox"
    assert correction["file_id"] == result.file_id


def test_assign_remembers_what_the_classifier_suggested(conn, root: Path) -> None:
    make(root / "Finance" / "invoice.txt", "invoice tax")
    make(root / "Recipes" / "curry.txt", "recipe curry")
    library = LibraryIndex(conn, root, FakeEmbedder())
    pipeline = Pipeline(conn, root, library=library, threshold=0.99, margin=0.5)
    waiting = pipeline.process(make(root / "tax invoice.txt", "invoice tax")).dst

    assign(conn, waiting, "Recipes", root=root)

    (correction,) = repository.list_corrections(conn)
    assert correction["from_folder"] == "Finance"
    assert correction["to_folder"] == "Recipes"


def test_assigned_file_can_be_undone(conn, root: Path) -> None:
    (root / "Finance").mkdir()
    src = make(root / "_Inbox" / "receipt.pdf")
    result = assign(conn, src, "Finance", root=root)

    undo(conn, result.history_id)

    assert src.exists()


def test_assign_refuses_files_already_in_a_folder(conn, root: Path) -> None:
    (root / "Recipes").mkdir()
    path = make(root / "Finance" / "invoice.pdf")
    with pytest.raises(AssignError, match="isn't waiting"):
        assign(conn, path, "Recipes", root=root)
    assert path.exists()


def test_assign_never_overwrites(conn, root: Path) -> None:
    make(root / "Finance" / "scan.pdf", "older")
    result = assign(conn, make(root / "_Inbox" / "scan.pdf", "newer"), "Finance", root=root)
    assert result.dst.name == "scan (2).pdf"
    assert (root / "Finance" / "scan.pdf").read_text() == "older"
