"""Re-sorting a folder's loose files when a new folder appears inside it."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.learning import Relocation
from autoshelf.db import repository
from autoshelf.features import resort
from autoshelf.ml.library import LibraryIndex

LOOSE = {
    "lecture notes 1.txt": "eigenvalues and eigenvectors",
    "Calculus notes.md": "limits and derivatives",
    "note_physics.txt": "newton laws momentum",
    "deploy.txt": "kubernetes cluster pods services",
    "pancakes.txt": "flour eggs milk",
}


@pytest.fixture
def shelf(root: Path) -> Path:
    for name, text in LOOSE.items():
        make(root / "My Folder" / name, text)
    make(root / "Other" / "more notes.txt", "notes elsewhere")
    return root


def _plan(conn, root: Path, parent: str = "My Folder", **options) -> resort.ResortPlan:
    library = LibraryIndex(conn, root, FakeEmbedder())
    library.refresh()
    return resort.plan_resort(conn, root, library, parent, **options)


def _names(candidates) -> list[tuple[str, str, str]]:
    return sorted((item.path.name, item.target, item.match) for item in candidates)


def test_is_placeholder() -> None:
    for name in ("New folder", "new folder (3)", "新しいフォルダー (2)", "untitled folder 2"):
        assert resort.is_placeholder(name), name
    assert resort.is_placeholder("名称未設定フォルダ")
    for name in ("Notes", "New folders of 2024", "untitled"):
        assert not resort.is_placeholder(name), name


def test_name_matches() -> None:
    assert resort.name_matches("Notes", "Calculus notes.md")
    assert resort.name_matches("Notes", "note_physics.txt")
    assert resort.name_matches("Notes", "week4.txt", title="Lecture notes, week 4")
    assert not resort.name_matches("Meeting notes", "meeting 2025.md")
    assert resort.name_matches("Meeting notes", "Meeting Notes Oct.md")
    assert resort.name_matches("請求書", "請求書_0412.pdf")
    assert resort.name_matches("資料", "講義資料第3回.pdf")
    assert not resort.name_matches("Notes", "nothing.txt")


def test_plan_moves_name_matches_only_inside_parent(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Notes").mkdir()

    plan = _plan(conn, shelf, triggers=("My Folder/Notes",))

    assert _names(plan.moves) == [
        ("Calculus notes.md", "My Folder/Notes", "name"),
        ("lecture notes 1.txt", "My Folder/Notes", "name"),
        ("note_physics.txt", "My Folder/Notes", "name"),
    ]
    assert all(item.path.parent == shelf / "My Folder" for item in plan.moves + plan.maybes)


def test_two_name_matches_is_not_moved(conn, shelf: Path) -> None:
    make(shelf / "My Folder" / "meeting notes oct.md", "agenda")
    (shelf / "My Folder" / "Notes").mkdir()
    (shelf / "My Folder" / "Meeting notes").mkdir()

    plan = _plan(conn, shelf)

    moved = {item.path.name: item.target for item in plan.moves}
    assert "meeting notes oct.md" not in moved
    assert moved["Calculus notes.md"] == "My Folder/Notes"


def test_rule_inside_parent_moves(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Recipes").mkdir()
    repository.insert_rule(
        conn, name="Pancakes", target_folder="My Folder/Recipes", kind="name", pattern="pancake"
    )
    repository.insert_rule(conn, name="Away", target_folder="Other", kind="name", pattern="deploy")

    plan = _plan(conn, shelf)

    assert _names(plan.moves) == [("pancakes.txt", "My Folder/Recipes", "rule")]


def test_maybes_need_threshold_and_both_margins(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Kubernetes").mkdir()
    (shelf / "My Folder" / "Docker").mkdir()
    repository.set_folder_description(conn, "My Folder/Kubernetes", "kubernetes cluster pods")
    make(shelf / "My Folder" / "both.txt", "kubernetes docker")

    plan = _plan(conn, shelf, threshold=0.4, margin=0.1)

    assert _names(plan.maybes) == [("deploy.txt", "My Folder/Kubernetes", "content")]
    assert plan.maybes[0].score is not None and plan.maybes[0].score >= 0.4
    assert plan.moves == []


def test_inbox_branch_moves_clear_matches(conn, root: Path) -> None:
    make(root / "_Inbox" / "pancake recipe.txt", "breakfast")
    make(root / "_Inbox" / "cake.txt", "baking flour sugar oven")
    make(root / "_Inbox" / "tax.txt", "income tax return")
    (root / "Recipes").mkdir()
    (root / "Finance").mkdir()
    repository.set_folder_description(conn, "Recipes", "baking flour sugar oven")

    plan = _plan(conn, root, parent="", triggers=("Recipes",))

    assert _names(plan.moves) == [
        ("cake.txt", "Recipes", "inbox"),
        ("pancake recipe.txt", "Recipes", "name"),
    ]
    assert plan.maybes == []


def test_apply_moves_as_one_batch(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Notes").mkdir()
    plan = _plan(conn, shelf, triggers=("My Folder/Notes",))

    result = resort.apply_moves(conn, shelf, plan, plan.moves, chosen_by_user=False)

    assert len(result.moved) == 3 and result.failed == []
    rows = repository.list_batch(conn, result.batch_id)
    assert {(row.reason, row.details["reason"]["match"]) for row in rows} == {("resort", "name")}
    assert (shelf / "My Folder" / "Notes" / "Calculus notes.md").exists()
    assert repository.list_corrections(conn) == []


def test_apply_user_choice_records_correction(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Kubernetes").mkdir()
    repository.set_folder_description(conn, "My Folder/Kubernetes", "kubernetes cluster pods")
    plan = _plan(conn, shelf)

    result = resort.apply_moves(conn, shelf, plan, plan.maybes, chosen_by_user=True)

    assert [item.dst.name for item in result.moved] == ["deploy.txt"]
    learned = repository.list_corrections(conn)
    assert [(row["from_folder"], row["to_folder"]) for row in learned] == [
        ("My Folder", "My Folder/Kubernetes")
    ]


def test_find_new_folders(conn, root: Path) -> None:
    first = resort.find_new_folders(conn, ["A", "A/B", "New folder"])
    assert first.seeded and first.new == [] and first.parents == {}
    resort.remember_folders(conn, ["A", "A/B"])

    news = resort.find_new_folders(
        conn, ["A", "A/B", "A/Notes", "New folder", "Pasted", "Pasted/Sub", "C", "Moved"]
    )
    assert not news.seeded
    assert news.new == ["A/Notes", "C", "Moved", "Pasted", "Pasted/Sub"]
    assert news.parents == {"A": ["A/Notes"], "": ["C", "Moved", "Pasted"]}

    relocated = Relocation(1, root / "x" / "a.txt", root / "Moved" / "a.txt", "x", "Moved", None)
    news = resort.find_new_folders(conn, ["A", "Moved", "A/Notes"], [relocated])
    assert news.renamed == ["Moved"]
    assert news.parents == {"A": ["A/Notes"]}
