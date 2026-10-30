"""Re-sorting a folder's loose files when a new folder appears inside it."""

from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions import folders
from autoshelf.actions.learning import Relocation
from autoshelf.actions.undo import undo
from autoshelf.db import repository
from autoshelf.extract import extract
from autoshelf.features import resort
from autoshelf.ml.library import LibraryIndex, store_vector

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


def _waiting(conn, root: Path, name: str, text: str) -> Path:
    """A file the watcher left in _Inbox: its vector is stored, as the pipeline does."""
    path = make(root / "_Inbox" / name, text)
    st = path.stat()
    file_id = repository.upsert_file(conn, path, sha256=None, size=st.st_size, mtime=st.st_mtime)
    library = LibraryIndex(conn, root, FakeEmbedder())
    document = library.embed(extract(path), name)
    store_vector(conn, file_id, document, model=library.model, size=st.st_size, mtime=st.st_mtime)
    return path


def test_inbox_branch_moves_clear_matches(conn, root: Path) -> None:
    _waiting(conn, root, "pancake recipe.txt", "breakfast")
    _waiting(conn, root, "cake.txt", "baking flour sugar oven")
    _waiting(conn, root, "tax.txt", "income tax return")
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


def test_a_file_put_back_is_not_moved_there_again(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Notes").mkdir()
    plan = _plan(conn, shelf, triggers=("My Folder/Notes",))
    result = resort.apply_moves(conn, shelf, plan, plan.moves, chosen_by_user=False)
    moved = {item.dst.name: item for item in result.moved}
    undo(conn, moved["Calculus notes.md"].history_id)  # undone in History
    folders.move_files(conn, shelf, [moved["lecture notes 1.txt"].dst], "My Folder")  # by hand
    (shelf / "My Folder" / "Exams").mkdir()

    again = _plan(conn, shelf, triggers=("My Folder/Exams",))

    put_back = {"Calculus notes.md", "lecture notes 1.txt"}
    assert [item for item in again.moves + again.maybes if item.path.name in put_back] == []


def test_a_put_back_file_matching_two_folders_by_name_stays(conn, shelf: Path) -> None:
    (shelf / "My Folder" / "Notes").mkdir()
    (shelf / "My Folder" / "Lecture").mkdir()
    index = LibraryIndex(conn, shelf, FakeEmbedder())
    index.refresh()
    record = repository.get_file_by_path(conn, shelf / "My Folder" / "lecture notes 1.txt")
    repository.record_correction(
        conn,
        file_id=record.id,
        from_folder="My Folder/Notes",
        to_folder="My Folder",
        source="external",
    )

    plan = _plan(conn, shelf, triggers=("My Folder/Lecture",))

    assert "lecture notes 1.txt" not in {item.path.name for item in plan.moves}


def test_moving_known_files_reads_none_of_them(conn, shelf: Path, monkeypatch) -> None:
    (shelf / "My Folder" / "Notes").mkdir()
    plan = _plan(conn, shelf, triggers=("My Folder/Notes",))

    def read(path: Path) -> str:
        raise AssertionError(f"{path.name} was read")

    monkeypatch.setattr("autoshelf.actions.mover.sha256_file", read)
    result = resort.apply_moves(conn, shelf, plan, plan.moves, chosen_by_user=False)

    assert len(result.moved) == 3 and result.failed == []


def test_inbox_files_not_indexed_are_left_alone(conn, root: Path, monkeypatch) -> None:
    make(root / "_Inbox" / "cake.txt", "baking flour sugar oven")
    (root / "Recipes").mkdir()
    repository.set_folder_description(conn, "Recipes", "baking flour sugar oven")

    def read(*args):
        raise AssertionError("cake.txt was read")

    monkeypatch.setattr(LibraryIndex, "vectorize", read)
    plan = _plan(conn, root, parent="", triggers=("Recipes",))

    assert plan.moves == [] and plan.maybes == []


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
