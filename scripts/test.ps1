[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$ProjectRegression,
    [ValidateSet('all', 'EvidenceSpec', 'MemoryType', 'MemoryWrite', 'Reflection', 'AnalysisSkill', 'SkillRegistry', 'AdapterContract', 'FinRobotAdapter', 'FinGPTAdapter', 'MultimodalAdapter', 'MandateSpec', 'AuditSpec', 'ObservabilitySpec', 'M0Observation', 'M1Judgment', 'M2Synthesis', 'PositionSafety', 'ResearchIsolation', 'RegressionSpec')]
    [string]$Select = 'all'
)

$ErrorActionPreference = 'Stop'
if ($ProjectRegression -and $Select -ne 'all') {
    throw 'ProjectRegression requires the complete regression suite; Select must be all.'
}
$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = "$root\memoryhub\src;$root\src\runtime" + $(if ($env:PYTHONPATH) { ";$env:PYTHONPATH" } else { "" })

if ($ProjectRegression) {
    # The project-regression gate is the smallest deterministic suite that
    # exercises the decision-cycle contract, frozen replay, recovery and the
    # two installed qualification paths. Keep it independent of the full
    # MemoryHub and desktop suites so it can be used by ticket automation.
    $regressionTests = @(
        "$root\tests\runtime\test_cycle_contract.py",
        "$root\tests\runtime\test_cycle_replay.py",
        "$root\tests\runtime\test_evidence_spec.py",
        "$root\tests\runtime\test_memory_type.py",
        "$root\memoryhub\tests\test_memory_types.py",
        "$root\tests\runtime\test_memory_write.py",
        "$root\memoryhub\tests\test_append_only_ledger.py",
        "$root\tests\runtime\test_reflection.py",
        "$root\tests\runtime\test_analysis_skill.py",
        "$root\tests\runtime\test_skill_registry.py",
        "$root\tests\runtime\test_adapter_contract.py",
        "$root\tests\runtime\test_finrobot_adapter.py",
        "$root\tests\runtime\test_fingpt_adapter.py",
        "$root\tests\runtime\test_multimodal_adapter.py",
        "$root\tests\runtime\test_evidence_gate.py",
        "$root\tests\runtime\test_memory_evidence_gate.py",
        "$root\tests\runtime\test_evidence_qualification.py",
        "$root\tests\runtime\test_evidence_snapshot.py",
        "$root\tests\runtime\test_temporal_integrity.py",
        "$root\tests\runtime\test_judgment_publication.py",
        "$root\tests\runtime\test_decision_cycle.py",
        "$root\tests\runtime\test_agent_role.py",
        "$root\tests\runtime\test_agent_contract.py",
        "$root\tests\runtime\test_mandate_spec.py",
        "$root\tests\runtime\test_audit_contract.py",
        "$root\tests\runtime\test_observability_contract.py",
        "$root\tests\runtime\test_m0_observation.py",
        "$root\tests\runtime\test_m1_judgment.py",
        "$root\tests\runtime\test_m2_judgment.py",
        "$root\tests\runtime\test_m2_portfolio_fact_gate.py",
        "$root\tests\runtime\test_position_safety.py",
        "$root\tests\runtime\test_research_isolation.py",
        "$root\tests\runtime\test_regression_gate.py",
        "$root\tests\runtime\test_companion_exchange.py",
        "$root\tests\runtime\test_debate.py",
        "$root\tests\runtime\test_preview.py"
    )
    py -m pytest $regressionTests -q
    if ($LASTEXITCODE -ne 0) { throw "Project regression tests failed with exit code $LASTEXITCODE." }
    exit 0
}

if ($Select -eq 'all') {
    py -m pytest "$root\memoryhub\tests" -q
    if ($LASTEXITCODE -ne 0) { throw "MemoryHub tests failed with exit code $LASTEXITCODE." }
    py -m pytest "$root\tests\runtime" -q
    if ($LASTEXITCODE -ne 0) { throw "Runtime tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'EvidenceSpec') {
    py -m pytest "$root\tests\runtime\test_evidence_spec.py" "$root\tests\runtime\test_evidence_qualification.py" "$root\tests\runtime\test_evidence_snapshot.py" "$root\tests\runtime\test_evidence_gate.py" "$root\tests\runtime\test_memory_evidence_gate.py" -q
    if ($LASTEXITCODE -ne 0) { throw "EvidenceSpec regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'MemoryType') {
    py -m pytest "$root\tests\runtime\test_memory_type.py" "$root\memoryhub\tests\test_memory_types.py" -q
    if ($LASTEXITCODE -ne 0) { throw "MemoryType regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'Reflection') {
    py -m pytest "$root\tests\runtime\test_reflection.py" -q
    if ($LASTEXITCODE -ne 0) { throw "Reflection regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'AnalysisSkill') {
    py -m pytest "$root\tests\runtime\test_analysis_skill.py" -q
    if ($LASTEXITCODE -ne 0) { throw "AnalysisSkill regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'SkillRegistry') {
    py -m pytest "$root\tests\runtime\test_skill_registry.py" -q
    if ($LASTEXITCODE -ne 0) { throw "SkillRegistry regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'AdapterContract') {
    py -m pytest "$root\tests\runtime\test_adapter_contract.py" -q
    if ($LASTEXITCODE -ne 0) { throw "AdapterContract regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'FinRobotAdapter') {
    py -m pytest "$root\tests\runtime\test_finrobot_adapter.py" -q
    if ($LASTEXITCODE -ne 0) { throw "FinRobotAdapter regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'FinGPTAdapter') {
    py -m pytest "$root\tests\runtime\test_fingpt_adapter.py" -q
    if ($LASTEXITCODE -ne 0) { throw "FinGPTAdapter regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'MultimodalAdapter') {
    py -m pytest "$root\tests\runtime\test_multimodal_adapter.py" -q
    if ($LASTEXITCODE -ne 0) { throw "MultimodalAdapter regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'MandateSpec') {
    py -m pytest "$root\tests\runtime\test_mandate_spec.py" -q
    if ($LASTEXITCODE -ne 0) { throw "MandateSpec regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'AuditSpec') {
    py -m pytest "$root\tests\runtime\test_audit_contract.py" -q
    if ($LASTEXITCODE -ne 0) { throw "AuditSpec regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'ObservabilitySpec') {
    py -m pytest "$root\tests\runtime\test_observability_contract.py" -q
    if ($LASTEXITCODE -ne 0) { throw "ObservabilitySpec regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'M0Observation') {
    py -m pytest "$root\tests\runtime\test_m0_observation.py" -q
    if ($LASTEXITCODE -ne 0) { throw "M0Observation regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'M1Judgment') {
    py -m pytest "$root\tests\runtime\test_m1_judgment.py" -q
    if ($LASTEXITCODE -ne 0) { throw "M1Judgment regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'M2Synthesis') {
    py -m pytest "$root\tests\runtime\test_m2_judgment.py" "$root\tests\runtime\test_m2_portfolio_fact_gate.py" -q
    if ($LASTEXITCODE -ne 0) { throw "M2Synthesis regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'PositionSafety') {
    py -m pytest "$root\tests\runtime\test_position_safety.py" -q
    if ($LASTEXITCODE -ne 0) { throw "PositionSafety regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'ResearchIsolation') {
    py -m pytest "$root\tests\runtime\test_research_isolation.py" -q
    if ($LASTEXITCODE -ne 0) { throw "ResearchIsolation regression tests failed with exit code $LASTEXITCODE." }
}
elseif ($Select -eq 'RegressionSpec') {
    py -m pytest "$root\tests\runtime\test_regression_gate.py" -q
    if ($LASTEXITCODE -ne 0) { throw "RegressionSpec regression tests failed with exit code $LASTEXITCODE." }
}
else {
    py -m pytest "$root\tests\runtime\test_memory_write.py" "$root\memoryhub\tests\test_append_only_ledger.py" -q
    if ($LASTEXITCODE -ne 0) { throw "MemoryWrite regression tests failed with exit code $LASTEXITCODE." }
}
dotnet test "$root\AITradingCompanion.sln" $(if ($Release) { '--configuration'; 'Release' }) --nologo
if ($LASTEXITCODE -ne 0) { throw "Desktop tests failed with exit code $LASTEXITCODE." }
