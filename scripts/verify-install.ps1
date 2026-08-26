[CmdletBinding()]
param(
    [string]$InstallRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$CompanionHome = (Join-Path $env:LOCALAPPDATA 'AITradingCompanion')
)

$ErrorActionPreference = 'Stop'
foreach ($required in @(
    'AITradingCompanion.exe',
    'resources\schedules\tasks.json',
    'resources\contracts\companion-client-event-v1.schema.json',
    'runtime\ai_trading_companion\__main__.py',
    'scripts\run_companion_service.ps1'
)) {
    $path = Join-Path $InstallRoot $required
    if (-not (Test-Path -LiteralPath $path)) { throw "Required installation artifact is missing: $path" }
}
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $InstallRoot
$env:AI_TRADING_COMPANION_HOME = $CompanionHome
$env:PYTHONPATH = "$InstallRoot\runtime"
$runtimePython = Join-Path $CompanionHome 'runtime\python\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
& $python -m ai_trading_companion status | Out-Null
Write-Output "AITradingCompanion installation verified: $InstallRoot"
