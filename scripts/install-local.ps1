[CmdletBinding()]
param(
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Runtime = 'win-x64',
    [switch]$EnableStartup,
    [string]$CompanionHome = (Join-Path $env:LOCALAPPDATA 'AITradingCompanion'),
    [switch]$SkipLegacyMigration
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root 'scripts\publish.ps1') -Runtime $Runtime -NoRestore
$source = Join-Path $root "dist\$Runtime\AITradingCompanion"
$companionHome = [System.IO.Path]::GetFullPath($CompanionHome)
$app = Join-Path $companionHome 'app'
$staging = "$app.staging-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $companionHome -Force | Out-Null
$pythonHome = Join-Path $companionHome 'runtime\python'
$python = Join-Path $pythonHome 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -m venv $pythonHome
}
& $python -m pip install --disable-pip-version-check --upgrade -r (Join-Path $source 'scripts\requirements-runtime.txt')
Copy-Item -LiteralPath $source -Destination $staging -Recurse -Force
if (Test-Path -LiteralPath $app) {
    $backup = Join-Path $companionHome ("app-backup-" + (Get-Date -Format 'yyyyMMddHHmmss'))
    Move-Item -LiteralPath $app -Destination $backup
}
Move-Item -LiteralPath $staging -Destination $app
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $app
$env:AI_TRADING_COMPANION_HOME = $companionHome
$env:PYTHONPATH = "$app\runtime"
if (-not $SkipLegacyMigration) {
    & $python -m ai_trading_companion migrate-legacy --legacy-root $root | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Legacy data migration failed; the previous data and installation remain unchanged.' }
}
& (Join-Path $app 'scripts\verify-install.ps1') -InstallRoot $app -CompanionHome $companionHome

if ($EnableStartup) {
    $run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-Item -Path $run -Force | Out-Null
    Set-ItemProperty -Path $run -Name 'AITradingCompanion' -Value ('"' + (Join-Path $app 'AITradingCompanion.exe') + '"')
    $old = Get-ItemProperty -Path $run -Name 'AIDecisionCenter' -ErrorAction SilentlyContinue
    if ($old -and $old.AIDecisionCenter -match 'windows-ai-decision-center') {
        Remove-ItemProperty -Path $run -Name 'AIDecisionCenter'
    }
}

Write-Output "AI Trading Companion installed: $app"
