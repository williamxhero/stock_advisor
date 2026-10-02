[CmdletBinding()]
param(
    [switch]$Release,
    [ValidateSet('all', 'EvidenceSpec')]
    [string]$Select = 'all'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\memoryhub\src;$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

if ($Select -eq 'all') {
    py -m pytest "$root\memoryhub\tests" -q
    if ($LASTEXITCODE -ne 0) { throw "MemoryHub tests failed with exit code $LASTEXITCODE." }
    py -m pytest "$root\tests\runtime" -q
    if ($LASTEXITCODE -ne 0) { throw "Runtime tests failed with exit code $LASTEXITCODE." }
}
else {
    py -m pytest "$root\tests\runtime\test_evidence_spec.py" "$root\tests\runtime\test_evidence_qualification.py" "$root\tests\runtime\test_evidence_snapshot.py" -q
    if ($LASTEXITCODE -ne 0) { throw "EvidenceSpec regression tests failed with exit code $LASTEXITCODE." }
}
dotnet test "$root\AITradingCompanion.sln" $(if ($Release) { '--configuration'; 'Release' }) --nologo
if ($LASTEXITCODE -ne 0) { throw "Desktop tests failed with exit code $LASTEXITCODE." }
