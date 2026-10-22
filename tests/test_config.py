from pathlib import Path

import pytest
from helpers import windows_only

from autoshelf.config import (
    APP_NAME,
    DEFAULT_IGNORE_PATTERNS,
    HOME_ENV_VAR,
    ConfigError,
    Settings,
    default_home,
    get_app_paths,
    is_in_icloud,
    load_settings,
    save_settings,
    validate_root,
)


def test_default_home_uses_env_override(app_home: Path) -> None:
    assert default_home() == app_home


def test_default_home_falls_back_to_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(HOME_ENV_VAR)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert default_home() == tmp_path / APP_NAME


def test_get_app_paths_creates_folders(app_home: Path) -> None:
    paths = get_app_paths()
    assert paths.home == app_home.resolve()
    assert paths.log_dir.is_dir()
    assert paths.cache_dir.is_dir()
    assert paths.db_path.parent == paths.home


@pytest.mark.parametrize("name", ["iCloudDrive", "iCloud Drive", "ICLOUDDRIVE"])
def test_app_home_inside_icloud_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="iCloud"):
        get_app_paths(tmp_path / name / "AutoShelf")
    assert not (tmp_path / name).exists()


@windows_only  # C:\ paths only split into parts on Windows
def test_is_in_icloud() -> None:
    assert is_in_icloud(Path(r"C:\Users\me\iCloudDrive\AutoShelf"))
    assert not is_in_icloud(Path(r"C:\Users\me\Documents\cloud notes"))


def test_validate_root_returns_resolved_path(root: Path, paths) -> None:
    assert validate_root(root, paths) == root.resolve()


def test_validate_root_refuses_missing_folder(tmp_path: Path, paths) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        validate_root(tmp_path / "nope", paths)


def test_validate_root_refuses_a_file(tmp_path: Path, paths) -> None:
    file = tmp_path / "file.txt"
    file.write_text("x")
    with pytest.raises(ConfigError, match="not a folder"):
        validate_root(file, paths)


def test_validate_root_refuses_drive_root(tmp_path: Path, paths) -> None:
    drive_root = Path(tmp_path.anchor)
    with pytest.raises(ConfigError, match="whole drive"):
        validate_root(drive_root, paths)


def test_validate_root_refuses_root_containing_app_data(tmp_path: Path) -> None:
    root = tmp_path / "library"
    paths = get_app_paths(root / "app data")
    with pytest.raises(ConfigError, match="must not be inside the root"):
        validate_root(root, paths)


def test_settings_defaults_when_nothing_stored(conn) -> None:
    settings = load_settings(conn)
    assert settings == Settings()
    assert settings.ignore_patterns == list(DEFAULT_IGNORE_PATTERNS)


def test_settings_round_trip(conn, root: Path) -> None:
    settings = Settings(root=str(root), stable_seconds=5.5, ignore_patterns=["*.bak"])
    save_settings(conn, settings)
    assert load_settings(conn) == settings


def test_balanced_preset_is_the_default() -> None:
    from autoshelf.config import (
        CONFIDENCE_LEVELS,
        DEFAULT_AUTO_MARGIN,
        DEFAULT_AUTO_THRESHOLD,
        confidence_level,
    )

    balanced = confidence_level(DEFAULT_AUTO_THRESHOLD, DEFAULT_AUTO_MARGIN)
    assert CONFIDENCE_LEVELS[balanced].name == "Balanced"
    assert confidence_level(0.42, 0.11) is None
    thresholds = [level.threshold for level in CONFIDENCE_LEVELS]
    assert thresholds == sorted(thresholds, reverse=True)  # cautious -> automatic


# --- start with Windows (Phase 8) ---------------------------------------------------------


@windows_only  # the registry's Run key
def test_start_with_windows_uses_the_run_key(fake_run_key: dict) -> None:
    from autoshelf import startup

    assert not startup.is_enabled()
    startup.set_enabled(True)
    command = fake_run_key[startup.VALUE_NAME]
    assert command.endswith("-m autoshelf --background")
    assert "pythonw.exe" in command or "python" in command.casefold()
    assert startup.is_enabled()
    startup.set_enabled(False)
    assert fake_run_key == {}


def test_packaged_app_starts_the_tray_app(monkeypatch, tmp_path) -> None:
    import sys

    from autoshelf import startup

    folder = tmp_path / "AutoShelf"
    folder.mkdir()
    app = folder / "AutoShelf.exe"
    app.write_bytes(b"")
    cli = folder / "autoshelf-cli.exe"
    cli.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    for executable in (app, cli):  # changed in the app, or with the command line
        monkeypatch.setattr(sys, "executable", str(executable))
        assert startup.startup_command() == f'"{app}" --background'


@windows_only  # config --startup uses the registry's Run key
def test_cli_startup_and_background(fake_run_key: dict, monkeypatch, capsys) -> None:
    from autoshelf import cli
    from autoshelf.ui import app as ui_app

    assert cli.main(["config", "--startup", "on"]) == 0
    assert "Start with Windows: on" in capsys.readouterr().out
    assert fake_run_key
    assert cli.main(["config", "--startup", "off"]) == 0
    assert fake_run_key == {}

    calls = []
    monkeypatch.setattr(ui_app, "run_app", lambda **kwargs: calls.append(kwargs) or 0)
    assert cli.main(["--background"]) == 0
    assert calls == [{"verbose": False, "show": False}]
