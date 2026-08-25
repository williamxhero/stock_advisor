[CmdletBinding()]
param(
    [string]$CompanionHome = (Join-Path $env:LOCALAPPDATA 'AITradingCompanion'),
    [string]$LegacyRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $root
$env:AI_TRADING_COMPANION_HOME = $CompanionHome
$env:PYTHONPATH = "$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { '' })

py -m ai_trading_companion migrate-legacy --legacy-root $LegacyRoot
