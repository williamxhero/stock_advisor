[CmdletBinding()]
param(
    [string]$InstallRoot = 'D:\APP\AITradingCompanion\app',
    [string]$CompanionHome = 'D:\APP\AITradingCompanion',
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
$env:PYTHONPATH = "$InstallRoot\runtime"
$runtimePython = Join-Path $CompanionHome 'runtime\python\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
$healthHome = Join-Path $CompanionHome ("verification\install-health-" + [guid]::NewGuid().ToString('N'))
$previousHome = $env:AI_TRADING_COMPANION_HOME
New-Item -ItemType Directory -Path $healthHome -Force | Out-Null
try {
    # Runtime status performs schema/schedule initialization. Keep that health
    # smoke isolated from the user's formal database and workspace while still
    # exercising the exact installed Runtime and resources.
    $env:AI_TRADING_COMPANION_HOME = $healthHome
    & $python -m ai_trading_companion status | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Installed Runtime health check failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($null -eq $previousHome) {
        Remove-Item Env:AI_TRADING_COMPANION_HOME -ErrorAction SilentlyContinue
    }
    else {
        $env:AI_TRADING_COMPANION_HOME = $previousHome
    }
    $resolvedHealthHome = [IO.Path]::GetFullPath($healthHome)
    $resolvedCompanionHome = [IO.Path]::GetFullPath($CompanionHome).TrimEnd('\')
    if (-not $resolvedHealthHome.StartsWith(
        $resolvedCompanionHome + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to clean install health home outside companion home: $resolvedHealthHome"
    }
    if (Test-Path -LiteralPath $resolvedHealthHome) {
        [IO.Directory]::Delete($resolvedHealthHome, $true)
    }
}
Write-Output "AITradingCompanion installation verified: $InstallRoot"
