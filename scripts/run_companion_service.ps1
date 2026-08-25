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

try {
    Write-Host "Companion service started. Execute=$Execute; poll=${PollSeconds}s. Ctrl+C stops it."
    while ($true) {
        try {
            if ($Execute) { Invoke-Companion -Arguments @('run-schedule', '--execute') }
            else { Invoke-Companion -Arguments @('run-schedule') }
            if ($Execute) { Invoke-Companion -Arguments @('run-due', '--execute') }
            else { Invoke-Companion -Arguments @('run-due') }
            if ($Execute) { Invoke-Companion -Arguments @('consume-command', '--execute') }
            else { Invoke-Companion -Arguments @('consume-command') }
            if ($Execute) { Invoke-Companion -Arguments @('run-background', '--execute') }
            else { Invoke-Companion -Arguments @('run-background') }
            Invoke-Companion -Arguments @('dispatch')
            Write-ServiceState 'running'
        }
        catch {
            Write-ServiceError $_.Exception.ToString()
            Write-ServiceState 'degraded' $_.Exception.Message
        }
        Start-Sleep -Seconds ([Math]::Max(1, $PollSeconds))
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
