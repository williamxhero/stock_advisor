[CmdletBinding()]
param(
    [string]$InstallRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CompanionHome = (Join-Path $env:LOCALAPPDATA 'AITradingCompanion'),
    [string]$ExpectedRevision
)

$ErrorActionPreference = 'Stop'
foreach ($required in @(
    'AITradingCompanion.exe',
    'resources\schedules\tasks.json',
    'resources\contracts\companion-client-event-v1.schema.json',
    'resources\contracts\companion-published-message-v2.schema.json',
    'runtime\ai_trading_companion\__main__.py',
    'runtime\ai_trading_companion\message_presentation.py',
    'build-info.json',
    'scripts\run_companion_service.ps1'
)) {
    $path = Join-Path $InstallRoot $required
    if (-not (Test-Path -LiteralPath $path)) { throw "Required installation artifact is missing: $path" }
}
$buildInfo = Get-Content -LiteralPath (Join-Path $InstallRoot 'build-info.json') -Raw | ConvertFrom-Json
if ($buildInfo.dirty -ne $false) { throw 'Installed build-info must record dirty=false.' }
if ([string]$buildInfo.source_revision -notmatch '^[0-9a-f]{40}$') { throw 'Installed build-info must contain the full Git SHA.' }
if ($ExpectedRevision -and $buildInfo.source_revision -ne $ExpectedRevision) {
    throw "Installed revision $($buildInfo.source_revision) does not match expected revision $ExpectedRevision."
}
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $InstallRoot
$env:AI_TRADING_COMPANION_HOME = $CompanionHome
$env:PYTHONPATH = "$InstallRoot\runtime"
$runtimePython = Join-Path $CompanionHome 'runtime\python\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
& $python -m ai_trading_companion status | Out-Null
Write-Output "AITradingCompanion installation verified: $InstallRoot"
