[CmdletBinding()]
param(
    [switch]$Execute,
    [int]$PollSeconds = 5
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:AI_TRADING_COMPANION_INSTALL_ROOT = $root
if (-not $env:AI_TRADING_COMPANION_HOME) {
    $env:AI_TRADING_COMPANION_HOME = Join-Path $env:LOCALAPPDATA 'AITradingCompanion'
}
$runtimePackageRoot = if (Test-Path -LiteralPath (Join-Path $root 'src\runtime')) {
    Join-Path $root 'src\runtime'
}
else {
    Join-Path $root 'runtime'
}
$env:PYTHONPATH = "$runtimePackageRoot" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })
$mutex = [System.Threading.Mutex]::new($false, 'Local\AITradingCompanionRuntime')
if (-not $mutex.WaitOne(0)) {
    Write-Host 'Companion service is already running.'
    exit 0
}
$logPath = Join-Path $env:AI_TRADING_COMPANION_HOME 'runtime\logs\service-errors.log'
$heartbeatPath = Join-Path $env:AI_TRADING_COMPANION_HOME 'runtime\service-heartbeat.json'

function Write-ServiceState([string]$State, [string]$ErrorText = '') {
    $directory = Split-Path -Parent $heartbeatPath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    @{
        state = $State
        pid = $PID
        updated_at = (Get-Date).ToUniversalTime().ToString('o')
        error = $ErrorText
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $heartbeatPath -Encoding UTF8
}

function Write-ServiceError([string]$Message) {
    $directory = Split-Path -Parent $logPath
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $Message)
}

function Invoke-Companion([string[]]$Arguments) {
    $runtimePython = Join-Path $env:AI_TRADING_COMPANION_HOME 'runtime\python\Scripts\python.exe'
    $python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
    $output = & $python -m ai_trading_companion @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-ServiceError ($output -join [Environment]::NewLine)
    }
}

function Start-ScheduledWorkers {
    $runtimePython = Join-Path $env:AI_TRADING_COMPANION_HOME 'runtime\python\Scripts\python.exe'
    $python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
    $raw = & $python -m ai_trading_companion claim-scheduled-workers 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-ServiceError ($raw -join [Environment]::NewLine)
        return
    }
    try { $cycles = (($raw -join "`n") | ConvertFrom-Json).cycles } catch {
        Write-ServiceError "Cannot parse scheduled worker claims: $raw"
        return
    }
    foreach ($cycleId in @($cycles)) {
        $arguments = @('-m', 'ai_trading_companion', 'run-scheduled-cycle', '--cycle-id', $cycleId)
        if ($Execute) { $arguments += '--execute' }
        # Workers have already made an atomic SQLite claim.  Hidden independent
        # processes let the scheduler continue to materialise later tasks.
        Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden | Out-Null
    }
}

try {
    Write-Host "Companion Gateway service started. Execute=$Execute. Ctrl+C stops it."
    $arguments = @('-m', 'ai_trading_companion', 'serve-gateway')
    if ($Execute) { $arguments += '--execute' }
    $runtimePython = Join-Path $env:AI_TRADING_COMPANION_HOME 'runtime\python\Scripts\python.exe'
    $python = if (Test-Path -LiteralPath $runtimePython) { $runtimePython } else { 'py' }
    Write-ServiceState 'starting'
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { Write-ServiceError "Gateway exited with code $LASTEXITCODE"; Write-ServiceState 'degraded' "Gateway exited with code $LASTEXITCODE" }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
