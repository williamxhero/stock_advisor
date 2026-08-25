[CmdletBinding()]
param(
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Runtime = 'win-x64'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$output = Join-Path $projectRoot "artifacts\$Runtime"
$revision = (& git -C $projectRoot rev-parse --short HEAD 2>$null)
if (-not $revision) { $revision = 'unknown' }
$dirty = -not [string]::IsNullOrWhiteSpace((& git -C $projectRoot status --short 2>$null | Out-String))

dotnet publish "$projectRoot\src\AIDecisionCenter.App\AIDecisionCenter.App.csproj" `
    --configuration Release `
    --runtime $Runtime `
    --self-contained false `
    --output $output `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    -p:UseSharedCompilation=false `
    -p:SourceRevisionId=$revision `
    -m:1 `
    --disable-build-servers

@{
    source_revision = $revision
    dirty = $dirty
    built_at = (Get-Date).ToUniversalTime().ToString('o')
    runtime = $Runtime
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'build-info.json') -Encoding UTF8

Write-Output "Published to: $output"
