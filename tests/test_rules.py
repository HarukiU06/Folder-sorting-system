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
    conditions = ({"kind": kind, "pattern": pattern},)
    return replace(RuleRecord(1, "Test rule", 1, True, conditions, target), **changes)


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
        check_rule("My rule", [{"kind": kind, "pattern": pattern}], target="Finance")


def test_rule_needs_a_name() -> None:
    with pytest.raises(RuleError, match="name"):
        check_rule(" ", [{"kind": "extension", "pattern": ".pdf"}], target="Finance")


def test_extensions_are_normalised() -> None:
    assert extensions(".PDF, docx;*.md  txt") == {".pdf", ".docx", ".md", ".txt"}


def test_name_rule_searches_the_file_name_in_any_case() -> None:
    found = match_rule(rule("name", "invoice|receipt"), Path("Amazon RECEIPT 2025.pdf"), None)
    assert found == ({"kind": "name", "name": "Amazon RECEIPT 2025.pdf", "found": "RECEIPT"},)
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

    bill = ({"kind": "name", "pattern": "bill"},)
    service.update_rule(replace(service.rules()[1], conditions=bill))
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
    assert "1. #2 Notebooks: extension is .ipynb -> move to Recipes  (off)" in output
    assert "2. #1 Invoices: file name matches /invoice/ -> move to Finance" in output

    sample = make(shelf.parent / "invoice.txt")
    assert main(["rules", "test", str(sample)]) == 0
    assert "Rule 'Invoices' matches" in capsys.readouterr().out

    assert main(["rules", "remove", "1"]) == 0
    assert "Removed rule #1 'Invoices'" in capsys.readouterr().out
    assert main(["rules"]) == 0
    assert "Invoices" not in capsys.readouterr().out
    assert main(["rules", "enable", "9"]) == 1


# --- Phase 12: richer conditions -------------------------------------------------------------


def test_size_date_and_any_or_all_conditions(tmp_path: Path) -> None:
    import os

    from autoshelf.actions.rules import check_condition, parse_size

    big = make(tmp_path / "scan.pdf", "x" * 3000)
    os.utime(big, (1_600_000_000, 1_600_000_000))  # 2020-09-13
    assert parse_size("2KB") == 2048 and parse_size("1.5 MB") == 1572864 and parse_size("10") == 10
    with pytest.raises(RuleError, match="isn't a size"):
        parse_size("lots")
    assert check_condition({"kind": "size", "min": "2KB", "max": None}) == {
        "kind": "size",
        "min": 2048,
        "max": None,
    }
    with pytest.raises(RuleError, match="larger than"):
        check_condition({"kind": "size", "min": "2MB", "max": "1MB"})
    with pytest.raises(RuleError, match="isn't a date"):
        check_condition({"kind": "date", "from": "yesterday"})

    big_old = (
        {"kind": "size", "min": 2048, "max": None},
        {"kind": "date", "from": None, "to": "2020-12-31"},
    )
    both = replace(rule("name", "x"), conditions=big_old)
    assert match_rule(both, big, None) == (
        {"kind": "size", "found": 3000},
        {"kind": "date", "found": "2020-09-13"},
    )
    small = make(tmp_path / "small.pdf", "x")
    os.utime(small, (1_600_000_000, 1_600_000_000))
    assert match_rule(both, small, None) is None  # all must hold
    either = replace(both, match_any=True)
    assert match_rule(either, small, None) == ({"kind": "date", "found": "2020-09-13"},)


def test_rename_templates(tmp_path: Path) -> None:
    import os

    from autoshelf.actions.rules import check_template, new_name

    path = make(tmp_path / "scan_0034.pdf", "x")
    os.utime(path, (1_741_000_000, 1_741_000_000))
    day = __import__("datetime").datetime.fromtimestamp(1_741_000_000).strftime("%Y-%m-%d")
    renaming = replace(rule("name", "scan"), rename_template="{date} {title}")
    assert new_name(renaming, path, extraction(title="Invoice: March?")) == (
        f"{day} Invoice March.pdf"  # made safe for Windows
    )
    assert new_name(renaming, path, None) == f"{day} scan_0034.pdf"
    same = replace(renaming, rename_template="{name}")
    assert new_name(same, path, None) is None  # it already has that name
    with pytest.raises(RuleError, match="Unknown field"):
        check_template("{author} {title}")
    with pytest.raises(RuleError, match="needs something to do"):
        check_rule("Nothing", [{"kind": "extension", "pattern": ".pdf"}])


def test_a_rule_moves_renames_and_tags_and_undo_puts_it_back(
    conn, shelf: Path, smart: Pipeline
) -> None:
    from autoshelf.actions.undo import undo

    repository.insert_rule(
        conn,
        name="Scans",
        conditions=[{"kind": "name", "pattern": r"^scan_\d+"}],
        target_folder="Finance",
        tags=["Scanned"],
        rename_template="Scanned {name}",
    )
    src = make(shelf / "scan_0034.txt", "something")

    outcome = smart.process(src)

    assert outcome.dst == shelf / "Finance" / "Scanned scan_0034.txt"
    entry = repository.get_history(conn, outcome.history_id)
    assert (entry.action, entry.reason) == ("move", "Rule: Scans")
    assert entry.details["renamed_from"] == "scan_0034.txt"
    assert entry.details["rule"]["rename"] == "Scanned {name}"
    record = repository.get_file_by_path(conn, outcome.dst)
    assert repository.tags_of(conn, record.id) == ["Scanned"]
    assert "renamed to 'Scanned scan_0034.txt'" in outcome.message

    undo(conn, outcome.history_id)
    assert src.exists()  # back where it was, with its old name


def test_a_rule_without_a_folder_lets_autoshelf_decide(conn, shelf: Path, smart: Pipeline) -> None:
    repository.insert_rule(
        conn,
        name="Dated",
        conditions=[{"kind": "extension", "pattern": ".txt"}],
        target_folder="",
        rename_template="{year} {name}",
    )
    outcome = smart.process(make(shelf / "invoice 2025.txt", "invoice total tax due"))
    assert outcome.dst.parent == shelf / "Finance"  # the classifier chose
    assert outcome.dst.name.endswith(" invoice 2025.txt")
    assert repository.get_history(conn, outcome.history_id).reason == "auto"

    # A file that stays waiting in the Inbox is renamed where it is.
    waiting = make(shelf / "_Inbox" / "call mom.txt", "call mom buy milk")
    outcome = smart.process(waiting)
    assert outcome.kind is OutcomeKind.WAITING
    assert outcome.dst.parent == shelf / "_Inbox"
    assert outcome.dst.name.endswith(" call mom.txt")
    renamed = repository.list_history(conn, 1)[0]
    assert (renamed.action, renamed.reason) == ("rename", "Rule: Dated")
    assert repository.get_file_by_path(conn, outcome.dst).status == "inbox"


def test_app_test_on_a_file_shows_every_action(service: AppService, shelf: Path) -> None:
    from autoshelf.wording import rule_test

    service.add_rule(
        "Invoices",
        conditions=[
            {"kind": "name", "pattern": "invoice"},
            {"kind": "extension", "pattern": "txt"},
        ],
        target="finance",
        tags=["Bills", " Bills "],
        rename_template="Bill {name}",
    )
    (saved,) = service.rules()
    assert saved.tags == ("Bills",)
    result = service.test_rules(make(shelf.parent / "invoice 9.txt", "x"))
    assert result.new_name == "Bill invoice 9.txt"
    assert rule_test(result) == (
        "Rule 'Invoices' matches ('invoice 9.txt' contains 'invoice' and the extension is "
        ".txt): it would go to Finance, be tagged Bills, be renamed to 'Bill invoice 9.txt'"
    )


def test_cli_adds_a_rich_rule(service: AppService, capsys: pytest.CaptureFixture[str]) -> None:
    command = [
        "rules",
        "add",
        "Old scans",
        "--extension",
        "pdf",
        "--size-min",
        "1MB",
        "--date-to",
        "2020-12-31",
        "--any",
        "--tag",
        "Old",
        "--rename",
        "{year} {name}",
    ]
    assert main(command) == 0
    out = capsys.readouterr().out
    assert (
        "Old scans: extension is .pdf or size is at least 1.0 MB or date is 2020-12-31 or "
        "earlier -> AutoShelf decides the folder; tag Old; rename to {year} {name}"
    ) in out
    assert main(["rules", "add", "Nothing", "--to", "Finance"]) == 1
    assert "at least one condition" in capsys.readouterr().err


def test_old_rules_keep_working_after_the_upgrade(paths) -> None:
    from autoshelf.db import schema
    from autoshelf.db.connection import connect, open_database

    conn = connect(paths.db_path)
    script = "".join((schema.V1, schema.V2, schema.V3, schema.V4, schema.V5))
    conn.executescript(f"BEGIN;{script}PRAGMA user_version = 5; COMMIT;")
    conn.execute(
        "INSERT INTO rules (name, priority, enabled, kind, pattern, target_folder)"
        " VALUES ('Invoices', 1, 1, 'name', 'invoice', 'Finance')"
    )
    conn.close()
    conn = open_database(paths.db_path)
    try:
        (old,) = repository.list_rules(conn)
        assert old.conditions == ({"kind": "name", "pattern": "invoice"},)
        assert (old.target_folder, old.match_any, old.tags, old.rename_template) == (
            "Finance",
            False,
            (),
            "",
        )
    finally:
        conn.close()


# --- Phase 12: rules suggested from your choices --------------------------------------------


def _choose(conn, path: Path, folder: str) -> None:
    """The user put ``path`` (already there) into ``folder``."""
    st = path.stat()
    file_id = repository.upsert_file(conn, path, sha256=None, size=st.st_size, mtime=st.st_mtime)
    repository.record_correction(
        conn, file_id=file_id, from_folder=None, to_folder=folder, source="inbox"
    )


def test_a_rule_is_suggested_when_choices_share_a_word(conn, shelf: Path) -> None:
    from autoshelf.features.rule_suggestions import suggest_rules

    for number in range(3):
        _choose(conn, make(shelf / "Finance" / f"Invoice_{number} ACME.txt", "x"), "Finance")
    _choose(conn, make(shelf / "Recipes" / "pancakes.txt", "x"), "Recipes")

    by_key = {item.key: item for item in suggest_rules(conn, shelf)}

    item = by_key["name:invoice->Finance"]
    assert item.condition == {"kind": "name", "pattern": "invoice"}
    assert len(item.files) == 3
    assert "name:acme->Finance" in by_key  # also shared
    assert not any(key.endswith("->Recipes") for key in by_key)  # only one file

    dismissed = suggest_rules(conn, shelf, dismissed=["name:invoice->Finance"])
    assert "name:invoice->Finance" not in {item.key for item in dismissed}
    repository.insert_rule(
        conn, name="Invoices", kind="name", pattern="invoice", target_folder="Finance"
    )
    assert "name:invoice->Finance" not in {item.key for item in suggest_rules(conn, shelf)}


def test_no_rule_is_suggested_against_the_library(conn, shelf: Path) -> None:
    from autoshelf.features.rule_suggestions import suggest_rules

    for number in range(3):
        _choose(conn, make(shelf / "Recipes" / f"notes {number}.md", "x"), "Recipes")
    for number in range(4):  # most .md files in the library are elsewhere
        make(shelf / "Finance" / f"budget {number}.md", "x")
    keys = {item.key for item in suggest_rules(conn, shelf)}
    assert "extension:.md->Recipes" not in keys
    assert "name:notes->Recipes" in keys


def test_suggestions_in_the_app_and_the_cli(
    conn, service: AppService, shelf: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for number in range(3):
        _choose(conn, make(shelf / "Finance" / f"receipt {number}.pdf", "x"), "Finance")
    assert service.rule_suggestion_for("recipes") is None
    found = service.rule_suggestion_for("finance")
    assert found.key in {"name:receipt->Finance", "extension:.pdf->Finance"}

    assert main(["rules", "suggest"]) == 0
    out = capsys.readouterr().out
    assert "You put 3 files with 'receipt' in their names into Finance" in out
    assert main(["rules", "suggest", "--add", "1"]) == 0
    assert "Added rule #" in capsys.readouterr().out
    (made,) = service.rules()
    assert made.target_folder == "Finance"
    for item in service.rule_suggestions():
        service.dismiss_rule_suggestion(item.key)
    assert service.rule_suggestions() == []
    assert main(["rules", "suggest"]) == 0
    assert "No rule to suggest" in capsys.readouterr().out
