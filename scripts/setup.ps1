<#
.SYNOPSIS
    One-time setup: Python environment, HTTPS certificates, and models.

.DESCRIPTION
    Everything here is idempotent -- re-running it is safe and only does what is
    missing. It does NOT create the SMB share, which needs elevation; run
    scripts\create-share.ps1 for that.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipModels
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv'
$python = Join-Path $venv 'Scripts\python.exe'

function Write-Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "   $text" -ForegroundColor Yellow }

# --- Python 3.12 -----------------------------------------------------------------
# CTranslate2 and onnxruntime publish no wheels for 3.14, which is the only interpreter
# on some machines, so the service gets its own 3.12 environment.

Write-Step 'Python environment'
if (-not (Test-Path $python)) {
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    $base = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $base) {
        try { $base = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch {}
    }
    if (-not $base) {
        Write-Warn 'Python 3.12 not found. Installing via winget...'
        winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements --disable-interactivity
        $base = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
    }
    if (-not (Test-Path $base)) { throw "Could not locate Python 3.12 at $base" }
    & $base -m venv $venv
    Write-Ok "Created virtual environment at $venv"
}
& $python -m pip install --upgrade pip --quiet
& $python -m pip install -r (Join-Path $root 'requirements-dev.txt') --quiet
Write-Ok ((& $python --version) + ' with dependencies installed')

# --- HTTPS certificates ----------------------------------------------------------
# Word refuses to load an add-in that is not served over trusted HTTPS.

Write-Step 'HTTPS certificate for localhost'
$crt = Join-Path $env:USERPROFILE '.office-addin-dev-certs\localhost.crt'
if (Test-Path $crt) {
    Write-Ok 'Developer certificate already installed.'
}
else {
    npx --yes office-addin-dev-certs install --days 365
    if (-not (Test-Path $crt)) { throw 'Certificate installation failed.' }
    Write-Ok 'Installed and trusted.'
}

# --- Models ----------------------------------------------------------------------

if (-not $SkipModels) {
    Write-Step 'Speech model (Silero VAD + Whisper)'
    & $python -c @"
import pathlib, sys
sys.path.insert(0, r'$root')
from service.audio.vad import ensure_model
print('   VAD:', ensure_model(pathlib.Path(r'$root') / 'models'))
"@

    Write-Step 'Language model'
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if (-not $ollama) {
        Write-Warn 'Ollama is not on PATH. Install it from https://ollama.com and re-run.'
    }
    else {
        $model = (Select-String -Path (Join-Path $root 'config.toml') -Pattern '^\s*model\s*=\s*"([^"]+)"' |
            Select-Object -Skip 1 -First 1).Matches.Groups[1].Value
        if (-not $model) { $model = 'qwen3:8b' }
        $have = (& ollama list) -join "`n"
        if ($have -match [regex]::Escape($model)) {
            Write-Ok "$model already pulled."
        }
        else {
            Write-Warn "Pulling $model ..."
            & ollama pull $model
        }
    }
}

# --- Word add-in catalog ---------------------------------------------------------

Write-Step 'Word add-in catalog'
$unc = "\\$env:COMPUTERNAME\addin"
if (Test-Path -LiteralPath $unc) {
    Write-Ok "$unc is reachable."
}
else {
    Write-Warn "$unc is not reachable yet."
    Write-Warn 'Run this once, elevated:  powershell -ExecutionPolicy Bypass -File scripts\create-share.ps1'
}

Write-Host "`nSetup complete. Start the service with:  .\scripts\run.ps1" -ForegroundColor Cyan
