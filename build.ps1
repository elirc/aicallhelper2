# One-command release build: checks -> frontend -> PyInstaller.
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1 [-SkipChecks]
param([switch]$SkipChecks)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($name, $script) {
    Write-Host "==> $name" -ForegroundColor Cyan
    & $script
    if ($LASTEXITCODE -ne 0) { throw "$name failed (exit $LASTEXITCODE)" }
}

if (-not $SkipChecks) {
    Step "ruff"        { .venv\Scripts\python -m ruff check app_core app.py tests }
    Step "mypy"        { .venv\Scripts\python -m mypy }
    Step "pytest"      { .venv\Scripts\python -m pytest tests -q }
    Step "typecheck"   { Push-Location frontend; npx tsc --noEmit; Pop-Location }
    Step "vitest"      { Push-Location frontend; npx vitest run; Pop-Location }
}

Step "vite build"      { Push-Location frontend; npm run build; Pop-Location }
Step "pyinstaller"     { .venv\Scripts\pyinstaller aica.spec --noconfirm }

Write-Host "==> Done: dist\AICallAssistant\AICallAssistant.exe" -ForegroundColor Green
Write-Host "    (optional) installer: iscc installer.iss"
