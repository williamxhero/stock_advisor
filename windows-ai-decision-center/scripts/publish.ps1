[CmdletBinding()]
param(
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Runtime = 'win-x64'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$output = Join-Path $projectRoot "artifacts\$Runtime"

dotnet publish "$projectRoot\src\AIDecisionCenter.App\AIDecisionCenter.App.csproj" `
    --configuration Release `
    --runtime $Runtime `
    --self-contained false `
    --output $output `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:UseSharedCompilation=false `
    -m:1 `
    --disable-build-servers

Write-Output "Published to: $output"
