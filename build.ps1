# One-command release build: metadata -> checks -> frontend -> PyInstaller -> installer.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1 [-Bootstrap] [-SkipChecks] [-SkipInstaller]
#   -Bootstrap      create .venv if missing and install the pinned toolchain
#                   (constraints.txt + pyproject extras) and `npm ci` first
#   -SkipChecks     skip ruff/mypy/pytest/typecheck/vitest (metadata check always runs)
#   -SkipInstaller  do not run the Inno Setup compiler even if it is installed
param([switch]$Bootstrap, [switch]$SkipChecks, [switch]$SkipInstaller)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".venv\Scripts\python.exe"

function Step($name, $script) {
    Write-Host "==> $name" -ForegroundColor Cyan
    & $script
    if ($LASTEXITCODE -ne 0) { throw "$name failed (exit $LASTEXITCODE)" }
}

if ($Bootstrap) {
    if (-not (Test-Path $py)) { Step "venv" { python -m venv .venv } }
    Step "pip install (pinned)" { & $py -m pip install -c constraints.txt -e ".[dev,build]" }
    Step "npm ci" { npm --prefix frontend ci }
}
if (-not (Test-Path $py)) { throw "No .venv - run with -Bootstrap first (see fabledocs/SETUP-AND-DEPLOY.md)" }

# Version drift or an unpinned dependency fails the build before anything is produced.
Step "release metadata"   { & $py tools\release_meta.py check }
$version = (& $py tools\release_meta.py version).Trim()

if (-not $SkipChecks) {
    Step "ruff"        { & $py -m ruff check app_core app.py tests tools }
    Step "mypy"        { & $py -m mypy }
    Step "pytest"      { & $py -m pytest tests -q }
    Step "typecheck"   { npm --prefix frontend run typecheck }
    Step "vitest"      { npm --prefix frontend test }
}

Step "vite build"      { npm --prefix frontend run build }
Step "pyinstaller"     { & $py -m PyInstaller aica.spec --noconfirm }

$exe = "dist\AICallAssistant\AICallAssistant.exe"
Write-Host "==> Built $exe  v$version" -ForegroundColor Green
Get-Content "dist\AICallAssistant\_internal\build_info.json" -ErrorAction SilentlyContinue |
    Select-Object -First 5 | ForEach-Object { Write-Host "    $_" }

if (-not $SkipInstaller) {
    $iscc = @(
        (Get-Command iscc -ErrorAction SilentlyContinue).Source,
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if ($iscc) {
        Step "installer" { & $iscc "/DAppVersion=$version" installer.iss }
        Write-Host "==> Built dist\AICallAssistant-Setup-$version.exe" -ForegroundColor Green
    } else {
        Write-Host "    Inno Setup 6 not found; skipped the installer (winget install JRSoftware.InnoSetup)" -ForegroundColor Yellow
    }
}
