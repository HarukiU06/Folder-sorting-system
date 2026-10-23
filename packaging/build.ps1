# Build the Windows app with PyInstaller into dist\AutoShelf\ (one folder).
#
# From the repository root:
#   .venv\Scripts\python.exe -m pip install -e ".[build]"
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#
# Then run dist\AutoShelf\AutoShelf.exe (the tray app) or dist\AutoShelf\autoshelf-cli.exe.
# The language model is not bundled: it is downloaded on first start, as when running from
# source. Copy the whole dist\AutoShelf folder to install it anywhere.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"

& $python (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $root "build\autoshelf.ico")
if ($LASTEXITCODE -ne 0) { throw "Couldn't make the icon" }

& $python -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller `
    (Join-Path $PSScriptRoot "autoshelf.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$out = Join-Path $root "dist\AutoShelf"
# The AGPL's text goes with every copy of the app (and so into the installer and the ZIP).
Copy-Item (Join-Path $root "LICENSE") (Join-Path $out "LICENSE.txt")
Write-Host "Built $out"
& (Join-Path $out "autoshelf-cli.exe") --version
