[CmdletBinding()]
param([switch]$Release)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

py -m pytest "$root\tests\runtime" -q
dotnet test "$root\AITradingCompanion.sln" $(if ($Release) { '--configuration'; 'Release' }) --nologo
