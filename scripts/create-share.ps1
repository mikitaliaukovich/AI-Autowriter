<#
.SYNOPSIS
    Publishes the add-in folder as the SMB share Word's trusted catalog points at.

.DESCRIPTION
    Word loads sideloaded add-ins from a "Trusted Add-in Catalog", which must be a UNC
    path -- a plain local path will not do. This machine already has the catalog
    registered as \\<COMPUTERNAME>\addin, so the share has to exist and point at
    .\addin, which holds manifest.xml.

    Requires elevation: creating an SMB share is an administrative operation.

.EXAMPLE
    Right-click PowerShell -> Run as administrator, then:
    powershell -ExecutionPolicy Bypass -File scripts\create-share.ps1
#>
[CmdletBinding()]
param(
    [string]$ShareName = 'addin',
    [string]$Path = (Join-Path (Split-Path -Parent $PSScriptRoot) 'addin')
)

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'This script must run elevated (New-SmbShare requires administrator rights).' -ForegroundColor Yellow
    Write-Host 'Re-launching with a UAC prompt...' -ForegroundColor Yellow
    $arguments = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$PSCommandPath`"",
        '-ShareName', "`"$ShareName`"",
        '-Path', "`"$Path`""
    )
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $arguments
    return
}

if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    Write-Host "Created $Path"
}

$existing = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Path -eq $Path) {
        Write-Host "Share '$ShareName' already points at $Path." -ForegroundColor Green
    }
    else {
        Write-Host "Share '$ShareName' points at $($existing.Path); repointing to $Path." -ForegroundColor Yellow
        Remove-SmbShare -Name $ShareName -Force
        New-SmbShare -Name $ShareName -Path $Path -FullAccess $env:USERNAME | Out-Null
    }
}
else {
    New-SmbShare -Name $ShareName -Path $Path -FullAccess $env:USERNAME | Out-Null
    Write-Host "Created share '$ShareName' -> $Path" -ForegroundColor Green
}

$unc = "\\$env:COMPUTERNAME\$ShareName"
if (Test-Path -LiteralPath $unc) {
    Write-Host "Verified: $unc is reachable." -ForegroundColor Green
    Get-ChildItem -LiteralPath $unc | Select-Object Name, Length | Format-Table -AutoSize
}
else {
    Write-Warning "Share created but $unc is not reachable. Check that the 'Server' (LanmanServer) service is running."
}

# Confirm Word's registered catalog matches the share we just created.
$catalogs = 'HKCU:\Software\Microsoft\Office\16.0\WEF\TrustedCatalogs'
if (Test-Path $catalogs) {
    $urls = Get-ChildItem $catalogs | ForEach-Object { (Get-ItemProperty $_.PSPath).Url }
    if ($urls -contains $unc) {
        Write-Host "Word's trusted catalog already lists $unc." -ForegroundColor Green
    }
    else {
        Write-Warning "Word's trusted catalogs are: $($urls -join ', ')"
        Write-Warning "Expected $unc. Add it in Word: File > Options > Trust Center > Trust Center Settings > Trusted Add-in Catalogs."
    }
}

Write-Host ''
Write-Host 'Next: start the service (scripts\run.ps1), then in Word open' -ForegroundColor Cyan
Write-Host '  Home > Add-ins > More Add-ins > SHARED FOLDER > AI Autowriter' -ForegroundColor Cyan
if (-not $env:WT_SESSION) { Read-Host 'Press Enter to close' | Out-Null }
