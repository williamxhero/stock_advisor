[CmdletBinding()]
param(
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Runtime = 'win-x64',
    [switch]$NoRestore
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$output = Join-Path $projectRoot "dist\$Runtime\AITradingCompanion"
$outputParent = Split-Path -Parent $output
if (Test-Path -LiteralPath $output) {
    $resolvedOutput = [IO.Path]::GetFullPath($output)
    $resolvedParent = [IO.Path]::GetFullPath($outputParent)
    if (-not $resolvedOutput.StartsWith($resolvedParent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean a directory outside the release output: $resolvedOutput"
    }
    # Some PowerShell hosts install a Remove-Item proxy that fails on a
    # self-contained publish tree.  The target has already been resolved and
    # constrained to this release output parent, so use the .NET operation
    # directly rather than widening deletion scope or silently reusing files.
    [IO.Directory]::Delete($resolvedOutput, $true)
}
$revision = (& git -C $projectRoot rev-parse --short HEAD 2>$null)
if (-not $revision) { $revision = 'unknown' }
$dirty = -not [string]::IsNullOrWhiteSpace((& git -C $projectRoot status --short 2>$null | Out-String))

$publishArguments = @(
    'publish', "$projectRoot\src\desktop\AITradingCompanion.Desktop\AITradingCompanion.Desktop.csproj",
    '--configuration', 'Release', '--runtime', $Runtime, '--self-contained', 'true', '--output', $output,
    '-p:PublishSingleFile=true', '-p:IncludeNativeLibrariesForSelfExtract=true', '-p:UseSharedCompilation=false',
    "-p:SourceRevisionId=$revision", '-m:1', '--disable-build-servers'
)
if ($NoRestore) { $publishArguments += '--no-restore' }
dotnet @publishArguments
if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish failed; no usable release artifact was generated."
}

foreach ($item in @(
    @{ Source = 'resources'; Destination = 'resources' },
    @{ Source = 'src\runtime\ai_trading_companion'; Destination = 'runtime\ai_trading_companion' },
    @{ Source = 'scripts\run_companion_service.ps1'; Destination = 'scripts\run_companion_service.ps1' },
    @{ Source = 'scripts\verify-install.ps1'; Destination = 'scripts\verify-install.ps1' },
    @{ Source = 'scripts\migrate-legacy.ps1'; Destination = 'scripts\migrate-legacy.ps1' },
    @{ Source = 'scripts\requirements-runtime.txt'; Destination = 'scripts\requirements-runtime.txt' }
)) {
    $source = Join-Path $projectRoot $item.Source
    $destination = Join-Path $output $item.Destination
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    if (Test-Path -LiteralPath $source -PathType Container) {
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Get-ChildItem -LiteralPath $source -Force | Copy-Item -Destination $destination -Recurse -Force
    }
    else {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

@{
    source_revision = $revision
    dirty = $dirty
    built_at = (Get-Date).ToUniversalTime().ToString('o')
    runtime = $Runtime
    product = 'AITradingCompanion'
    python_runtime = 'external-python-required'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $output 'build-info.json') -Encoding UTF8

Write-Output "Published to: $output"
