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
    'resources\contracts\companion-decision-cycle-v1.schema.json',
    'resources\contracts\evidence-snapshot-spec-v1.schema.json',
    'resources\contracts\evidence-spec-v1.schema.json',
    'resources\contracts\temporal-integrity-spec-v1.schema.json',
    'resources\contracts\agent-contract-spec-v1.schema.json',
    'resources\contracts\agent-contract-input-v1.schema.json',
    'resources\contracts\agent-role-spec-v1.schema.json',
    'resources\contracts\agent-role-input-v1.schema.json',
    'resources\contracts\coordinator-spec-v1.schema.json',
    'resources\contracts\companion-m1-result-v5.schema.json',
    'resources\contracts\narrative-review-m1-v1.schema.json',
    'resources\contracts\debate-spec-v1.schema.json',
    'resources\contracts\debate-input-v1.schema.json',
    'resources\contracts\companion-published-message-v2.schema.json',
    'runtime\ai_trading_companion\__main__.py',
    'runtime\ai_trading_companion\evidence_snapshot.py',
    'runtime\ai_trading_companion\temporal_integrity.py',
    'runtime\ai_trading_companion\message_presentation.py',
    'runtime\ai_trading_companion\agent_contract.py',
    'runtime\ai_trading_companion\agent_role.py',
    'runtime\ai_trading_companion\judgment_publication.py',
    'runtime\ai_trading_companion\debate.py',
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
$coordinatorSchemaPath = Join-Path $InstallRoot 'resources\contracts\coordinator-spec-v1.schema.json'
$coordinatorSchema = Get-Content -LiteralPath $coordinatorSchemaPath -Raw | ConvertFrom-Json
if ($coordinatorSchema.title -ne 'CoordinatorSpec/v1' -or $coordinatorSchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed CoordinatorSpec schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('contract', 'version', 'dependency_graph', 'states', 'frontier', 'lifecycles')) {
    if ($coordinatorSchema.required -notcontains $required) {
        throw "Installed CoordinatorSpec schema is missing required field: $required."
    }
}
$m1Schema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\companion-m1-result-v5.schema.json') -Raw | ConvertFrom-Json
$m1Publication = $m1Schema.properties.publication
foreach ($required in @('coordination', 'coordination_hash')) {
    if ($m1Publication.required -notcontains $required) {
        throw "Installed M1 schema is missing required coordination field: $required."
    }
}
foreach ($required in @('version', 'core_hash', 'source_hash', 'reasons', 'counterargument', 'source_quality', 'conflicts', 'risk_stance', 'critical_unknowns')) {
    if ($m1Publication.properties.coordination.required -notcontains $required) {
        throw "Installed M1 coordination schema is missing required field: $required."
    }
}
$m1ReviewSchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\narrative-review-m1-v1.schema.json') -Raw | ConvertFrom-Json
if ($m1ReviewSchema.required -notcontains 'coordination_hash') {
    throw 'Installed M1 review schema does not bind the coordination hash.'
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
    Push-Location $healthHome
    try {
        & $python -m ai_trading_companion status | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Installed Runtime health check failed with exit code $LASTEXITCODE."
        }
        # Run only from the installed runtime path and replay the same frozen
        # role evidence twice.  The two receipts must be byte-identical; the
        # qualification keeps speed, qualification probability, research
        # quality, judgment outcome, and safety reliability as separate axes.
        $roleReplayOne = ((& $python -m ai_trading_companion.agent_role) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AgentRole replay 1 failed with exit code $LASTEXITCODE." }
        $roleReplayTwo = ((& $python -m ai_trading_companion.agent_role) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AgentRole replay 2 failed with exit code $LASTEXITCODE." }
        if ($roleReplayOne -ne $roleReplayTwo) { throw 'Installed AgentRole frozen replays were not deterministic.' }
        $roleQualification = $roleReplayOne | ConvertFrom-Json
        if ($roleQualification.contract -ne 'AgentRoleInstallQualification/v1' -or $roleQualification.qualified -ne $true) {
            throw 'Installed AgentRole qualification did not pass.'
        }
        $evidenceReplayOne = ((& $python -m ai_trading_companion.evidence_spec) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed EvidenceSpec replay 1 failed with exit code $LASTEXITCODE." }
        $evidenceReplayTwo = ((& $python -m ai_trading_companion.evidence_spec) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed EvidenceSpec replay 2 failed with exit code $LASTEXITCODE." }
        if ($evidenceReplayOne -ne $evidenceReplayTwo) { throw 'Installed EvidenceSpec frozen replays were not deterministic.' }
        $evidenceQualification = $evidenceReplayOne | ConvertFrom-Json
        if ($evidenceQualification.contract -ne 'EvidenceSpecInstallQualification/v1' -or $evidenceQualification.qualified -ne $true) {
            throw 'Installed EvidenceSpec qualification did not pass.'
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($evidenceQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed EvidenceSpec qualification is missing evaluation axis: $axis"
            }
            if ($evidenceQualification.evaluation_vector.$axis.status -notin @('pass', 'fail', 'not_measured')) {
                throw "Installed EvidenceSpec evaluation axis lacks an explicit measurement status: $axis"
            }
            $measurements = $evidenceQualification.evaluation_vector.$axis.measurements
            if ($null -eq $measurements -or $measurements.measured -isnot [bool]) {
                throw "Installed EvidenceSpec evaluation axis lacks structured measurements: $axis"
            }
            if ($evidenceQualification.evaluation_vector.$axis.status -ne 'pass' -and
                [string]::IsNullOrWhiteSpace([string]$evidenceQualification.evaluation_vector.$axis.reason)) {
                throw "Installed EvidenceSpec non-passing evaluation axis lacks a reason: $axis"
            }
        }
        if ($evidenceQualification.evaluation_vector.safety_reliability.status -ne 'pass' -or
            $evidenceQualification.evaluation_vector.safety_reliability.measurements.replay_equal -ne $true -or
            $evidenceQualification.evaluation_vector.safety_reliability.measurements.unavailable_source_safe -ne $true) {
            throw 'Installed EvidenceSpec measured safety checks failed.'
        }
        $unavailableSmoke = $evidenceQualification.source_unavailable_smoke
        if ($unavailableSmoke.contract -ne 'EvidenceSpecSourceAvailability/v1' -or
            $unavailableSmoke.status -ne 'failed' -or
            $unavailableSmoke.available -ne $false -or
            $unavailableSmoke.qualified -ne $false -or
            $unavailableSmoke.verifier_passed -ne $false -or
            @($unavailableSmoke.backend_calls).Count -lt 1 -or
            $unavailableSmoke.reason -ne 'source_unavailable' -or
            @($unavailableSmoke.evidence_items).Count -ne 0) {
            throw 'Installed EvidenceSpec source-unavailable smoke did not preserve an unqualified unavailable result.'
        }
        foreach ($measurement in @('expected_backend_call', 'single_failed_acquisition', 'no_fabricated_evidence', 'no_qualified_fallback')) {
            if ($unavailableSmoke.measurements.$measurement -ne $true) {
                throw "Installed EvidenceSpec source-unavailable measurement failed: $measurement"
            }
        }
        $debateReplayOne = ((& $python -m ai_trading_companion.debate) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed Debate replay 1 failed with exit code $LASTEXITCODE." }
        $debateReplayTwo = ((& $python -m ai_trading_companion.debate) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed Debate replay 2 failed with exit code $LASTEXITCODE." }
        if ($debateReplayOne -ne $debateReplayTwo) { throw 'Installed Debate frozen replays were not deterministic.' }
        $debateQualification = $debateReplayOne | ConvertFrom-Json
        if ($debateQualification.contract -ne 'DebateInstallQualification/v1' -or $debateQualification.qualified -ne $true) {
            throw 'Installed Debate qualification did not pass.'
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($debateQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed Debate qualification is missing evaluation axis: $axis"
            }
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($roleQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed AgentRole qualification is missing evaluation axis: $axis"
            }
        }
    }
    finally {
        Pop-Location
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
