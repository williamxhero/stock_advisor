[CmdletBinding()]
param([switch]$Release)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

py -m unittest discover -s "$root\tests\runtime" -v
dotnet test "$root\AITradingCompanion.sln" $(if ($Release) { '--configuration'; 'Release' }) --nologo
