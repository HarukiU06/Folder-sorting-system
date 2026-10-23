"""The installer (packaging/autoshelf.iss) removes every key AutoShelf may write."""

import re
from pathlib import Path

from autoshelf import explorer, startup

ISS = Path(__file__).resolve().parent.parent / "packaging" / "autoshelf.iss"


def registry_lines() -> list[dict[str, str]]:
    text = ISS.read_text(encoding="utf-8")
    section = text.split("[Registry]", 1)[1].split("\n[", 1)[0]
    entries = []
    for line in section.splitlines():
        if line.startswith("Root:"):
            fields = dict(part.strip().split(": ", 1) for part in line.split("; ") if ": " in part)
            entries.append({key: value.strip('"') for key, value in fields.items()})
    return entries


def test_uninstall_removes_every_key_autoshelf_writes() -> None:
    removed = {
        entry["Subkey"].replace("{#AppId}", explorer.APP_ID)
        for entry in registry_lines()
        if "uninsdeletekey" in entry.get("Flags", "")
    }
    written = {explorer.MENU_KEY, explorer.LINK_KEY, explorer.APP_ID_KEY}
    written |= {explorer.convert_key(suffix) for suffix in explorer.CONVERT_SUFFIXES}
    assert removed == written
    run_values = [
        entry
        for entry in registry_lines()
        if entry["Subkey"] == startup.RUN_KEY and "uninsdeletevalue" in entry.get("Flags", "")
    ]
    assert {entry["ValueName"] for entry in run_values} == {startup.VALUE_NAME}


def test_the_installer_uses_the_apps_identity_and_options() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert re.search(rf'#define AppId "{re.escape(explorer.APP_ID)}"', text)
    assert 'AppUserModelID: "{#AppId}"' in text
    assert r'""{app}\AutoShelf.exe"" --url ""%1""' in text  # as explorer.add_link writes it
    assert f"{startup.BACKGROUND_FLAG}" in text
    assert rf"\{explorer.ICON_FILE}" in text
