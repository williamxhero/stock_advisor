[CmdletBinding()]
param([string]$OutputRoot = "")

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutputRoot) { $OutputRoot = Join-Path $projectRoot 'dist\memoryhub' }
$resolvedRoot = [IO.Path]::GetFullPath($OutputRoot)
$revision = (& git -C $projectRoot rev-parse --short HEAD).Trim()
$packageName = "trading-memoryhub-$revision"
$stagingParent = Join-Path $resolvedRoot 'staging'
$staging = Join-Path $stagingParent $packageName
$artifact = Join-Path $resolvedRoot "$packageName.tar.gz"

if (Test-Path -LiteralPath $staging) {
    $resolvedStaging = [IO.Path]::GetFullPath($staging)
    if (-not $resolvedStaging.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean staging outside output root: $resolvedStaging"
    }
    [IO.Directory]::Delete($resolvedStaging, $true)
}
New-Item -ItemType Directory -Path $staging -Force | Out-Null

foreach ($item in @('pyproject.toml', 'README.md', 'src', 'openapi', 'deploy')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "memoryhub\$item") -Destination $staging -Recurse -Force
}
Get-ChildItem -LiteralPath $staging -Recurse -Directory -Filter '__pycache__' |
    Sort-Object FullName -Descending |
    ForEach-Object { [IO.Directory]::Delete($_.FullName, $true) }
Get-ChildItem -LiteralPath $staging -Recurse -File -Include '*.pyc','*.pyo' |
    ForEach-Object { [IO.File]::Delete($_.FullName) }
@{
    product = 'TradingMemoryHub'
    source_revision = $revision
    protocol_version = 'memoryhub/v1'
    built_at = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $staging 'build-info.json') -Encoding UTF8

New-Item -ItemType Directory -Path $resolvedRoot -Force | Out-Null
if (Test-Path -LiteralPath $artifact) { [IO.File]::Delete([IO.Path]::GetFullPath($artifact)) }
tar -C $stagingParent -czf $artifact $packageName
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $artifact)) {
    throw 'MemoryHub package creation failed.'
}
Write-Output "Published MemoryHub: $artifact"
