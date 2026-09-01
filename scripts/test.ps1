[CmdletBinding()]
param([switch]$Release)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\memoryhub\src;$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

py -m pytest "$root\memoryhub\tests" -q
if ($LASTEXITCODE -ne 0) { throw "MemoryHub tests failed with exit code $LASTEXITCODE." }
py -m pytest "$root\tests\runtime" -q
if ($LASTEXITCODE -ne 0) { throw "Runtime tests failed with exit code $LASTEXITCODE." }
dotnet test "$root\AITradingCompanion.sln" $(if ($Release) { '--configuration'; 'Release' }) --nologo
if ($LASTEXITCODE -ne 0) { throw "Desktop tests failed with exit code $LASTEXITCODE." }
