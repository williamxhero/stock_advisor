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
    'resources\contracts\evidence-qualification-spec-v1.schema.json',
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
    'resources\contracts\memory-type-spec-v1.schema.json',
    'resources\contracts\memory-write-spec-v1.schema.json',
    'resources\contracts\reflection-spec-v1.schema.json',
    'resources\contracts\analysis-skill-spec-v1.schema.json',
    'resources\contracts\skill-registry-spec-v1.schema.json',
    'resources\contracts\companion-published-message-v2.schema.json',
    'runtime\ai_trading_companion\__main__.py',
    'runtime\ai_trading_companion\cycle_replay.py',
    'runtime\ai_trading_companion\evidence_snapshot.py',
    'runtime\ai_trading_companion\temporal_integrity.py',
    'runtime\ai_trading_companion\message_presentation.py',
    'runtime\ai_trading_companion\agent_contract.py',
    'runtime\ai_trading_companion\agent_role.py',
    'runtime\ai_trading_companion\judgment_publication.py',
    'runtime\ai_trading_companion\debate.py',
    'runtime\ai_trading_companion\memory_type.py',
    'runtime\ai_trading_companion\memory_write.py',
    'runtime\ai_trading_companion\reflection.py',
    'runtime\ai_trading_companion\analysis_skill.py',
    'runtime\ai_trading_companion\skill_registry.py',
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
$skillRegistrySchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\skill-registry-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($skillRegistrySchema.title -ne 'SkillRegistrySpec/v1' -or $skillRegistrySchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed SkillRegistry schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('contract', 'version', 'registry_version', 'skills', 'provenance', 'permissions')) {
    if ($skillRegistrySchema.required -notcontains $required) {
        throw "Installed SkillRegistry schema is missing required field: $required."
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
        # Source-unavailable smoke: resolve the imported module and prove that
        # this run is served by the release runtime, rather than the checkout
        # that happened to launch the verifier.
        $modulePath = ((& $python -c "import ai_trading_companion, pathlib; print(pathlib.Path(ai_trading_companion.__file__).resolve())") -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed Runtime module resolution failed with exit code $LASTEXITCODE." }
        $resolvedInstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
        $resolvedModulePath = [IO.Path]::GetFullPath($modulePath)
        if (-not $resolvedModulePath.StartsWith($resolvedInstallRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Source-unavailable smoke resolved runtime outside the release directory: $resolvedModulePath"
        }
        # Run only from the installed runtime path and replay the same frozen
        # cycle twice, preserving the original receipt and published artifact.
        $cycleSmoke = @'
import copy
import json
from pathlib import Path
from ai_trading_companion.broker_client import canonical_packet_hash
from ai_trading_companion.cycle_replay import freeze_cycle, replay_cycle
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore
store = CompanionStore(Path('cycle-replay.sqlite3'))
cycle = CompanionEngine(store).start_cycle('daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z')
packet = {'frozen_public_evidence': [], 'business_context': {'positions': []}}
attempt = store.begin_attempt(cycle['cycle_id'], 'm1_judgment', cycle['as_of'], input_packet=packet, input_sha256=canonical_packet_hash(packet), model='install-fixture', runner_fingerprint='install-fixture/v1')
store.finish_attempt(attempt['attempt_id'], 'succeeded', output={'direction': 'wait'}, verifier={'passed': True})
store.append_artifact(cycle['cycle_id'], 'm1', 'model', 'Original judgment', cycle['as_of'])
frozen = freeze_cycle(store, cycle['cycle_id'])
original = copy.deepcopy(frozen)
first = replay_cycle(frozen)
second = replay_cycle(copy.deepcopy(frozen))
if first != second or frozen != original or freeze_cycle(store, cycle['cycle_id']) != original:
    raise RuntimeError('Cycle replay changed frozen history or was not deterministic')
if first['qualification']['attempts'][0]['qualified'] is not True:
    raise RuntimeError('Installed cycle replay did not reconstruct qualification')
print(json.dumps(first['evaluation_vector'], sort_keys=True))
'@
        $cycleQualification = ((& $python -c $cycleSmoke) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed cycle replay failed with exit code $LASTEXITCODE." }
        $cycleVector = $cycleQualification | ConvertFrom-Json
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($cycleVector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed cycle replay is missing evaluation axis: $axis"
            }
        }
        # Replay the frozen
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
        $memoryTypeReplayOne = ((& $python -m ai_trading_companion.memory_type) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MemoryType replay 1 failed with exit code $LASTEXITCODE." }
        $memoryTypeReplayTwo = ((& $python -m ai_trading_companion.memory_type) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MemoryType replay 2 failed with exit code $LASTEXITCODE." }
        if ($memoryTypeReplayOne -ne $memoryTypeReplayTwo) { throw 'Installed MemoryType frozen replays were not deterministic.' }
        $memoryTypeQualification = $memoryTypeReplayOne | ConvertFrom-Json
        if ($memoryTypeQualification.contract -ne 'MemoryTypeInstallQualification/v1' -or $memoryTypeQualification.qualified -ne $true) {
            throw 'Installed MemoryType qualification did not pass.'
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($memoryTypeQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed MemoryType qualification is missing evaluation axis: $axis"
            }
        }
        $memoryWriteReplayOne = ((& $python -m ai_trading_companion.memory_write) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MemoryWrite replay 1 failed with exit code $LASTEXITCODE." }
        $memoryWriteReplayTwo = ((& $python -m ai_trading_companion.memory_write) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MemoryWrite replay 2 failed with exit code $LASTEXITCODE." }
        if ($memoryWriteReplayOne -ne $memoryWriteReplayTwo) { throw 'Installed MemoryWrite frozen replays were not deterministic.' }
        $memoryWriteQualification = $memoryWriteReplayOne | ConvertFrom-Json
        if ($memoryWriteQualification.contract -ne 'MemoryWriteInstallQualification/v1' -or $memoryWriteQualification.qualified -ne $true) {
            throw 'Installed MemoryWrite qualification did not pass.'
        }
        foreach ($check in $memoryWriteQualification.checks.PSObject.Properties) {
            if ($check.Value -ne $true) { throw "Installed MemoryWrite check failed: $($check.Name)" }
        }
        $reflectionReplayOne = ((& $python -m ai_trading_companion.reflection) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed Reflection replay 1 failed with exit code $LASTEXITCODE." }
        $reflectionReplayTwo = ((& $python -m ai_trading_companion.reflection) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed Reflection replay 2 failed with exit code $LASTEXITCODE." }
        if ($reflectionReplayOne -ne $reflectionReplayTwo) { throw 'Installed Reflection frozen replays were not deterministic.' }
        $reflectionQualification = $reflectionReplayOne | ConvertFrom-Json
        if ($reflectionQualification.contract -ne 'ReflectionInstallQualification/v1' -or $reflectionQualification.qualified -ne $true) {
            throw 'Installed Reflection qualification did not pass.'
        }
        $analysisSkillReplayOne = ((& $python -m ai_trading_companion.analysis_skill) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AnalysisSkill replay 1 failed with exit code $LASTEXITCODE." }
        $analysisSkillReplayTwo = ((& $python -m ai_trading_companion.analysis_skill) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AnalysisSkill replay 2 failed with exit code $LASTEXITCODE." }
        if ($analysisSkillReplayOne -ne $analysisSkillReplayTwo) { throw 'Installed AnalysisSkill frozen replays were not deterministic.' }
        $analysisSkillQualification = $analysisSkillReplayOne | ConvertFrom-Json
        if ($analysisSkillQualification.contract -ne 'AnalysisSkillInstallQualification/v1' -or $analysisSkillQualification.qualified -ne $true) {
            throw 'Installed AnalysisSkill qualification did not pass.'
        }
        $skillRegistryReplayOne = ((& $python -m ai_trading_companion.skill_registry) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed SkillRegistry replay 1 failed with exit code $LASTEXITCODE." }
        $skillRegistryReplayTwo = ((& $python -m ai_trading_companion.skill_registry) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed SkillRegistry replay 2 failed with exit code $LASTEXITCODE." }
        if ($skillRegistryReplayOne -ne $skillRegistryReplayTwo) { throw 'Installed SkillRegistry frozen replays were not deterministic.' }
        $skillRegistryQualification = $skillRegistryReplayOne | ConvertFrom-Json
        if ($skillRegistryQualification.contract -ne 'SkillRegistryInstallQualification/v1' -or $skillRegistryQualification.qualified -ne $true) {
            throw 'Installed SkillRegistry qualification did not pass.'
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($skillRegistryQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed SkillRegistry qualification is missing evaluation axis: $axis"
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
