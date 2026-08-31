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

$memoryHubSource = Join-Path $projectRoot 'memoryhub'
robocopy $memoryHubSource $staging /E /XD tests __pycache__ .pytest_cache /XF *.pyc *.pyo /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "MemoryHub staging copy failed with robocopy exit code $LASTEXITCODE" }
foreach ($cache in [IO.Directory]::EnumerateDirectories($staging, '__pycache__', [IO.SearchOption]::AllDirectories)) {
    [IO.Directory]::Delete($cache, $true)
}
foreach ($file in [IO.Directory]::EnumerateFiles($staging, '*', [IO.SearchOption]::AllDirectories)) {
    if ([IO.Path]::GetExtension($file) -in @('.pyc', '.pyo')) { [IO.File]::Delete($file) }
}
@{
    product = 'TradingMemoryHub'
    source_revision = $revision
    protocol_version = 'memoryhub/v1'
    built_at = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $staging 'build-info.json') -Encoding UTF8

foreach ($required in @(
    'pyproject.toml', 'README.md', 'src\trading_memory_hub\server.py',
    'openapi\memoryhub-v1.openapi.json', 'deploy\memoryhub.service'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $staging $required) -PathType Leaf)) {
        throw "MemoryHub package is missing required file: $required"
    }
}

New-Item -ItemType Directory -Path $resolvedRoot -Force | Out-Null
if (Test-Path -LiteralPath $artifact) { [IO.File]::Delete([IO.Path]::GetFullPath($artifact)) }
tar -C $stagingParent -czf $artifact $packageName
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $artifact)) {
    throw 'MemoryHub package creation failed.'
}
Write-Output "Published MemoryHub: $artifact"
