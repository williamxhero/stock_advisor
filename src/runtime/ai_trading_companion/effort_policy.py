from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


EFFORT_ORDER = ("medium", "high", "xhigh")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class EffortPolicyFacts:
    cell_key: str
    family: str
    stage: str
    major: bool
    evidence_gaps: int
    source_count: int
    source_conflicts: int
    high_impact_events: int
    data_blocked: bool
    deadline_seconds: int
    dependency_health: str = "unknown"
    market_regime: str = "unknown"


@dataclass(frozen=True)
class EffortDecision:
    policy_version: str
    input_fingerprint: str
    baseline_effort: str
    candidate_effort: str | None
    selected_effort: str
    mode: str
    stratum: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class EffortCandidate:
    policy_version: str
    input_fingerprint: str
    effort: str
    stratum: str
    reason_codes: tuple[str, ...]


DEFAULT_POLICY: dict[str, Any] = {
    "version": "cognitive-effort/v1",
    "baseline_by_family": {"research": "medium", "judgment": "medium", "expression": "medium"},
    "candidate_rules": {
        "deadline_tight": {"maximum_deadline_seconds": 90},
        "major_judgment": {"effort": "xhigh", "minimum_deadline_seconds": 240},
        "evidence_pressure": {"effort": "high", "minimum_gaps": 2, "minimum_conflicts": 1, "minimum_sources": 3},
        "dependency_degraded": {"effort": "high"},
    },
}


class CognitiveEffortPolicy:
    """Select effort from frozen task facts; provider configuration is not an input."""

    def __init__(self, document: dict[str, Any]) -> None:
        self.document = json.loads(json.dumps(document, ensure_ascii=False, sort_keys=True))
        self._validate()

    @classmethod
    def bootstrap(cls) -> "CognitiveEffortPolicy":
        return cls(DEFAULT_POLICY)

    @classmethod
    def load(cls, store: Any) -> "CognitiveEffortPolicy":
        """Load the active version, creating the explicit bootstrap version once."""
        with store.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS cognitive_effort_policy (
                  policy_version TEXT PRIMARY KEY,
                  policy_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  previous_policy_version TEXT,
                  evidence_snapshot_id TEXT,
                  created_at TEXT NOT NULL,
                  activated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_cognitive_effort_policy_active
                  ON cognitive_effort_policy(state) WHERE state='active';
            """)
            row = connection.execute(
                "SELECT policy_json FROM cognitive_effort_policy WHERE state='active' ORDER BY activated_at DESC LIMIT 1",
            ).fetchone()
            if row is None:
                payload = json.dumps(DEFAULT_POLICY, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                at = _now()
                connection.execute(
                    "INSERT INTO cognitive_effort_policy(policy_version,policy_json,state,created_at,activated_at) VALUES(?,?,'active',?,?)",
                    (DEFAULT_POLICY["version"], payload, at, at),
                )
                return cls(DEFAULT_POLICY)
            return cls(json.loads(row["policy_json"]))

    @property
    def version(self) -> str:
        return str(self.document["version"])

    def select(self, facts: EffortPolicyFacts, *, mode: str = "shadow") -> EffortDecision:
        if mode not in {"shadow", "promoted", "rolled_back"}:
            raise ValueError(f"unsupported effort policy mode: {mode}")
        baseline = str(self.document["baseline_by_family"].get(facts.family, "medium"))
        candidate, stratum, reasons = self._candidate(facts, baseline)
        selected = candidate if mode == "promoted" and candidate else baseline
        payload = json.dumps(asdict(facts), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(f"{self.version}|{payload}".encode("utf-8")).hexdigest()
        return EffortDecision(
            policy_version=self.version, input_fingerprint=fingerprint,
            baseline_effort=baseline, candidate_effort=candidate,
            selected_effort=selected, mode=mode, stratum=stratum, reason_codes=tuple(reasons),
        )

    def propose_shadow(self, decision: EffortDecision) -> EffortCandidate | None:
        if decision.candidate_effort is None:
            return None
        return EffortCandidate(
            policy_version=decision.policy_version,
            input_fingerprint=decision.input_fingerprint,
            effort=decision.candidate_effort,
            stratum=decision.stratum,
            reason_codes=decision.reason_codes,
        )

    def _candidate(self, facts: EffortPolicyFacts, baseline: str) -> tuple[str | None, str, list[str]]:
        if facts.data_blocked:
            return None, "data_blocked", ["data_blocked_fail_closed"]
        rules = self.document["candidate_rules"]
        tight = rules["deadline_tight"]
        if facts.deadline_seconds <= int(tight["maximum_deadline_seconds"]):
            return None, "deadline_tight", ["deadline_tight_no_verified_fallback"]
        major = rules["major_judgment"]
        if facts.family == "judgment" and facts.major and facts.deadline_seconds >= int(major["minimum_deadline_seconds"]):
            return self._different(str(major["effort"]), baseline), "major", ["major_judgment", "deadline_allows_deeper_effort"]
        pressure = rules["evidence_pressure"]
        pressured = (
            facts.evidence_gaps >= int(pressure["minimum_gaps"])
            or facts.source_conflicts >= int(pressure["minimum_conflicts"])
            or facts.source_count < int(pressure["minimum_sources"])
        )
        if pressured:
            return self._different(str(pressure["effort"]), baseline), "evidence_sparse", ["evidence_pressure"]
        if facts.dependency_health == "degraded":
            degraded = str(rules["dependency_degraded"]["effort"])
            return self._different(degraded, baseline), "evidence_sparse", ["dependency_degraded"]
        return None, "routine", ["baseline_sufficient"]

    @staticmethod
    def _different(candidate: str, baseline: str) -> str | None:
        return candidate if candidate != baseline else None

    def _validate(self) -> None:
        if not isinstance(self.document.get("version"), str) or not self.document["version"]:
            raise ValueError("effort policy requires a version")
        baselines = self.document.get("baseline_by_family")
        rules = self.document.get("candidate_rules")
        if not isinstance(baselines, dict) or not isinstance(rules, dict):
            raise ValueError("effort policy is incomplete")
        efforts = list(baselines.values()) + [
            row["effort"] for row in rules.values() if isinstance(row, dict) and "effort" in row
        ]
        if any(effort not in EFFORT_ORDER for effort in efforts):
            raise ValueError("effort policy contains an unsupported effort")
