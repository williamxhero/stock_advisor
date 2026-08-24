[CmdletBinding()]
param(
    [switch]$Release
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configuration = if ($Release) { 'Release' } else { 'Debug' }

dotnet run --project "$projectRoot\src\AIDecisionCenter.App\AIDecisionCenter.App.csproj" --configuration $configuration
