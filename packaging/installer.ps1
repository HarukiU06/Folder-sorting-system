# Build the installer (dist\AutoShelf-<version>-setup.exe) from dist\AutoShelf with Inno Setup.
#
# From the repository root, after packaging\build.ps1:
#   powershell -ExecutionPolicy Bypass -File packaging\installer.ps1
#
# Needs Inno Setup 6 (winget install JRSoftware.InnoSetup, or choco install innosetup).
# The version comes from the built app (autoshelf-cli.exe --version).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$app = Join-Path $root "dist\AutoShelf"
if (-not (Test-Path (Join-Path $app "AutoShelf.exe"))) { throw "Build the app first: packaging\build.ps1" }

$version = (& (Join-Path $app "autoshelf-cli.exe") --version).Trim().Split(" ")[-1]
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Couldn't read the app's version" }

$iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }

& $iscc "/DAppVersion=$version" (Join-Path $PSScriptRoot "autoshelf.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Built $(Join-Path $root "dist\AutoShelf-$version-setup.exe")"
