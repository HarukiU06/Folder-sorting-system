# PyInstaller spec for AutoShelf: one folder (dist/AutoShelf/) holding
#   AutoShelf.exe      the tray app, no console window
#   autoshelf-cli.exe  the command line
# Build it with packaging/build.ps1. Paths are relative to this file, so the spec works
# from any checkout. A one-folder build starts much faster than a one-file build (which
# would unpack PyTorch on every start) and keeps the Qt (LGPL) libraries replaceable.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

HERE = Path(SPECPATH)  # noqa: F821 (provided by PyInstaller)
ROOT = HERE.parent
ICON = ROOT / "build" / "autoshelf.ico"

# Extractors are imported by name when a file type is first seen, so name them all.
hidden_imports = collect_submodules("autoshelf")
# Notifications with buttons: winrt is a namespace package of compiled modules.
hidden_imports += collect_submodules("winrt") + collect_submodules("windows_toasts")

# transformers checks the versions of its dependencies through their package metadata.
datas = []
for package in (
    "transformers",
    "sentence-transformers",
    "tokenizers",
    "huggingface-hub",
    "safetensors",
    "torch",
    "tqdm",
    "regex",
    "requests",
    "filelock",
    "numpy",
    "packaging",
    "pyyaml",
    "keyring",
):
    try:
        datas += copy_metadata(package)
    except Exception:  # noqa: BLE001 (a missing optional package is fine)
        pass

# The icon as a file, for Windows' notifications and the autoshelf: link (explorer.py).
if ICON.exists():
    datas.append((str(ICON), "."))

excludes = ["tkinter", "matplotlib", "IPython", "pytest", "pytestqt", "ruff", "PyInstaller"]


def analysis(script: str) -> Analysis:  # noqa: F821
    return Analysis(  # noqa: F821
        [str(HERE / script)],
        pathex=[str(ROOT)],
        hiddenimports=hidden_imports,
        datas=datas,
        excludes=excludes,
        noarchive=False,
    )


app = analysis("app_entry.py")
cli = analysis("cli_entry.py")
icon = str(ICON) if ICON.exists() else None

app_exe = EXE(  # noqa: F821
    PYZ(app.pure),  # noqa: F821
    app.scripts,
    [],
    exclude_binaries=True,
    name="AutoShelf",
    console=False,
    icon=icon,
    upx=False,
)
cli_exe = EXE(  # noqa: F821
    PYZ(cli.pure),  # noqa: F821
    cli.scripts,
    [],
    exclude_binaries=True,
    name="autoshelf-cli",
    console=True,
    icon=icon,
    upx=False,
)
COLLECT(  # noqa: F821
    app_exe,
    cli_exe,
    app.binaries,
    app.datas,
    cli.binaries,
    cli.datas,
    name="AutoShelf",
    upx=False,
)
