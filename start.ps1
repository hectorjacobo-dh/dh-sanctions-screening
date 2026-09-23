# DH Group — Sanctions Screening Engine
# One-step start. Sets up anything missing, then launches the app.
#
#   Right-click this file -> "Run with PowerShell"
#   or:  powershell -ExecutionPolicy Bypass -File start.ps1
#
# Requires no administrator rights.

$ErrorActionPreference = 'Stop'
$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$backend = Join-Path $root 'backend'
$venvPy  = Join-Path $backend '.venv\Scripts\python.exe'

Write-Host ''
Write-Host '  DH Group - Sanctions Screening Engine' -ForegroundColor Cyan
Write-Host '  -------------------------------------' -ForegroundColor Cyan
Write-Host ''

# --- 1. Locate a real Python (not the Microsoft Store stub) ----------------
function Get-RealPython {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }

    # Fall back to PATH, but reject the Store stub, which reports 0.0.0.0.
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { return $cmd.Source }
    return $null
}

$python = Get-RealPython

if (-not $python) {
    Write-Host '  Python not found. Installing (per-user, no admin needed)...' -ForegroundColor Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host ''
        Write-Host '  ERROR: winget is unavailable, so Python cannot be installed automatically.' -ForegroundColor Red
        Write-Host '  Install Python 3.12+ from https://www.python.org/downloads/' -ForegroundColor Red
        Write-Host '  and tick "Add python.exe to PATH", then re-run this script.' -ForegroundColor Red
        Read-Host '  Press Enter to close'
        exit 1
    }
    winget install --id Python.Python.3.12 --scope user `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    $python = Get-RealPython
    if (-not $python) {
        Write-Host '  Python installed but not found. Close and reopen PowerShell, then re-run.' -ForegroundColor Red
        Read-Host '  Press Enter to close'
        exit 1
    }
}

Write-Host "  Python: $python" -ForegroundColor DarkGray

# --- 2. Create the virtualenv and install dependencies --------------------
if (-not (Test-Path $venvPy)) {
    Write-Host '  First run - creating environment (about a minute)...' -ForegroundColor Yellow
    & $python -m venv (Join-Path $backend '.venv')
    & $venvPy -m pip install --upgrade pip --quiet
    & $venvPy -m pip install -r (Join-Path $backend 'requirements.txt') --quiet
    Write-Host '  Environment ready.' -ForegroundColor Green
}

# --- 3. Launch -------------------------------------------------------------
Write-Host ''
Write-Host '  Starting. On first launch it downloads five sanctions lists' -ForegroundColor Gray
Write-Host '  (~60MB, about a minute) - the page fills in as they arrive.' -ForegroundColor Gray
Write-Host ''
Write-Host '  Open:  http://127.0.0.1:8000' -ForegroundColor Green
Write-Host '  Stop:  press Ctrl+C in this window' -ForegroundColor DarkGray
Write-Host ''
Write-Host '  This binds to 127.0.0.1 only - reachable from THIS computer' -ForegroundColor DarkYellow
Write-Host '  alone, and it has no login. Do not expose it to a network' -ForegroundColor DarkYellow
Write-Host '  without setting SINGLE_USER_MODE=false first (see README).' -ForegroundColor DarkYellow
Write-Host ''

Start-Job -ScriptBlock {
    Start-Sleep -Seconds 4
    Start-Process 'http://127.0.0.1:8000'
} | Out-Null

Push-Location $backend
try {
    & $venvPy -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} finally {
    Pop-Location
}
