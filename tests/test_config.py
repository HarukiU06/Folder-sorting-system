from pathlib import Path

import pytest

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
