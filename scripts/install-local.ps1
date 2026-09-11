[CmdletBinding()]
param(
    [ValidateSet('win-x64', 'win-arm64')]
    [string]$Runtime = 'win-x64',
    [switch]$EnableStartup,
    [string]$CompanionHome = 'D:\APP\AITradingCompanion',
    [string]$PreviousCompanionHome = (Join-Path $env:LOCALAPPDATA 'AITradingCompanion'),
    [switch]$KeepPreviousHome,
    [switch]$SkipLegacyMigration
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
& (Join-Path $root 'scripts\publish.ps1') -Runtime $Runtime -NoRestore
$source = Join-Path 'D:\APP\AITradingCompanion\release' "$Runtime\AITradingCompanion"
$companionHome = [System.IO.Path]::GetFullPath($CompanionHome)
$previousCompanionHome = [System.IO.Path]::GetFullPath($PreviousCompanionHome)
$app = Join-Path $companionHome 'app'
$staging = "$app.staging-$([guid]::NewGuid().ToString('N'))"
$migratedPreviousHome = $false

function Stop-InstalledCompanionProcessTree {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$CompanionRoot
    )

    $normalizedRoot = ([System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\').ToLowerInvariant()
    $normalizedRuntimePythonRoot = (
        [System.IO.Path]::GetFullPath((Join-Path $CompanionRoot 'runtime\python')).TrimEnd('\') + '\'
    ).ToLowerInvariant()
    $processes = @(Get-CimInstance Win32_Process)
    $selected = New-Object 'System.Collections.Generic.Dictionary[uint32,object]'
    $depths = New-Object 'System.Collections.Generic.Dictionary[uint32,int]'
    $queue = New-Object 'System.Collections.Generic.Queue[uint32]'

    foreach ($process in $processes) {
        $executable = if ($process.ExecutablePath) { $process.ExecutablePath.ToLowerInvariant() } else { '' }
        $commandLine = if ($process.CommandLine) { $process.CommandLine.ToLowerInvariant() } else { '' }
        $isInstalledProcess = $executable.StartsWith($normalizedRoot) -or $commandLine.Contains($normalizedRoot)
        $isRuntimeGateway = (
            $executable.StartsWith($normalizedRuntimePythonRoot) -and
            $commandLine.Contains('-m ai_trading_companion serve-gateway')
        )
        if ($isInstalledProcess -or $isRuntimeGateway) {
            $processId = [uint32]$process.ProcessId
            if (-not $selected.ContainsKey($processId)) {
                $selected.Add($processId, $process)
                $depths.Add($processId, 0)
                $queue.Enqueue($processId)
            }
        }
    }

    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in ($processes | Where-Object ParentProcessId -eq $parentId)) {
            $childId = [uint32]$child.ProcessId
            $childDepth = $depths[$parentId] + 1
            if (-not $selected.ContainsKey($childId)) {
                $selected.Add($childId, $child)
                $depths.Add($childId, $childDepth)
                $queue.Enqueue($childId)
            } elseif ($depths[$childId] -lt $childDepth) {
                $depths[$childId] = $childDepth
            }
        }
    }

    foreach ($entry in ($selected.GetEnumerator() | Sort-Object { $depths[$_.Key] } -Descending)) {
        Stop-Process -Id $entry.Key -Force -ErrorAction SilentlyContinue
    }
    if ($selected.Count -gt 0) { Start-Sleep -Milliseconds 500 }
}

function Move-DirectoryWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [int]$MaximumAttempts = 10,
        [int]$DelayMilliseconds = 200
    )

    for ($attempt = 1; $attempt -le $MaximumAttempts; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination
            return
        } catch {
            if ($attempt -eq $MaximumAttempts) { throw }
            Start-Sleep -Milliseconds $DelayMilliseconds
        }
    }
}

function Copy-PreviousCompanionState {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $destinationParent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
    $migrationStaging = "$Destination.migration-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $migrationStaging -Force | Out-Null
    try {
        $excludedDirectories = @(
            (Join-Path $Source 'app'),
            (Join-Path $Source 'runtime\python')
        )
        $excludedDirectories += @(
            Get-ChildItem -LiteralPath $Source -Directory -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'app-backup-*' -or $_.Name -like 'app.staging-*' } |
                ForEach-Object FullName
        )
        $arguments = @(
            $Source, $migrationStaging, '/E', '/COPY:DAT', '/DCOPY:DAT', '/R:2', '/W:1', '/XJ',
            '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/XF', 'gateway.json', 'service-heartbeat.json'
        )
        if ($excludedDirectories.Count -gt 0) {
            $arguments += '/XD'
            $arguments += $excludedDirectories
        }
        & robocopy @arguments | Out-Null
        if ($LASTEXITCODE -gt 7) {
            throw "State migration copy failed with robocopy exit code $LASTEXITCODE."
        }

        $sourceDatabase = Join-Path $Source 'data\trading-companion.sqlite3'
        $copiedDatabase = Join-Path $migrationStaging 'data\trading-companion.sqlite3'
        if (Test-Path -LiteralPath $sourceDatabase) {
            if (-not (Test-Path -LiteralPath $copiedDatabase)) {
                throw 'The migrated Runtime database is missing.'
            }
            $sourceHash = (Get-FileHash -LiteralPath $sourceDatabase -Algorithm SHA256).Hash
            $copiedHash = (Get-FileHash -LiteralPath $copiedDatabase -Algorithm SHA256).Hash
            if ($sourceHash -ne $copiedHash) {
                throw 'The migrated Runtime database hash does not match the source.'
            }
        }
        Move-DirectoryWithRetry -Source $migrationStaging -Destination $Destination
    }
    finally {
        if (Test-Path -LiteralPath $migrationStaging) {
            $resolvedStaging = [IO.Path]::GetFullPath($migrationStaging)
            $resolvedParent = [IO.Path]::GetFullPath($destinationParent).TrimEnd('\')
            if (-not $resolvedStaging.StartsWith(
                $resolvedParent + [IO.Path]::DirectorySeparatorChar,
                [StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Refusing to clean migration staging outside its parent: $resolvedStaging"
            }
            [IO.Directory]::Delete($resolvedStaging, $true)
        }
    }
}

$sameHome = $companionHome.Equals($previousCompanionHome, [StringComparison]::OrdinalIgnoreCase)
$previousDatabase = Join-Path $previousCompanionHome 'data\trading-companion.sqlite3'
$targetDatabase = Join-Path $companionHome 'data\trading-companion.sqlite3'
if (-not $sameHome -and (Test-Path -LiteralPath $previousDatabase) -and -not (Test-Path -LiteralPath $targetDatabase)) {
    if (Test-Path -LiteralPath $companionHome) {
        $existingTargetItems = @(Get-ChildItem -LiteralPath $companionHome -Force -ErrorAction SilentlyContinue)
        if ($existingTargetItems.Count -gt 0) {
            throw "Refusing to merge previous state into a non-empty target without a Runtime database: $companionHome"
        }
        [IO.Directory]::Delete($companionHome, $false)
    }
    Stop-InstalledCompanionProcessTree -InstallRoot (Join-Path $previousCompanionHome 'app') -CompanionRoot $previousCompanionHome
    Copy-PreviousCompanionState -Source $previousCompanionHome -Destination $companionHome
    $migratedPreviousHome = $true
}

New-Item -ItemType Directory -Path $companionHome -Force | Out-Null
$pythonHome = Join-Path $companionHome 'runtime\python'
$python = Join-Path $pythonHome 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -m venv $pythonHome
}
& $python -m pip install --disable-pip-version-check --upgrade -r (Join-Path $source 'scripts\requirements-runtime.txt')
$backup = $null
try {
    Copy-Item -LiteralPath $source -Destination $staging -Recurse -Force
    Stop-InstalledCompanionProcessTree -InstallRoot $app -CompanionRoot $companionHome
    if (Test-Path -LiteralPath $app) {
        $backup = Join-Path $companionHome ("app-backup-" + (Get-Date -Format 'yyyyMMddHHmmss'))
        Move-DirectoryWithRetry -Source $app -Destination $backup
    }
    try {
        Move-DirectoryWithRetry -Source $staging -Destination $app
    } catch {
        if ($backup -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $app)) {
            Move-DirectoryWithRetry -Source $backup -Destination $app
        }
        throw
    }
} finally {
    if (Test-Path -LiteralPath $staging) {
        $resolvedStaging = [IO.Path]::GetFullPath($staging)
        $resolvedCompanionHome = [IO.Path]::GetFullPath($companionHome).TrimEnd('\')
        if (-not $resolvedStaging.StartsWith(
            $resolvedCompanionHome + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing to clean staging outside companion home: $resolvedStaging"
        }
        # Remove-Item can be replaced by the host's recoverable-delete proxy.
        # This directory is a unique, validated install staging path and must
        # be removed synchronously without masking the original install error.
        [IO.Directory]::Delete($resolvedStaging, $true)
    }
}
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $app
$env:AI_TRADING_COMPANION_HOME = $companionHome
$env:PYTHONPATH = "$app\runtime"
if (-not $SkipLegacyMigration) {
    & $python -m ai_trading_companion migrate-legacy --legacy-root $root | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Legacy data migration failed; the previous data and installation remain unchanged.' }
}
& (Join-Path $app 'scripts\verify-install.ps1') -InstallRoot $app -CompanionHome $companionHome

$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$existingStartup = Get-ItemProperty -Path $run -Name 'AITradingCompanion' -ErrorAction SilentlyContinue
if ($EnableStartup -or $existingStartup) {
    New-Item -Path $run -Force | Out-Null
    Set-ItemProperty -Path $run -Name 'AITradingCompanion' -Value ('"' + (Join-Path $app 'AITradingCompanion.exe') + '"')
    $old = Get-ItemProperty -Path $run -Name 'AIDecisionCenter' -ErrorAction SilentlyContinue
    if ($old -and $old.AIDecisionCenter -match 'windows-ai-decision-center') {
        Remove-ItemProperty -Path $run -Name 'AIDecisionCenter'
    }
}

$previousArchive = $null
if ($migratedPreviousHome -and -not $KeepPreviousHome -and (Test-Path -LiteralPath $previousCompanionHome)) {
    $previousArchive = "$previousCompanionHome.migrated-backup-$(Get-Date -Format 'yyyyMMddHHmmss')"
    Move-DirectoryWithRetry -Source $previousCompanionHome -Destination $previousArchive
}

Write-Output "AI Trading Companion installed: $app"
if ($migratedPreviousHome) {
    Write-Output "AI Trading Companion data migrated: $previousCompanionHome -> $companionHome"
}
if ($previousArchive) {
    Write-Output "Previous installation retained for rollback: $previousArchive"
}
