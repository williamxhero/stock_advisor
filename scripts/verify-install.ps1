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
    'resources\contracts\adapter-contract-spec-v1.schema.json',
    'resources\contracts\mandate-spec-v1.schema.json',
    'resources\contracts\m0-observation-spec-v1.schema.json',
    'resources\contracts\m1-judgment-spec-v1.schema.json',
    'resources\contracts\position-safety-spec-v1.schema.json',
    'resources\contracts\risk-gate-spec-v1.schema.json',
    'resources\contracts\research-isolation-spec-v1.schema.json',
    'resources\contracts\regression-spec-v1.schema.json',
    'resources\contracts\observability-spec-v1.schema.json',
    'resources\contracts\observability-evaluation-v1.schema.json',
    'resources\contracts\observability-replay-v1.schema.json',
    'resources\contracts\companion-published-message-v2.schema.json',
    'runtime\ai_trading_companion\__main__.py',
    'runtime\ai_trading_companion\regression_gate.py',
    'runtime\ai_trading_companion\regression_probes.py',
    'runtime\ai_trading_companion\regression_spec.py',
    'runtime\ai_trading_companion\observability_contract.py',
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
    'runtime\ai_trading_companion\adapter_contract.py',
    'runtime\ai_trading_companion\mandate_spec.py',
    'runtime\ai_trading_companion\m0_observation.py',
    'runtime\ai_trading_companion\m1_judgment.py',
    'runtime\ai_trading_companion\position_safety.py',
    'runtime\ai_trading_companion\risk_gate.py',
    'runtime\ai_trading_companion\evidence_spec.py',
    'runtime\ai_trading_companion\evidence_qualification.py',
    'runtime\ai_trading_companion\local_research.py',
    'runtime\ai_trading_companion\research_isolation.py',
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
$mandateSchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\mandate-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($mandateSchema.title -ne 'MandateSpec/v1' -or $mandateSchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed MandateSpec schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('contract', 'version', 'task_key', 'stage', 'required_skills', 'optional_skills', 'memory_scope', 'quantresearch_permission', 'risk_level', 'visibility', 'provenance', 'permissions', 'sha256')) {
    if ($mandateSchema.required -notcontains $required) {
        throw "Installed MandateSpec schema is missing required field: $required."
    }
}
$m0ObservationSchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\m0-observation-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($m0ObservationSchema.title -ne 'M0ObservationResult/v1' -or $m0ObservationSchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed M0 observation schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('contract', 'version', 'spec_contract', 'stage', 'evidence_snapshot', 'evidence_refs', 'semantic', 'permissions', 'boundary', 'quantresearch', 'provenance', 'sha256')) {
    if ($m0ObservationSchema.required -notcontains $required) {
        throw "Installed M0 observation schema is missing required field: $required."
    }
}
$positionSafetySchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\position-safety-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($positionSafetySchema.title -ne 'PositionSafetySpec/v1' -or $positionSafetySchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed PositionSafety schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('oneOf', '$defs')) {
    if ($null -eq $positionSafetySchema.$required) { throw "Installed PositionSafety schema is missing required section: $required." }
}
$riskGateSchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\risk-gate-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($riskGateSchema.title -ne 'RiskGateSpec/v1' -or $riskGateSchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed RiskGate schema has an unexpected contract or schema dialect.'
}
foreach ($definition in @('input', 'result', 'frozen', 'replay', 'policy', 'permissions')) {
    if ($null -eq $riskGateSchema.'$defs'.$definition) { throw "Installed RiskGate schema is missing definition: $definition." }
}
$researchIsolationSchema = Get-Content -LiteralPath (Join-Path $InstallRoot 'resources\contracts\research-isolation-spec-v1.schema.json') -Raw | ConvertFrom-Json
if ($researchIsolationSchema.title -ne 'ResearchIsolationSpec/v1' -or $researchIsolationSchema.'$schema' -ne 'https://json-schema.org/draft/2020-12/schema') {
    throw 'Installed ResearchIsolation schema has an unexpected contract or schema dialect.'
}
foreach ($required in @('oneOf', '$defs')) {
    if ($null -eq $researchIsolationSchema.$required) { throw "Installed ResearchIsolation schema is missing required section: $required." }
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
$previousPythonIOEncoding = $env:PYTHONIOENCODING
$previousConsoleOutputEncoding = [Console]::OutputEncoding
New-Item -ItemType Directory -Path $healthHome -Force | Out-Null
try {
    # Runtime status performs schema/schedule initialization. Keep that health
    # smoke isolated from the user's formal database and workspace while still
    # exercising the exact installed Runtime and resources.
    # PS5.1 decodes native stdout using Console.OutputEncoding, independently
    # of Python's encoder. Both must agree for non-ASCII JSON qualification.
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
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
        # The RegressionSpec module must qualify twice from the installed tree
        # without importing the source checkout or contacting any provider.
        $regressionFirst = ((& $python -m ai_trading_companion.regression_spec) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed RegressionSpec qualification failed with exit code $LASTEXITCODE." }
        $regressionSecond = ((& $python -m ai_trading_companion.regression_spec) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed RegressionSpec replay qualification failed with exit code $LASTEXITCODE." }
        if ($regressionFirst -ne $regressionSecond) { throw 'Installed RegressionSpec qualification was not deterministic.' }
        if ($regressionFirst -match '"(aggregate|aggregate_score|overall_score|score|scores|weighted_score|weighted_average)"\s*:') { throw 'Installed RegressionSpec exposed a forbidden aggregate score.' }
        $regressionQualification = $regressionFirst | ConvertFrom-Json
        if ($regressionQualification.contract -ne 'RegressionSpecInstallQualification/v1' -or $regressionQualification.qualified -ne $true) {
            throw 'Installed RegressionSpec did not produce a qualified installation result.'
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            if ($regressionQualification.evaluation_vector.PSObject.Properties.Name -notcontains $axis) {
                throw "Installed RegressionSpec is missing evaluation axis: $axis"
            }
            if ($regressionQualification.evaluation_vector.$axis.passed -ne $true) {
                throw "Installed RegressionSpec evaluation axis did not qualify: $axis"
            }
        }
        # Validate the ObservabilitySpec event, independent evaluation vector, and
        # frozen replay twice from the installed runtime without contacting a provider.
        $observabilitySmoke = @'
import json
from ai_trading_companion.observability_contract import (
    build_evaluation_vector,
    build_event,
    frozen_replay,
    validate_evaluation_vector,
    validate_event,
    validate_replay,
)


def qualification():
    event = build_event(
        event_id='install-observability-event',
        event_type='actual_started',
        cycle_id='install-observability-cycle',
        task_key='install.observability.smoke',
        stage='m0',
        occurred_at='2026-10-06T01:00:00Z',
        known_at='2026-10-06T01:00:01Z',
        recorded_at='2026-10-06T01:00:02Z',
        source={'source': 'runtime', 'component': 'install-smoke', 'writer': 'install-smoke'},
        observations={'workload': {'retry_count': 0}},
        provenance={'source': 'install-smoke'},
    )
    vector = build_evaluation_vector(
        {
            axis: {'status': 'pass', 'measurements': {'measured': True}}
            for axis in (
                'delivery_speed', 'qualification_probability', 'research_quality',
                'judgment_outcome', 'safety_reliability',
            )
        },
        provenance={'source': 'install-smoke'},
    )
    replay = frozen_replay(event)
    validate_event(event)
    validate_evaluation_vector(vector)
    validate_replay(replay)
    return {'event': event, 'vector': vector, 'replay': replay}


first = qualification()
second = qualification()
if first != second:
    raise RuntimeError('Installed ObservabilitySpec smoke was not deterministic.')
print(json.dumps(first, sort_keys=True, separators=(',', ':')))
'@
        $observabilityFirst = ((& $python -c $observabilitySmoke) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed ObservabilitySpec smoke 1 failed with exit code $LASTEXITCODE." }
        $observabilitySecond = ((& $python -c $observabilitySmoke) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed ObservabilitySpec smoke 2 failed with exit code $LASTEXITCODE." }
        if ($observabilityFirst -ne $observabilitySecond) { throw 'Installed ObservabilitySpec smoke receipts were not deterministic.' }
        $observabilityQualification = $observabilityFirst | ConvertFrom-Json
        if ($observabilityQualification.event.contract -ne 'ObservabilitySpec/v1' -or
            $observabilityQualification.vector.contract -ne 'ObservabilityEvaluation/v1' -or
            $observabilityQualification.replay.contract -ne 'ObservabilitySpecReplay/v1') {
            throw 'Installed ObservabilitySpec smoke returned an unexpected contract.'
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
        $adapterReplayOne = ((& $python -m ai_trading_companion.adapter_contract) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AdapterContract replay 1 failed with exit code $LASTEXITCODE." }
        $adapterReplayTwo = ((& $python -m ai_trading_companion.adapter_contract) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed AdapterContract replay 2 failed with exit code $LASTEXITCODE." }
        if ($adapterReplayOne -ne $adapterReplayTwo) { throw 'Installed AdapterContract frozen replays were not deterministic.' }
        $adapterQualification = $adapterReplayOne | ConvertFrom-Json
        if ($adapterQualification.contract -ne 'AdapterContractInstallQualification/v1' -or $adapterQualification.qualified -ne $true) {
            throw 'Installed AdapterContract qualification did not pass.'
        }
        $mandateReplayOne = ((& $python -m ai_trading_companion.mandate_spec) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MandateSpec replay 1 failed with exit code $LASTEXITCODE." }
        $mandateReplayTwo = ((& $python -m ai_trading_companion.mandate_spec) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed MandateSpec replay 2 failed with exit code $LASTEXITCODE." }
        if ($mandateReplayOne -ne $mandateReplayTwo) { throw 'Installed MandateSpec frozen replays were not deterministic.' }
        $mandateQualification = $mandateReplayOne | ConvertFrom-Json
        if ($mandateQualification.contract -ne 'MandateSpecInstallQualification/v1' -or $mandateQualification.qualified -ne $true) {
            throw 'Installed MandateSpec qualification did not pass.'
        }
        foreach ($check in @('schema', 'frozen_replay', 'm1_blind', 'read_only')) {
            if ($mandateQualification.evaluation_vector.$check -ne $true) {
                throw "Installed MandateSpec qualification check failed: $check"
            }
        }
        $m0ObservationReplayOne = ((& $python -m ai_trading_companion.m0_observation) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed M0Observation replay 1 failed with exit code $LASTEXITCODE." }
        $m0ObservationReplayTwo = ((& $python -m ai_trading_companion.m0_observation) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed M0Observation replay 2 failed with exit code $LASTEXITCODE." }
        if ($m0ObservationReplayOne -ne $m0ObservationReplayTwo) { throw 'Installed M0Observation frozen replays were not deterministic.' }
        $m0ObservationQualification = $m0ObservationReplayOne | ConvertFrom-Json
        if ($m0ObservationQualification.contract -ne 'M0ObservationInstallQualification/v1' -or $m0ObservationQualification.qualified -ne $true) {
            throw 'Installed M0Observation qualification did not pass.'
        }
        foreach ($check in @('schema', 'facts_vs_inference', 'directional_language_blocked', 'unknowns_supported', 'frozen_replay', 'h0_m1_m2_isolation', 'quantresearch_read_only', 'write_permissions_empty')) {
            if ($m0ObservationQualification.evaluation_vector.$check -ne $true) {
                throw "Installed M0Observation qualification check failed: $check"
            }
        }
        $m1JudgmentReplayOne = ((& $python -m ai_trading_companion.m1_judgment) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed M1Judgment replay 1 failed with exit code $LASTEXITCODE." }
        $m1JudgmentReplayTwo = ((& $python -m ai_trading_companion.m1_judgment) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed M1Judgment replay 2 failed with exit code $LASTEXITCODE." }
        if ($m1JudgmentReplayOne -ne $m1JudgmentReplayTwo) { throw 'Installed M1Judgment frozen replays were not deterministic.' }
        $m1JudgmentQualification = $m1JudgmentReplayOne | ConvertFrom-Json
        if ($m1JudgmentQualification.contract -ne 'M1JudgmentInstallQualification/v1' -or $m1JudgmentQualification.qualified -ne $true) {
            throw 'Installed M1Judgment qualification did not pass.'
        }
        $positionSafetyReplayOne = ((& $python -m ai_trading_companion.position_safety) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed PositionSafety replay 1 failed with exit code $LASTEXITCODE." }
        $positionSafetyReplayTwo = ((& $python -m ai_trading_companion.position_safety) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed PositionSafety replay 2 failed with exit code $LASTEXITCODE." }
        if ($positionSafetyReplayOne -ne $positionSafetyReplayTwo) { throw 'Installed PositionSafety frozen replays were not deterministic.' }
        $positionSafetyQualification = $positionSafetyReplayOne | ConvertFrom-Json
        if ($positionSafetyQualification.contract -ne 'PositionSafetyInstallQualification/v1' -or $positionSafetyQualification.qualified -ne $true) {
            throw 'Installed PositionSafety qualification did not pass.'
        }
        foreach ($check in @('frozen_replay', 'precise_qualified', 'stale_assets_refused', 'llm_order_refused', 'llm_write_refused', 'quantresearch_read_only', 'quantresearch_write_refused')) {
            if ($positionSafetyQualification.checks.$check -ne $true) { throw "Installed PositionSafety check failed: $check" }
        }
        # RiskGate requalification is read-only and preserves the original packet,
        # evidence, model output and receipt. Fixed fixtures do not measure live axes.
        $riskGateReplayOne = ((& $python -m ai_trading_companion.risk_gate) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed RiskGate replay 1 failed with exit code $LASTEXITCODE." }
        $riskGateReplayTwo = ((& $python -m ai_trading_companion.risk_gate) -join "`n").Trim()
        if ($LASTEXITCODE -ne 0) { throw "Installed RiskGate replay 2 failed with exit code $LASTEXITCODE." }
        if ($riskGateReplayOne -ne $riskGateReplayTwo) { throw 'Installed RiskGate frozen replays were not deterministic.' }
        $riskGateQualification = $riskGateReplayOne | ConvertFrom-Json
        if ($riskGateQualification.contract -ne 'RiskGateInstallQualification/v1' -or $riskGateQualification.qualified -ne $true) {
            throw 'Installed RiskGate qualification did not pass.'
        }
        foreach ($check in @('frozen_replay', 'history_preserved', 'precise_qualified', 'source_unavailable_refused', 'critical_conflict_refused', 'stale_assets_refused', 'leverage_refused', 'write_refused', 'historical_not_upgraded', 'read_only')) {
            if ($riskGateQualification.checks.$check -ne $true) { throw "Installed RiskGate check failed: $check" }
        }
        foreach ($axis in @('delivery_speed', 'qualification_probability', 'research_quality', 'judgment_outcome', 'safety_reliability')) {
            $evaluation = $riskGateQualification.evaluation_vector.$axis
            if ($null -eq $evaluation -or $null -eq $evaluation.measurements -or $evaluation.measurements.measured -isnot [bool]) {
                throw "Installed RiskGate axis lacks structured measurements: $axis"
            }
            if ($axis -eq 'safety_reliability') {
                if ($evaluation.status -ne 'pass' -or $evaluation.measurements.measured -ne $true) {
                    throw 'Installed RiskGate measured safety checks failed.'
                }
            }
            elseif ($evaluation.status -ne 'not_measured' -or $evaluation.measurements.measured -ne $false -or
                    [string]::IsNullOrWhiteSpace([string]$evaluation.reason)) {
                throw "Installed RiskGate must not claim a live measurement from fixed fixtures: $axis"
            }
        }
        if ($riskGateReplayOne -match '"(aggregate|aggregate_score|composite_score|overall_score|score|scores|total_score|weighted_score|weighted_average)"\s*:') {
            throw 'Installed RiskGate exposed a forbidden aggregate score.'
        }
        if ($riskGateQualification.source_unavailable_smoke.requalification.state -ne 'refused' -or
            $riskGateQualification.source_unavailable_smoke.requalification.problems -notcontains 'market_evidence_missing') {
            throw 'Installed RiskGate accepted source-unavailable advice.'
        }
        $researchIsolationReplayOne = ((& $python -m ai_trading_companion.research_isolation) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed ResearchIsolation replay 1 failed with exit code $LASTEXITCODE." }
        $researchIsolationReplayTwo = ((& $python -m ai_trading_companion.research_isolation) -join "`n")
        if ($LASTEXITCODE -ne 0) { throw "Installed ResearchIsolation replay 2 failed with exit code $LASTEXITCODE." }
        if ($researchIsolationReplayOne -ne $researchIsolationReplayTwo) { throw 'Installed ResearchIsolation qualification was not deterministic.' }
        $researchIsolationQualification = $researchIsolationReplayOne | ConvertFrom-Json
        if ($researchIsolationQualification.contract -ne 'ResearchIsolationInstallQualification/v1' -or $researchIsolationQualification.qualified -ne $true) {
            throw 'Installed ResearchIsolation qualification did not pass.'
        }
        foreach ($check in @('versioned_evidence', 'read_only_port', 'write_attempt_rejected', 'frozen_replay', 'm1_evidence_only', 'no_auto_override', 'no_auto_promotion')) {
            if ($researchIsolationQualification.evaluation_vector.$check -ne $true) {
                throw "Installed ResearchIsolation qualification check failed: $check"
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
    [Console]::OutputEncoding = $previousConsoleOutputEncoding
    if ($null -eq $previousPythonIOEncoding) {
        Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTHONIOENCODING = $previousPythonIOEncoding
    }
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
