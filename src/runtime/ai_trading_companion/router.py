from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from .config import DEFAULT_PROVIDER


RESEARCH_STAGES = frozenset({"m0_research", "m1_research", "outcome_research", "chat_research"})
JUDGMENT_STAGES = frozenset({"m1_judgment", "m2", "reflection", "workflow_feedback"})
MAJOR_TASKS = frozenset({"daily.execution.1430", "daily.review.1520", "periodic.monthly", "periodic.quarterly", "periodic.annual"})


@dataclass(frozen=True)
class RoutingDecision:
    model: str
    reasoning_effort: str
    search: bool
    timeout_seconds: int
    reason: str
    model_slot: str = "fast"

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CognitiveTaskProfile:
    family: str
    cell_key: str
    major: bool
    evidence_gaps: int
    source_count: int
    source_conflicts: int
    high_impact_events: int
    data_blocked: bool
    deadline_seconds: int
    m1_blind: bool

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingPlan:
    profile: CognitiveTaskProfile
    baseline: RoutingDecision
    selected: RoutingDecision
    candidate: RoutingDecision | None
    mode: str


class CognitiveRouter:
    """Select configured Provider model slots without a consumption quota."""

    def __init__(self, provider: dict[str, Any] | None = None) -> None:
        self.provider = provider or DEFAULT_PROVIDER

    def _slot(self, name: str) -> tuple[str, str]:
        models = self.provider.get("models") if isinstance(self.provider.get("models"), dict) else {}
        item = models.get(name) if isinstance(models.get(name), dict) else {}
        fallback = DEFAULT_PROVIDER["models"][name]
        return str(item.get("id") or fallback["id"]), str(item.get("effort") or fallback["effort"])

    def profile(self, stage: str, packet: dict[str, Any], requested_timeout: int) -> CognitiveTaskProfile:
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        task_key = str(packet.get("task_key") or "")
        gaps = len(evidence.get("critical_gaps") or [])
        sources = evidence.get("sources") or []
        conflicts = evidence.get("conflicts") or []
        events = evidence.get("high_impact_events") or []
        blocked = any("网络" in str(gap) or "不可用" in str(gap) or "缺失" in str(gap) for gap in evidence.get("critical_gaps") or [])
        serialized = json.dumps(packet, ensure_ascii=False, sort_keys=True)
        human_keys = ("human_messages", "h0", "chat_human", "我的消息")
        m1_blind = stage != "m1_judgment" or not any(key in serialized for key in human_keys)
        family = "research" if stage in RESEARCH_STAGES else "judgment" if stage in JUDGMENT_STAGES else "expression"
        return CognitiveTaskProfile(
            family=family, cell_key=f"{('m1' if stage == 'm1_judgment' else stage)}:{task_key or 'unknown'}",
            major=task_key in MAJOR_TASKS or len(events) >= 1 or len(conflicts) >= 2,
            evidence_gaps=gaps, source_count=len(sources), source_conflicts=len(conflicts),
            high_impact_events=len(events), data_blocked=blocked,
            deadline_seconds=max(1, int(requested_timeout)), m1_blind=m1_blind,
        )

    def baseline(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision:
        profile = self.profile(stage, packet, requested_timeout)
        if stage in RESEARCH_STAGES:
            model, effort = self._slot("research")
            return RoutingDecision(model, effort, search, profile.deadline_seconds, "使用已配置的研究模型槽", "research")
        if stage in JUDGMENT_STAGES:
            model, effort = self._slot("judgment")
            return RoutingDecision(model, effort, search, profile.deadline_seconds, "正式判断基线使用已配置的判断模型槽", "judgment")
        model, effort = self._slot("fast")
        return RoutingDecision(model, effort, search, profile.deadline_seconds, "使用已配置的快速表达模型槽", "fast")

    def candidate(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision | None:
        profile = self.profile(stage, packet, requested_timeout)
        if stage not in JUDGMENT_STAGES or profile.data_blocked or (stage == "m1_judgment" and not profile.m1_blind):
            return None
        model, default_effort = self._slot("judgment")
        if profile.major and profile.deadline_seconds >= 240:
            # Reserve a deterministic hedge window.  XHigh is valuable only if
            # it cannot consume the deadline needed to publish a sound Medium
            # result after a timeout or runner failure.
            xhigh_timeout = max(60, int(profile.deadline_seconds * 0.65))
            return RoutingDecision(model, "xhigh", search, xhigh_timeout, "重大事件或明显证据冲突，使用判断模型槽进行更深复核", "judgment")
        if stage in {"reflection", "workflow_feedback"} or profile.evidence_gaps >= 2 or profile.source_count < 3:
            return RoutingDecision(model, "high" if default_effort == "medium" else default_effort, search, profile.deadline_seconds, "证据缺口、稀疏性或因果复盘需要更强反证", "judgment")
        return None

    def plan(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool, mode: str = "shadow") -> RoutingPlan:
        profile = self.profile(stage, packet, requested_timeout)
        baseline = self.baseline(stage, packet, requested_timeout, search)
        candidate = self.candidate(stage, packet, requested_timeout, search)
        selected = candidate if mode == "promoted" and candidate else baseline
        return RoutingPlan(profile, baseline, selected, candidate, mode)

    def route(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision:
        return self.plan(stage, packet, requested_timeout, search).selected

    def verify(self, stage: str, packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        problems: list[str] = []
        profile = self.profile(stage, packet, 1)
        if stage == "m1_judgment" and not profile.m1_blind:
            problems.append("m1_packet_contains_human_input")
        snapshot = output.get("snapshot") if isinstance(output.get("snapshot"), dict) else None
        if stage == "m1_judgment" and snapshot is not None:
            if bool(output.get("judgment_qualified")) != bool(snapshot.get("qualified")):
                problems.append("judgment_qualification_conflicts_with_snapshot")
        if stage in {"m1_judgment", "m2"} and snapshot:
            if snapshot.get("qualified") and snapshot.get("direction") in {"unknown", "unqualified"}:
                problems.append("qualified_snapshot_has_no_direction")
            if snapshot.get("qualified") and (not snapshot.get("triggers") or not snapshot.get("invalidations")):
                problems.append("qualified_snapshot_lacks_execution_boundary")
        return {"passed": not problems, "problems": problems, "profile": profile.as_json()}
