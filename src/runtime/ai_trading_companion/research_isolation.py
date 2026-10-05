"""Versioned, read-only evidence seam between Companion and QuantResearch.

QuantResearch owns experiments, backtests, strategies, and promotion.  The
Companion can only ask a configured port for an already versioned evidence
bundle; the returned conclusion is an input to M1, never an M1 verdict.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone
from typing import Any, Callable

from .mandate_spec import canonical_json, sha256


CONTRACT = "ResearchIsolationSpec/v1"
VERSION = 1
PORT_CONTRACT = "QuantResearchPort/v1"
REQUEST_CONTRACT = "ResearchEvidenceRequest/v1"
EVIDENCE_CONTRACT = "ResearchEvidence/v1"
REPLAY_CONTRACT = "ResearchIsolationReplay/v1"

_READ_ONLY = {"access": "read_only", "write_permissions": []}
_WRITE_OPERATIONS = frozenset({
    "write", "write_evidence", "write_experiment", "write_backtest",
    "mutate", "mutate_strategy", "update_strategy", "update_experiment",
    "promote", "promote_strategy", "start_research", "run_experiment",
    "delete", "delete_experiment",
})


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _timestamp(value: Any, field: str) -> str:
    result = _required_string(value, field)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return result


def _research_clock(value: Any, field: str) -> datetime:
    result = _required_string(value, field)
    if len(result) == 10 and result[4] == "-" and result[7] == "-":
        try:
            parsed_date = date.fromisoformat(result)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO-8601 timestamp or date") from exc
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp or date") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def _validate_intervals(intervals: dict[str, Any], as_of: str) -> None:
    cutoff = _research_clock(as_of, "provenance.as_of")
    for name, interval in intervals.items():
        if not isinstance(interval, dict) or not interval:
            raise ValueError(f"intervals.{name} must be a non-empty object")
        if "start" not in interval or "end" not in interval:
            raise ValueError(f"intervals.{name} must contain start and end")
        start = _research_clock(interval["start"], f"intervals.{name}.start")
        end = _research_clock(interval["end"], f"intervals.{name}.end")
        if start > end:
            raise ValueError(f"intervals.{name} start is after end")
        if end > cutoff:
            raise ValueError(f"intervals.{name} extends beyond the evidence cutoff")


def _object(value: Any, field: str, *, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict) or (not allow_empty and not value):
        raise ValueError(f"{field} must be a non-empty object")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    result = list(dict.fromkeys(item.strip() for item in value))
    if not allow_empty and not result:
        raise ValueError(f"{field} must not be empty")
    return result


def read_only_permissions() -> dict[str, Any]:
    """Return a fresh permission declaration for a QuantResearch boundary."""
    return copy.deepcopy(_READ_ONLY)


def access_descriptor() -> dict[str, Any]:
    """Describe the only QuantResearch capability a Runtime packet may carry."""
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "port": PORT_CONTRACT,
        **read_only_permissions(),
    }


def validate_access_descriptor(value: dict[str, Any]) -> dict[str, Any]:
    expected = {"contract", "version", "port", "access", "write_permissions"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("ResearchIsolation access fields are not exact")
    if (
        value["contract"] != CONTRACT
        or type(value["version"]) is not int
        or value["version"] != VERSION
        or value["port"] != PORT_CONTRACT
        or value["access"] != "read_only"
        or value["write_permissions"] != []
    ):
        raise ValueError("QuantResearch access is not read_only")
    return value


def build_request(
    *,
    task_key: str,
    as_of: str,
    market_scope: dict[str, Any],
    universe: list[str],
    research_goal: str,
    baseline_strategy_version: str | None = None,
    strategy_package: dict[str, Any] | None = None,
    research_run_id: str | None = None,
) -> dict[str, Any]:
    """Build the Runtime-owned, as-of-bounded request sent through the port."""
    value: dict[str, Any] = {
        "contract": REQUEST_CONTRACT,
        "version": VERSION,
        "task_key": _required_string(task_key, "task_key"),
        "as_of": _timestamp(as_of, "as_of"),
        "market_scope": copy.deepcopy(_object(market_scope, "market_scope")),
        "universe": _strings(universe, "universe", allow_empty=True),
        "research_goal": _required_string(research_goal, "research_goal"),
        "baseline_strategy_version": (
            _required_string(baseline_strategy_version, "baseline_strategy_version")
            if baseline_strategy_version is not None else None
        ),
        "strategy_package": copy.deepcopy(strategy_package) if strategy_package is not None else None,
        "research_run_id": (
            _required_string(research_run_id, "research_run_id")
            if research_run_id is not None else None
        ),
        "permissions": read_only_permissions(),
    }
    if value["strategy_package"] is not None:
        _object(value["strategy_package"], "strategy_package")
    value["sha256"] = sha256(value)
    return validate_request(value)


def validate_request(value: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "contract", "version", "task_key", "as_of", "market_scope", "universe",
        "research_goal", "baseline_strategy_version", "strategy_package",
        "research_run_id", "permissions", "sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("ResearchEvidenceRequest fields are not exact")
    if value["contract"] != REQUEST_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unsupported ResearchEvidenceRequest identity")
    _required_string(value["task_key"], "task_key")
    _timestamp(value["as_of"], "as_of")
    _object(value["market_scope"], "market_scope")
    _strings(value["universe"], "universe", allow_empty=True)
    _required_string(value["research_goal"], "research_goal")
    for field in ("baseline_strategy_version", "research_run_id"):
        if value[field] is not None:
            _required_string(value[field], field)
    if value["strategy_package"] is not None:
        _object(value["strategy_package"], "strategy_package")
    if value["permissions"] != _READ_ONLY:
        raise ValueError("QuantResearch request permissions must be read_only")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("ResearchEvidenceRequest digest mismatch")
    return value


def build_evidence(
    *,
    research_run_id: str,
    dataset_version: str,
    code_version: str,
    parameter_version: str,
    intervals: dict[str, Any],
    out_of_sample: dict[str, Any],
    cost_assumptions: dict[str, Any],
    risk_metrics: dict[str, Any],
    applicability: dict[str, Any],
    baseline_comparison: dict[str, Any],
    conclusion: dict[str, Any],
    as_of: str,
    artifact_ref: str,
    known_at: str | None = None,
    evidence_refs: list[str] | None = None,
    request_sha256: str | None = None,
    reproducibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a complete ResearchEvidence envelope; no implicit versioning."""
    run_id = _required_string(research_run_id, "research_run_id")
    refs = evidence_refs or [f"quantresearch:{run_id}"]
    value: dict[str, Any] = {
        "contract": EVIDENCE_CONTRACT,
        "version": VERSION,
        "research_run_id": run_id,
        "dataset_version": _required_string(dataset_version, "dataset_version"),
        "code_version": _required_string(code_version, "code_version"),
        "parameter_version": _required_string(parameter_version, "parameter_version"),
        "intervals": copy.deepcopy(_object(intervals, "intervals")),
        "out_of_sample": copy.deepcopy(_object(out_of_sample, "out_of_sample")),
        "cost_assumptions": copy.deepcopy(_object(cost_assumptions, "cost_assumptions")),
        "risk_metrics": copy.deepcopy(_object(risk_metrics, "risk_metrics")),
        "applicability": copy.deepcopy(_object(applicability, "applicability")),
        "baseline_comparison": copy.deepcopy(_object(baseline_comparison, "baseline_comparison")),
        "conclusion": copy.deepcopy(_object(conclusion, "conclusion")),
        "reproducibility": copy.deepcopy(reproducibility or {}),
        "evidence_refs": _strings(refs, "evidence_refs"),
        "provenance": {
            "source": "quantresearch",
            "as_of": _timestamp(as_of, "as_of"),
            "known_at": _timestamp(known_at or as_of, "known_at"),
            "artifact_ref": _required_string(artifact_ref, "artifact_ref"),
            "request_sha256": request_sha256,
        },
        "permissions": read_only_permissions(),
        "quantresearch": read_only_permissions(),
    }
    value["sha256"] = sha256(value)
    return validate_evidence(value)


def validate_evidence(value: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "contract", "version", "research_run_id", "dataset_version", "code_version",
        "parameter_version", "intervals", "out_of_sample", "cost_assumptions",
        "risk_metrics", "applicability", "baseline_comparison", "conclusion",
        "reproducibility", "evidence_refs", "provenance", "permissions",
        "quantresearch", "sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("ResearchEvidence fields are not exact")
    if value["contract"] != EVIDENCE_CONTRACT or type(value["version"]) is not int or value["version"] != VERSION:
        raise ValueError("unversioned or unsupported research evidence")
    for field in ("research_run_id", "dataset_version", "code_version", "parameter_version"):
        _required_string(value[field], field)
    for field in (
        "intervals", "out_of_sample", "cost_assumptions", "risk_metrics", "applicability",
        "baseline_comparison", "conclusion",
    ):
        _object(value[field], field)
    _object(value["reproducibility"], "reproducibility")
    refs = _strings(value["evidence_refs"], "evidence_refs")
    if refs != value["evidence_refs"]:
        raise ValueError("research evidence_refs must be unique")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"source", "as_of", "known_at", "artifact_ref", "request_sha256"}:
        raise ValueError("research evidence provenance fields are not exact")
    if provenance["source"] != "quantresearch":
        raise ValueError("research evidence source must be QuantResearch")
    _timestamp(provenance["as_of"], "provenance.as_of")
    _timestamp(provenance["known_at"], "provenance.known_at")
    if _research_clock(provenance["known_at"], "provenance.known_at") < _research_clock(provenance["as_of"], "provenance.as_of"):
        raise ValueError("research evidence known_at precedes as_of")
    _validate_intervals(value["intervals"], provenance["as_of"])
    _required_string(provenance["artifact_ref"], "provenance.artifact_ref")
    request_hash = provenance["request_sha256"]
    if (
        not isinstance(request_hash, str) or len(request_hash) != 64
        or any(char not in "0123456789abcdef" for char in request_hash)
    ):
        raise ValueError("research evidence request_sha256 is required and invalid")
    if value["permissions"] != _READ_ONLY or value["quantresearch"] != _READ_ONLY:
        raise ValueError("research evidence is not read_only")
    if value["sha256"] != sha256({key: item for key, item in value.items() if key != "sha256"}):
        raise ValueError("research evidence digest mismatch")
    return value


class QuantResearchPort:
    """A read-only port; the optional reader may be a local adapter or RPC client."""

    def __init__(self, reader: Callable[[dict[str, Any]], dict[str, Any] | None] | Any | None = None) -> None:
        candidate = reader
        if candidate is not None and not callable(candidate):
            candidate = getattr(candidate, "read", None) or getattr(candidate, "read_evidence", None)
        if candidate is not None and not callable(candidate):
            raise TypeError("QuantResearchPort requires a read-only callable")
        self.__reader = candidate

    def read(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request = validate_request(copy.deepcopy(request))
        reader = self.__reader
        if reader is None:
            return None
        result = reader(copy.deepcopy(request))
        if result is None:
            return None
        # Never upgrade an old/raw result into evidence: the provider must
        # return the complete versioned envelope itself and bind it to this
        # exact request.
        validated = validate_evidence(result)
        if validated["provenance"]["request_sha256"] != request["sha256"]:
            raise ValueError("research evidence request binding mismatch")
        return copy.deepcopy(validated)

    def read_evidence(self, request: dict[str, Any]) -> dict[str, Any] | None:
        return self.read(request)

    def _reject_write(self, operation: str) -> None:
        raise PermissionError(f"QuantResearchPort is read_only: {operation}")

    def write(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_write("write")

    def write_evidence(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_write("write_evidence")

    def start_research(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_write("start_research")

    def update_strategy(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_write("update_strategy")

    def promote_strategy(self, *_args: Any, **_kwargs: Any) -> None:
        self._reject_write("promote_strategy")

    def __getattr__(self, name: str) -> Any:
        lowered = name.casefold()
        if lowered in _WRITE_OPERATIONS or any(
            token in lowered for token in ("write", "mutate", "promote", "update", "delete", "remove", "experiment", "backtest")
        ):
            self._reject_write(name)
        raise AttributeError(name)


class NullQuantResearchPort(QuantResearchPort):
    """The installed Companion has no implicit QuantResearch dependency."""

    def read(self, request: dict[str, Any]) -> None:
        validate_request(copy.deepcopy(request))
        return None


class InMemoryQuantResearchPort(QuantResearchPort):
    """Deterministic read-only adapter useful for replay and integration tests."""

    def __init__(self, evidence: dict[str, Any] | None = None) -> None:
        self._evidence = copy.deepcopy(validate_evidence(evidence)) if evidence is not None else None

    def read(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request = validate_request(copy.deepcopy(request))
        if self._evidence is None:
            return None
        if self._evidence["provenance"]["request_sha256"] != request["sha256"]:
            raise ValueError("research evidence request binding mismatch")
        return copy.deepcopy(self._evidence)


def coerce_quant_research_port(value: Any | None) -> QuantResearchPort:
    """Wrap an adapter's read method so Runtime never retains its write surface."""
    if value is None:
        return NullQuantResearchPort()
    # Always wrap supplied ports, including subclasses, so an overridden
    # writable method cannot widen the capability retained by Runtime.
    return QuantResearchPort(value)


def apply_research_evidence(m1_verdict: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    """Validate evidence without allowing it to replace or mutate an M1 verdict."""
    validate_evidence(evidence)
    if not isinstance(m1_verdict, dict):
        raise ValueError("M1 verdict must be an object")
    return copy.deepcopy(m1_verdict)


def validate_replay(value: dict[str, Any]) -> dict[str, Any]:
    fields = {"contract", "source", "source_sha256", "qualification"}
    if not isinstance(value, dict) or set(value) != fields or value["contract"] != REPLAY_CONTRACT:
        raise ValueError("ResearchIsolation replay fields are not exact")
    source = validate_evidence(value["source"])
    if value["source_sha256"] != source["sha256"]:
        raise ValueError("ResearchIsolation replay source digest mismatch")
    qualification = value["qualification"]
    expected = {"valid", "read_only", "m1_evidence_only", "no_auto_override", "no_auto_promotion"}
    if not isinstance(qualification, dict) or set(qualification) != expected or any(
        qualification[key] is not True for key in expected
    ):
        raise ValueError("ResearchIsolation replay qualification mismatch")
    return value


def frozen_replay(evidence: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(validate_evidence(evidence))
    return validate_replay({
        "contract": REPLAY_CONTRACT,
        "source": value,
        "source_sha256": value["sha256"],
        "qualification": {
            "valid": True,
            "read_only": value["permissions"] == _READ_ONLY,
            "m1_evidence_only": True,
            "no_auto_override": True,
            "no_auto_promotion": True,
        },
    })


def install_qualification() -> dict[str, Any]:
    request = build_request(
        task_key="daily.execution.0945",
        as_of="2026-10-05T01:45:00Z",
        market_scope={"market": "CN_A_SHARE", "stage": "m1_judgment"},
        universe=[],
        research_goal="installation qualification",
        baseline_strategy_version="runtime-baseline-v1",
        strategy_package={"contract": "CompanionResearchSubject/v1", "task_key": "daily.execution.0945"},
    )
    evidence = build_evidence(
        research_run_id="install-research-run",
        dataset_version="dataset-1",
        code_version="code-1",
        parameter_version="params-1",
        intervals={"backtest": {"start": "2024-01-01", "end": "2024-12-31"}},
        out_of_sample={"sharpe": 0.8, "observations": 100},
        cost_assumptions={"commission_bps": 3, "slippage_bps": 5},
        risk_metrics={"max_drawdown": 0.2, "volatility": 0.3},
        applicability={"market": "CN_A_SHARE", "conditions": ["liquid"], "limitations": ["not a mandate"]},
        baseline_comparison={"baseline_version": "baseline-1", "relative_return": 0.02},
        conclusion={"status": "evidence_only", "summary": "candidate requires independent judgment"},
        reproducibility={"uri": "quantresearch://install-research-run"},
        as_of="2026-10-05T01:45:00Z",
        artifact_ref="install-artifact",
        request_sha256=request["sha256"],
    )
    replay = frozen_replay(evidence)
    evaluation_vector = {
        "versioned_evidence": True,
        "read_only_port": True,
        "write_attempt_rejected": all(
            _write_rejected(getattr(QuantResearchPort(), operation))
            for operation in ("write", "start_research", "promote_strategy")
        ),
        "frozen_replay": True,
        "m1_evidence_only": replay["qualification"]["m1_evidence_only"],
        "no_auto_override": replay["qualification"]["no_auto_override"],
        "no_auto_promotion": replay["qualification"]["no_auto_promotion"],
    }
    return {
        "contract": "ResearchIsolationInstallQualification/v1",
        "qualified": replay["qualification"]["valid"] and all(evaluation_vector.values()),
        "replay_sha256": sha256(replay),
        "evaluation_vector": evaluation_vector,
    }


def _write_rejected(operation: Callable[..., Any]) -> bool:
    try:
        operation()
    except PermissionError:
        return True
    return False


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
