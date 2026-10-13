"""Features that use the optional LLM (with a fake one), and the parts that work without it:
summaries, better names, new-folder suggestions, Ask your library, study mode."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fakes import FakeEmbedder, FakeProvider
from helpers import make

from autoshelf.actions.undo import undo
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.features.ask import ask, build_prompt, cited_numbers
from autoshelf.features.new_folders import (
    FolderNameError,
    clean_folder_name,
    clusters,
    create_folder,
    suggest_folders,
)
from autoshelf.features.rename import (
    RenameError,
    clean_stem,
    is_junk_name,
    propose_name,
    rename,
    rename_suggestions,
)
from autoshelf.features.search import SearchIndex
from autoshelf.features.study import export_csv, json_array, make_cards, reading_list
from autoshelf.features.summaries import file_summary
from autoshelf.llm.base import LLMError
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import Pipeline


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    make(root / "Finance" / "invoice.txt", "invoice total tax due bank transfer")
    make(root / "Machine Learning" / "kmeans.md", "# K-means clustering\ncentroids and inertia")
    make(root / "Machine Learning" / "pca.md", "# Principal component analysis\nvariance")
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank")
    return root


@pytest.fixture
def library(conn, shelf: Path, embedder: FakeEmbedder) -> LibraryIndex:
    index = LibraryIndex(conn, shelf, embedder)
    index.refresh()
    return index


# --- summaries ------------------------------------------------------------------------


def test_summaries_are_made_once_and_cached(conn, shelf: Path, library) -> None:
    path = shelf / "Finance" / "invoice.txt"
    provider = FakeProvider("An invoice  for\nhosting.", "A second summary.")

    assert file_summary(conn, provider, path) == "An invoice for hosting."  # one line
    assert file_summary(conn, provider, path) == "An invoice for hosting."  # cached
    assert len(provider.calls) == 1
    system, (message,) = provider.calls[0]
    assert "2 or 3 short sentences" in system
    assert "invoice total tax due" in message.content

    assert file_summary(conn, provider, path, refresh=True) == "A second summary."


def test_a_changed_file_loses_its_summary(conn, shelf: Path, library) -> None:
    path = shelf / "Finance" / "invoice.txt"
    file_summary(conn, FakeProvider("Old."), path)
    path.write_text("a completely new text", encoding="utf-8")
    library.refresh()
    record = repository.get_file_by_path(conn, path)
    assert repository.get_summary(conn, record.id) is None


# --- better names --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "junk"),
    [
        ("scan_0034.pdf", True),
        ("document(3).docx", True),
        ("download.pdf", True),
        ("IMG_2041.heic", True),
        ("3f2504e0-4f89-11d3-9a0c-0305e82c3301.pdf", True),
        ("20250314_1201.jpg", True),
        ("名称未設定 2.pages", True),
        ("Kyoto trip.docx", False),
        ("lecture-05.pdf", False),
        ("report v2.docx", False),
    ],
)
def test_junk_names(name: str, junk: bool) -> None:
    assert is_junk_name(name) is junk


def test_clean_names_are_safe_on_windows() -> None:
    assert clean_stem('Q3: "Final" report/notes?') == "Q3 Final report notes"
    assert clean_stem("con") == "con file"
    assert len(clean_stem("word " * 50)) <= 100
    assert clean_stem("   ...  ") == ""


def test_name_proposals_come_from_the_title_then_the_heading(tmp_path: Path) -> None:
    path = tmp_path / "scan_0034.pdf"
    proposal = propose_name(path, "Microsoft Word - Unsupervised Learning.docx", "text")
    assert (proposal.new_name, proposal.source) == ("Unsupervised Learning.pdf", "title")
    heading = propose_name(path, "untitled", "\n# Kubernetes basics\nPods and nodes")
    assert (heading.new_name, heading.source) == ("Kubernetes basics.pdf", "heading")
    assert propose_name(path, None, "123\n\n") is None
    ai = propose_name(path, None, "", FakeProvider('"Lecture 5: Autoencoders"\n'))
    assert (ai.new_name, ai.source) == ("Lecture 5 Autoencoders.pdf", "AI")


def test_rename_suggestions_and_an_undoable_rename(conn, shelf: Path, library) -> None:
    pipeline = Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)
    pipeline.process(make(shelf / "scan_0034.txt", "Invoice March 2025\ninvoice total tax due"))
    make(shelf / "Finance" / "Invoice March 2025.txt", "already taken")
    (proposal,) = rename_suggestions(conn, shelf)
    assert proposal.new_name == "Invoice March 2025.txt"

    result = rename(conn, proposal.path, proposal.new_name, root=shelf)

    assert result.dst.name == "Invoice March 2025 (2).txt"  # never overwrites
    entry = repository.get_history(conn, result.history_id)
    assert (entry.action, entry.reason) == ("rename", "renamed (you approved)")
    undo(conn, result.history_id)
    assert proposal.path.exists()
    with pytest.raises(RenameError):
        rename(conn, proposal.path, "  ", root=shelf)


# --- new folders -------------------------------------------------------------------------------


def test_clusters_join_similar_vectors() -> None:
    vectors = np.array([[1, 0], [0.99, 0.14], [0, 1], [0.1, 0.99]], dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    assert sorted(map(sorted, clusters(vectors, 0.8))) == [[0, 1], [2, 3]]


def test_new_folder_suggested_for_a_topic_without_a_folder(
    conn, shelf: Path, library, paths: AppPaths
) -> None:
    pipeline = Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)
    for number in range(3):
        pipeline.process(
            make(
                shelf / f"k8s notes {number}.txt",
                f"kubernetes pods deployment cluster nodes yaml kubectl {number}",
            )
        )
    pipeline.process(make(shelf / "milk.txt", "call mom buy milk"))

    (suggestion,) = suggest_folders(conn, shelf, library, threshold=0.3)

    assert suggestion.name.startswith("Kubernetes") or "kubernetes" in suggestion.keywords
    assert len(suggestion.paths) == 3
    assert not (shelf / suggestion.name).exists()  # only a suggestion

    service = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    service.set_root(shelf)
    key, moved = service.create_folder("Kubernetes", list(suggestion.paths))
    assert key == "Kubernetes"
    assert len(moved) == 3
    assert len(list((shelf / "Kubernetes").iterdir())) == 3
    assert {row["source"] for row in repository.list_corrections(conn)} == {"inbox"}


def test_folder_names_are_checked(root: Path) -> None:
    assert clean_folder_name('_Private: "notes"') == "Private notes"
    with pytest.raises(FolderNameError, match="can't be a folder name"):
        create_folder(root, "_Hidden")
    assert create_folder(root, "Kubernetes") == "Kubernetes"
    with pytest.raises(FolderNameError, match="already exists"):
        create_folder(root, "Kubernetes")
    assert create_folder(root, "Pods", parent="Kubernetes") == "Kubernetes/Pods"


# --- ask your library -----------------------------------------------------------------------------


def test_answers_cite_the_excerpts_they_use(conn, shelf: Path, library, embedder) -> None:
    provider = FakeProvider("K-means moves centroids [1]. See also [9] and [1].")

    answer = ask(conn, shelf, provider, embedder, SearchIndex(), "how does k-means work?")

    assert answer.cited == (1,)
    assert answer.sources[0].path.name == "kmeans.md"
    system, messages = provider.calls[0]
    assert "Cite the excerpts" in system
    assert "[1] kmeans.md (in Machine Learning)" in messages[-1].content
    follow_up = ask(conn, shelf, provider, embedder, SearchIndex(), "and PCA?", history=[answer])
    assert [m.role for m in provider.calls[1][1]] == ["user", "assistant", "user"]
    assert follow_up.question == "and PCA?"
    with pytest.raises(LLMError):
        ask(conn, shelf, provider, embedder, SearchIndex(), "  ")


def test_citation_helpers() -> None:
    assert cited_numbers("a [2] b [2][3] c [7]", 3) == (2, 3)
    assert build_prompt("q?", []).endswith("Question: q?")


# --- study mode --------------------------------------------------------------------------------


def test_reading_list_follows_the_ais_order() -> None:
    files = [(Path(f"{name}.pdf"), None, None) for name in ("advanced", "intro", "middle")]
    reply = 'Here you go:\n```json\n[{"n": 2, "why": "basics"}, {"n": 3, "why": "next"}]\n```'

    ordered = reading_list(FakeProvider(reply), files)

    assert [item.path.stem for item in ordered] == ["intro", "middle", "advanced"]
    assert ordered[0].reason == "basics"
    assert ordered[2].reason == ""  # left out by the AI: kept, at the end


def test_flashcards_are_parsed_stored_and_exported(conn, tmp_path: Path) -> None:
    reply = json.dumps([{"q": "What is PCA?", "a": "A projection"}, {"q": "", "a": "x"}])
    cards = make_cards(FakeProvider(reply), "PCA", "principal components")
    assert [(card.question, card.answer) for card in cards] == [("What is PCA?", "A projection")]
    with pytest.raises(LLMError):
        make_cards(FakeProvider("no list here"), "PCA", "text")
    with pytest.raises(LLMError):
        json_array("[not json")

    repository.add_flashcards(conn, [("Q1", "A1")], file_id=None, folder="Machine Learning/PCA")
    repository.add_flashcards(conn, [("Q2", "A2")], file_id=None, folder="Finance")
    stored = repository.list_flashcards(conn, "machine learning")
    assert [card.question for card in stored] == ["Q1"]

    target = tmp_path / "cards.csv"
    assert export_csv(stored, target) == 1
    assert target.read_text(encoding="utf-8").strip() == "Q1,A1"
    with pytest.raises(FileExistsError):
        export_csv(stored, target)  # never overwrites


# --- through the app and the command line -------------------------------------------------------


@pytest.fixture
def service(paths: AppPaths, shelf: Path, library):
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(shelf)
    yield app
    app.release()


def test_ai_features_need_a_provider(service: AppService, shelf: Path) -> None:
    assert not service.ai_enabled
    with pytest.raises(LLMError, match="Turn on an AI provider"):
        service.summarize(shelf / "Finance" / "invoice.txt")
    service.save_settings(replace(service.settings(), llm_provider="claude"))
    with pytest.raises(LLMError, match="No Claude API key"):
        service.summarize(shelf / "Finance" / "invoice.txt")
    service.save_api_key("sk-ant-abcd1234")
    assert service.api_key_status() == "saved (...1234)"
    service.forget_api_key()
    assert service.api_key_status() == "not saved"


def test_app_features_with_a_fake_ai(
    service: AppService, shelf: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = FakeProvider(
        "A k-means note.",
        '[{"n": 2}, {"n": 1}]',
        '[{"q": "What is k-means?", "a": "Clustering"}]',
    )
    monkeypatch.setattr(service, "provider", lambda: replies)
    kmeans = shelf / "Machine Learning" / "kmeans.md"

    assert service.summarize(kmeans) == "A k-means note."
    assert service.file_info(kmeans).summary == "A k-means note."
    order = service.reading_list("machine learning")
    assert [item.path.name for item in order] == ["pca.md", "kmeans.md"]
    (card,) = service.make_flashcards(path=kmeans)
    assert card.folder == "Machine Learning"
    assert service.flashcards("Machine Learning")[0].question == "What is k-means?"


def test_inbox_offers_a_better_name_for_junk_names(service: AppService, shelf: Path) -> None:
    make(shelf / "_Inbox" / "scan_0034.txt", "Quarterly Report\nrevenue grew")
    (item,) = service.inbox()
    assert item.rename.new_name == "Quarterly Report.txt"
    moved = service.rename(item.path, item.rename.new_name)
    assert moved.dst == shelf / "_Inbox" / "Quarterly Report.txt"


def test_cli_ai_commands(
    service: AppService,
    shelf: Path,
    fake_model,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import autoshelf.app_service as app_service_module

    fake = FakeProvider("Kmeans answer [1].")
    monkeypatch.setattr(app_service_module.AppService, "provider", lambda self: fake)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "sk-ant-cli-key")

    assert main(["config", "--ai", "claude", "--api-key"]) == 0
    output = capsys.readouterr().out
    assert "Saved the key" in output
    assert "AI (optional):    Claude API (Anthropic), model claude-opus-5-5" in output
    assert "sk-ant-cli-key" not in output

    assert main(["ask", "how does k-means work?"]) == 0
    output = capsys.readouterr().out
    assert "Kmeans answer [1]." in output
    assert "[1] " in output.split("Sources:")[1]

    make(shelf / "_Inbox" / "scan_0001.txt", "Meeting Minutes\nwe met")
    service.inbox()
    assert main(["rename", str(shelf / "_Inbox" / "scan_0001.txt")]) == 0
    assert "Renamed scan_0001.txt -> Meeting Minutes.txt" in capsys.readouterr().out
    assert main(["config", "--forget-api-key", "--ai", "off"]) == 0
