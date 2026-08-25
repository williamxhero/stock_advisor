[CmdletBinding()]
param(
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configuration = if ($Release) { 'Release' } else { 'Debug' }
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $projectRoot

dotnet run --project "$projectRoot\src\desktop\AITradingCompanion.Desktop\AITradingCompanion.Desktop.csproj" --configuration $configuration
