"""Rules that override the classifier (SPEC 4.4): matching, order, the pipeline and the CLI."""

from dataclasses import replace
from pathlib import Path

import pytest
from fakes import FakeEmbedder
from helpers import make

from autoshelf.actions.inbox import AssignError
from autoshelf.actions.rules import (
    RuleError,
    check_rule,
    extensions,
    first_match,
    match_rule,
)
from autoshelf.app_service import AppService
from autoshelf.cli import main
from autoshelf.config import AppPaths
from autoshelf.db import repository
from autoshelf.db.repository import RuleRecord
from autoshelf.extract import Extraction
from autoshelf.ml.library import LibraryIndex
from autoshelf.pipeline import OutcomeKind, Pipeline


def rule(kind: str, pattern: str, target: str = "Finance", **changes) -> RuleRecord:
    return replace(RuleRecord(1, "Test rule", 1, True, kind, pattern, target), **changes)


def extraction(text: str = "", title: str | None = None, **metadata: str) -> Extraction:
    return Extraction("text", (), text=text, title=title, metadata=metadata)


# --- matching -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "pattern", "message"),
    [
        ("name", "invoice(", "not a valid regular expression"),
        ("name", "  ", "needs something to look for"),
        ("colour", "red", "Unknown kind"),
        ("extension", ", ;", "contains no extension"),
    ],
)
def test_invalid_rules_are_refused(kind: str, pattern: str, message: str) -> None:
    with pytest.raises(RuleError, match=message):
        check_rule("My rule", kind, pattern)


def test_rule_needs_a_name() -> None:
    with pytest.raises(RuleError, match="name"):
        check_rule(" ", "extension", ".pdf")


def test_extensions_are_normalised() -> None:
    assert extensions(".PDF, docx;*.md  txt") == {".pdf", ".docx", ".md", ".txt"}


def test_name_rule_searches_the_file_name_in_any_case() -> None:
    found = match_rule(rule("name", "invoice|receipt"), Path("Amazon RECEIPT 2025.pdf"), None)
    assert found == {"kind": "name", "name": "Amazon RECEIPT 2025.pdf", "found": "RECEIPT"}
    assert match_rule(rule("name", "invoice"), Path("notes.txt"), None) is None


def test_extension_rule() -> None:
    assert match_rule(rule("extension", "ipynb, py"), Path("kmeans.IPYNB"), None)
    assert match_rule(rule("extension", ".ipynb"), Path("kmeans.ipynb.txt"), None) is None


def test_text_rule_looks_at_text_title_and_metadata() -> None:
    text_rule = rule("text", "syllabus")
    assert match_rule(text_rule, Path("a.pdf"), extraction("Course SYLLABUS 2025"))
    assert match_rule(text_rule, Path("a.pdf"), extraction(title="ML Syllabus"))
    assert match_rule(text_rule, Path("a.pdf"), extraction(subject="the syllabus"))
    assert match_rule(text_rule, Path("a.pdf"), extraction("lecture notes")) is None
    assert match_rule(text_rule, Path("a.pdf"), None) is None


def test_text_rule_matches_full_width_text() -> None:
    assert match_rule(rule("text", "ABC"), Path("a.txt"), extraction("ＡＢＣ商事 請求書"))


def test_first_enabled_rule_in_order_wins() -> None:
    rules = [
        rule("name", "report", "Off", id=1, enabled=False),
        rule("extension", ".pdf", "First", id=2),
        rule("name", "report", "Second", id=3),
    ]
    found = first_match(rules, Path("report.pdf"), None)
    assert found.rule.target_folder == "First"
    assert first_match(rules, Path("report.docx"), None).rule.target_folder == "Second"
    assert first_match(rules, Path("notes.txt"), None) is None


def test_conditions_read_well() -> None:
    from autoshelf.wording import condition

    assert condition({"kind": "name", "pattern": "invoice|receipt"}) == (
        "file name matches /invoice|receipt/"
    )
    assert condition({"kind": "extension", "pattern": "pdf .docx"}) == "extension is .docx, .pdf"
    assert condition({"kind": "text", "pattern": "Syllabus"}) == 'text contains "Syllabus"'


# --- storage ---------------------------------------------------------------------------


def test_rules_are_listed_in_priority_order(conn) -> None:
    first = repository.insert_rule(
        conn, name="A", kind="extension", pattern=".pdf", target_folder="X"
    )
    second = repository.insert_rule(
        conn, name="B", kind="extension", pattern=".md", target_folder="Y", enabled=False
    )
    assert [r.name for r in repository.list_rules(conn)] == ["A", "B"]
    assert [r.name for r in repository.list_rules(conn, enabled_only=True)] == ["A"]

    repository.reorder_rules(conn, [second, first])

    assert [r.name for r in repository.list_rules(conn)] == ["B", "A"]


# --- in the pipeline -----------------------------------------------------------------------


@pytest.fixture
def shelf(conn, root: Path) -> Path:
    make(root / "Finance" / "invoice 2024.txt", "invoice total tax due bank transfer")
    make(root / "Recipes" / "cake.txt", "recipe cooking baking oven cake")
    repository.set_folder_description(conn, "Finance", "invoice receipt tax bank")
    repository.set_folder_description(conn, "Recipes", "recipe cooking baking oven")
    return root


@pytest.fixture
def smart(conn, shelf: Path) -> Pipeline:
    library = LibraryIndex(conn, shelf, FakeEmbedder())
    return Pipeline(conn, shelf, library=library, threshold=0.3, margin=0.1)


def test_rule_overrides_the_classifier(conn, shelf: Path, smart: Pipeline) -> None:
    repository.insert_rule(
        conn, name="Scans", kind="name", pattern=r"^scan_\d+", target_folder="Recipes"
    )
    # The text says Finance, but the rule says Recipes.
    src = make(shelf / "scan_0034.txt", "invoice total tax due bank")

    outcome = smart.process(src)

    assert outcome.kind is OutcomeKind.MOVED
    assert outcome.dst == shelf / "Recipes" / "scan_0034.txt"
    entry = repository.get_history(conn, outcome.history_id)
    assert entry.reason == "Rule: Scans"
    assert entry.confidence is None
    assert entry.details["rule"]["name"] == "Scans"
    assert entry.details["rule"]["conditions"] == [{"kind": "name", "pattern": r"^scan_\d+"}]
    assert entry.details["reason"]["code"] == "rule"
    assert "explanation" not in entry.details  # data, not words
    assert entry.details["candidates"][0]["folder"] == "Finance"  # what the AI thought
    assert repository.get_file_by_path(conn, outcome.dst).suggestion == "Recipes"


def test_disabled_rule_is_skipped(conn, shelf: Path, smart: Pipeline) -> None:
    repository.insert_rule(
        conn, name="Off", kind="extension", pattern=".txt", target_folder="Recipes", enabled=False
    )
    outcome = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    assert outcome.dst.parent == shelf / "Finance"


def test_rule_whose_folder_is_gone_asks_instead(conn, shelf: Path, smart: Pipeline) -> None:
    repository.insert_rule(conn, name="Taxes", kind="name", pattern="tax", target_folder="Taxes")
    outcome = smart.process(make(shelf / "tax 2025.txt", "invoice total tax due"))
    assert outcome.dst == shelf / "_Inbox" / "tax 2025.txt"
    assert "rule 'Taxes' matched, but its folder 'Taxes' doesn't exist" in outcome.message
    assert not (shelf / "Taxes").exists()  # folders are never created


def test_rule_applies_to_files_waiting_in_the_inbox(conn, shelf: Path, smart: Pipeline) -> None:
    repository.insert_rule(
        conn, name="Notes", kind="text", pattern="buy milk", target_folder="Recipes"
    )
    src = make(shelf / "_Inbox" / "notes.txt", "call mom, buy milk")
    outcome = smart.process(src)
    assert outcome.dst == shelf / "Recipes" / "notes.txt"


def test_rules_work_without_a_language_model(conn, shelf: Path) -> None:
    repository.insert_rule(
        conn, name="PDFs", kind="extension", pattern=".pdf", target_folder="Finance"
    )
    outcome = Pipeline(conn, shelf).process(make(shelf / "anything.pdf"))
    assert outcome.dst == shelf / "Finance" / "anything.pdf"
    assert outcome.details["rule"]["name"] == "PDFs"


def test_dry_run_reports_the_rule(conn, shelf: Path) -> None:
    repository.insert_rule(
        conn, name="PDFs", kind="extension", pattern=".pdf", target_folder="Finance"
    )
    src = make(shelf / "anything.pdf")
    outcome = Pipeline(conn, shelf, dry_run=True).process(src)
    assert outcome.kind is OutcomeKind.WOULD_MOVE
    assert outcome.dst == shelf / "Finance" / "anything.pdf"
    assert "rule 'PDFs' matched" in outcome.message
    assert src.exists()


# --- the app and the command line ------------------------------------------------------------


@pytest.fixture
def service(paths: AppPaths, shelf: Path):
    app = AppService(paths, loader=lambda model, cache_dir: FakeEmbedder())
    app.set_root(shelf)
    yield app
    app.release()


def test_app_adds_validates_reorders_and_tests_rules(service: AppService, shelf: Path) -> None:
    first = service.add_rule("Invoices", "name", "invoice|receipt", "finance")
    assert first.target_folder == "Finance"  # stored with the folder's real name
    with pytest.raises(AssignError, match="never creates folders"):
        service.add_rule("Taxes", "name", "tax", "Taxes")
    with pytest.raises(RuleError):
        service.add_rule("Broken", "name", "(", "Finance")
    second = service.add_rule("Notebooks", "extension", ".ipynb", "Recipes")

    service.move_rule(second.id, -1)
    assert [rule.name for rule in service.rules()] == ["Notebooks", "Invoices"]
    service.set_rule_enabled(second.id, False)
    assert not service.rules()[0].enabled

    sample = make(shelf.parent / "receipt.txt", "anything")
    result = service.test_rules(sample)
    assert result.rule.name == "Invoices"
    assert result.target == "Finance"
    assert service.test_rules(make(shelf.parent / "x.ipynb")).rule is None  # switched off

    service.update_rule(replace(service.rules()[1], pattern="bill"))
    assert service.test_rules(sample).rule is None
    service.delete_rule(first.id)
    assert [rule.name for rule in service.rules()] == ["Notebooks"]


def test_cli_rules(service: AppService, shelf: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules", "add", "Invoices", "--name-matches", "invoice", "--to", "finance"]) == 0
    assert main(["rules", "add", "Notebooks", "--extension", "ipynb", "--to", "Recipes"]) == 0
    assert main(["rules", "add", "Syllabus", "--text-contains", "Syllabus", "--to", "X"]) == 1
    assert "never creates folders" in capsys.readouterr().err

    assert main(["rules", "down", "1"]) == 0
    assert main(["rules", "disable", "2"]) == 0
    output = capsys.readouterr().out
    assert "1. #2 Notebooks: extension is .ipynb -> Recipes  (off)" in output
    assert "2. #1 Invoices: file name matches /invoice/ -> Finance" in output

    sample = make(shelf.parent / "invoice.txt")
    assert main(["rules", "test", str(sample)]) == 0
    assert "Rule 'Invoices' matches" in capsys.readouterr().out

    assert main(["rules", "remove", "1"]) == 0
    assert "Removed rule #1 'Invoices'" in capsys.readouterr().out
    assert main(["rules"]) == 0
    assert "Invoices" not in capsys.readouterr().out
    assert main(["rules", "enable", "9"]) == 1
