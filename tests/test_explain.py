""" "Why did it go here?" (SPEC 4.7): keywords, stored details and how they read."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.cli import main
from autoshelf.db import repository
from autoshelf.ml.classifier import FolderScore, Neighbor, choose
from autoshelf.ml.explain import decision_details, rule_details, shared_keywords, terms
from autoshelf.ml.learned import Adjustment, Evidence
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Pipeline
from autoshelf.wording import decision_lines as describe


def test_terms_skip_stop_words_and_split_japanese_into_pairs() -> None:
    assert terms("The K-means clustering of DATA") == ["k-means", "clustering", "data"]
    assert terms("給与明細 2025年") == ["給与", "与明", "明細"]
    assert terms("ＰＣＡ lecture") == ["pca", "lecture"]  # noqa: RUF001 (full-width on purpose)


def test_shared_keywords_prefer_words_typical_of_the_folder() -> None:
    folders = {
        "Finance": "invoice receipt tax bank invoice statement total",
        "Recipes": "recipe cake oven total minutes",
    }
    words = shared_keywords(
        "invoice 2025 total due tax", folders["Finance"], list(folders.values())
    )
    assert words[:2] == ["invoice", "tax"]  # "total" is in every folder: less telling
    assert words[-1] == "total"
    assert shared_keywords("call mom", folders["Finance"], list(folders.values())) == []


def test_japanese_keywords_are_joined_back_into_words() -> None:
    words = shared_keywords(
        "給与明細書 所得税 4月分",
        "確定申告 給与所得 所得税",
        ["確定申告 給与所得 所得税", "レシピ"],
    )
    assert "所得税" in words
    assert "得税" not in words


def scores() -> list[FolderScore]:
    return [
        FolderScore(
            "Finance",
            0.62,
            0.5,
            0.7,
            (Neighbor(Path("C:/Shelf/Finance/a.pdf"), 0.8), Neighbor(Path("b.pdf"), 0.6)),
            -0.08,
        ),
        FolderScore("Travel", 0.3, 0.3),
    ]


def test_decision_details_hold_everything_the_panel_shows() -> None:
    learned = {
        "Finance": Adjustment("Finance", -0.08, (Evidence("hotel.pdf", "Travel", 0.9),)),
        "Gone": Adjustment("Gone", -0.1, ()),  # a folder not among the top three
    }
    classification = choose(scores(), 0.4, 0.1)

    details = decision_details(classification, keywords=["invoice", "tax"], learned=learned)

    assert details["choice"] == "Finance"
    assert details["focus"] == "Finance"
    assert details["candidates"][0] == {
        "folder": "Finance",
        "score": 0.62,
        "name": 0.5,
        "files": 0.7,
        "learned": -0.08,
    }
    assert details["similar_files"][0] == {
        "path": str(Path("C:/Shelf/Finance/a.pdf")),
        "similarity": 0.8,
    }
    assert details["keywords"] == ["invoice", "tax"]
    assert [item["folder"] for item in details["learned"]] == ["Finance"]

    lines = describe(details, Path("C:/Shelf"))
    assert lines[0].startswith("Decision: best match 'Finance' 0.62")
    assert "  0.62  Finance  (name/description 0.50, files 0.70, learned -0.08)" in lines
    assert "  0.80  Finance/a.pdf" in lines
    assert "Words in common with Finance: invoice, tax" in lines
    assert lines[-1].startswith("Learned -0.08 for Finance: you moved similar files away")


def test_rule_details_read_well() -> None:
    details = rule_details(
        "Invoices",
        [{"kind": "name", "pattern": "invoice"}],
        "Finance",
        [{"kind": "name", "name": "invoice.pdf", "found": "invoice"}],
    )
    assert details["reason"]["code"] == "rule"
    assert "explanation" not in details  # data, not words
    assert describe(details) == [
        "Rule 'Invoices': file name matches /invoice/ -> Finance",
        "  matched: 'invoice.pdf' contains 'invoice'",
    ]


def test_rule_details_written_before_reasons_still_read() -> None:
    lines = describe(
        {
            "rule": {
                "name": "Invoices",
                "condition": "file name matches /invoice/",
                "target": "Finance",
                "match": "'invoice.pdf' contains 'invoice'",
            },
            "explanation": "rule 'Invoices' matched",
        }
    )
    assert lines == [
        "Rule 'Invoices': file name matches /invoice/ -> Finance",
        "  matched: 'invoice.pdf' contains 'invoice'",
    ]


def test_old_details_without_extras_still_read() -> None:
    lines = describe({"candidates": [{"folder": "A", "score": 0.5}], "explanation": "x"})
    assert lines == ["Decision: x", "Best folders:", "  0.50  A"]


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank transfer")
    make(root / "Recipes" / "cake.txt", "recipe cooking baking oven cake")
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank")
    return root


def test_pipeline_stores_the_why_with_the_move(conn, shelf: Path, capsys) -> None:
    library = LibraryIndex(conn, shelf, FakeEmbedder())
    pipeline = Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)

    outcome = pipeline.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))

    entry = repository.get_history(conn, outcome.history_id)
    assert entry.details["focus"] == "Finance"
    assert entry.details["similar_files"][0]["path"] == str(shelf / "Finance" / "invoice 2024.txt")
    assert {"invoice", "tax"} <= set(entry.details["keywords"])

    assert main(["why", str(outcome.history_id)]) == 0
    output = capsys.readouterr().out
    assert "Most similar files in Finance:" in output
    assert "Finance/invoice 2024.txt" in output
    assert main(["why", "999"]) == 1


def test_pairs_with_hiragana_are_grammar_not_keywords() -> None:
    assert terms("志望動機について話して") == ["志望", "望動", "動機"]
    assert terms("データの前処理") == ["デー", "ータ", "前処", "処理"]
