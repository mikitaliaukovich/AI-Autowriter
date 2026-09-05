<#
.SYNOPSIS
    Starts the AI Autowriter service.

.DESCRIPTION
    Serves the task pane and the WebSocket API on https://localhost:3000, captures the
    microphone, and drives Whisper and Ollama. Leave it running while you write.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    throw "Virtual environment missing. Run scripts\setup.ps1 first."
}

Push-Location $root
try {
    & $python -m service.main
}
finally {
    Pop-Location
}
