<#
.SYNOPSIS
    Stop the service and clean up anything it left behind.

.DESCRIPTION
    Use this if the port is reported busy, the global hotkey stops working, or the
    microphone stays claimed after a bad exit. A single service is two OS processes --
    the venv's python.exe is a launcher that runs the real interpreter as a child -- so
    a half-killed run can leave the interpreter holding the port, the hotkey and an
    ffmpeg capture process.
#>
[CmdletBinding()]
param([int]$Port = 3000)

$stopped = 0

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*service.main*' } |
    ForEach-Object {
        Write-Host "Stopping service process $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

Get-CimInstance Win32_Process -Filter "Name='ffmpeg.exe'" |
    Where-Object { $_.CommandLine -like '*dshow*' } |
    ForEach-Object {
        Write-Host "Stopping orphaned capture process $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped++
    }

Start-Sleep -Milliseconds 600

$listening = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
if ($listening.Count -gt 0) {
    Write-Warning "Port $Port is still held by PID $($listening[0].OwningProcess) - not one of ours."
}
else {
    Write-Host "Port $Port is free." -ForegroundColor Green
}

if ($stopped -eq 0) { Write-Host "Nothing was running." }
else { Write-Host "Stopped $stopped process(es)." -ForegroundColor Green }
