from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .effort_policy import CognitiveEffortPolicy, EffortPolicyFacts
from .stage_expression import normalize_stage_output, semantic_snapshot_conflicts


RESEARCH_STAGES = frozenset({"m0_research", "m1_research", "outcome_research", "chat_research"})
JUDGMENT_STAGES = frozenset({"m1_judgment", "m2", "reflection", "workflow_feedback"})
MAJOR_TASKS = frozenset({"daily.execution.1430", "daily.review.1520", "manual.non_trading_outlook", "periodic.monthly", "periodic.quarterly", "periodic.annual"})


def _contains_human_input(value: Any) -> bool:
    """Detect human channels structurally without matching ordinary text such as sh000001."""
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in {"human_messages", "h0", "chat_human", "我的消息"}:
                return True
            if normalized == "kind" and str(item).strip().lower() in {"h0", "chat_human"}:
                return True
            if _contains_human_input(item):
                return True
    elif isinstance(value, list):
        return any(_contains_human_input(item) for item in value)
    return False


@dataclass(frozen=True)
class RoutingDecision:
    intellect: str
    reasoning_effort: str
    search: bool
    timeout_seconds: int
    reason: str
    effort_policy_version: str | None = None
    effort_input_fingerprint: str | None = None
    effort_reason_codes: tuple[str, ...] = ()

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
    dependency_health: str = "unknown"
    market_regime: str = "unknown"

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
    """Choose Broker intellect and effort; Broker owns all model routing."""

    def __init__(self, effort_policy: CognitiveEffortPolicy | None = None) -> None:
        self.effort_policy = effort_policy or CognitiveEffortPolicy.bootstrap()

    def profile(self, stage: str, packet: dict[str, Any], requested_timeout: int) -> CognitiveTaskProfile:
        evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
        task_key = str(packet.get("task_key") or "")
        gaps = len(evidence.get("critical_gaps") or [])
        sources, conflicts, events = evidence.get("sources") or [], evidence.get("conflicts") or [], evidence.get("high_impact_events") or []
        blocked = any("网络" in str(gap) or "不可用" in str(gap) or "缺失" in str(gap) for gap in evidence.get("critical_gaps") or [])
        m1_blind = stage != "m1_judgment" or not _contains_human_input(packet)
        family = "research" if stage in RESEARCH_STAGES else "judgment" if stage in JUDGMENT_STAGES else "expression"
        dependency = packet.get("dependency_health")
        dependency_health = str(dependency.get("status") or "unknown") if isinstance(dependency, dict) else str(dependency or "unknown")
        market = packet.get("market_regime")
        market_regime = str(market.get("regime") or "unknown") if isinstance(market, dict) else str(market or "unknown")
        return CognitiveTaskProfile(
            family=family, cell_key=f"{('m1' if stage == 'm1_judgment' else stage)}:{task_key or 'unknown'}",
            major=task_key in MAJOR_TASKS or len(events) >= 1 or len(conflicts) >= 2,
            evidence_gaps=gaps, source_count=len(sources), source_conflicts=len(conflicts), high_impact_events=len(events),
            data_blocked=blocked, deadline_seconds=max(1, int(requested_timeout)), m1_blind=m1_blind,
            dependency_health=dependency_health, market_regime=market_regime,
        )

    @staticmethod
    def _effort_facts(stage: str, profile: CognitiveTaskProfile) -> EffortPolicyFacts:
        return EffortPolicyFacts(
            cell_key=profile.cell_key, family=profile.family, stage=stage, major=profile.major,
            evidence_gaps=profile.evidence_gaps, source_count=profile.source_count,
            source_conflicts=profile.source_conflicts, high_impact_events=profile.high_impact_events,
            data_blocked=profile.data_blocked, deadline_seconds=profile.deadline_seconds,
            dependency_health=profile.dependency_health, market_regime=profile.market_regime,
        )

    def baseline(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision:
        profile = self.profile(stage, packet, requested_timeout)
        effort = self.effort_policy.select(self._effort_facts(stage, profile), mode="shadow")
        if stage in RESEARCH_STAGES:
            intellect, reason = "smart", "公开研究使用 Broker smart；effort 由认知策略决定"
        elif stage in JUDGMENT_STAGES:
            intellect, reason = "expert", "正式判断使用 Broker expert；effort 由认知策略决定"
        else:
            intellect, reason = "standard", "自然表达使用 Broker standard；effort 由认知策略决定"
        return RoutingDecision(
            intellect, effort.baseline_effort, search, profile.deadline_seconds, reason,
            effort.policy_version, effort.input_fingerprint, effort.reason_codes,
        )

    def candidate(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision | None:
        profile = self.profile(stage, packet, requested_timeout)
        if profile.data_blocked or (stage == "m1_judgment" and not profile.m1_blind):
            return None
        effort = self.effort_policy.select(self._effort_facts(stage, profile), mode="shadow")
        candidate = self.effort_policy.propose_shadow(effort)
        if candidate is None:
            return None
        intellect = "smart" if stage in RESEARCH_STAGES else "expert" if stage in JUDGMENT_STAGES else "standard"
        timeout = max(60, int(profile.deadline_seconds * .65)) if candidate.effort == "xhigh" else profile.deadline_seconds
        return RoutingDecision(
            intellect, candidate.effort, search, timeout,
            "shadow 候选 effort 由版本化认知策略产生",
            effort.policy_version, effort.input_fingerprint, effort.reason_codes,
        )

    def plan(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool, mode: str = "shadow") -> RoutingPlan:
        profile = self.profile(stage, packet, requested_timeout)
        baseline = self.baseline(stage, packet, requested_timeout, search)
        candidate = self.candidate(stage, packet, requested_timeout, search)
        return RoutingPlan(profile, baseline, candidate if mode == "promoted" and candidate else baseline, candidate, mode)

    def route(self, stage: str, packet: dict[str, Any], requested_timeout: int, search: bool) -> RoutingDecision:
        return self.plan(stage, packet, requested_timeout, search).selected

    def verify(self, stage: str, packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
        problems: list[str] = []
        profile = self.profile(stage, packet, 1)
        normalized = normalize_stage_output(stage, output)
        if stage == "m0_compose":
            calendar = packet.get("calendar_context") if isinstance(packet.get("calendar_context"), dict) else {}
            body = "".join(normalized.text.split()).lower()
            if any(marker in body for marker in (
                "建议买入", "建议卖出", "建议加仓", "建议减仓", "不新增仓", "不加仓", "不减仓", "不清仓",
                "买入股数", "卖出股数", "持有观察", "今日动作",
                "看多", "看空", "偏多", "偏空", "做多", "做空", "bullish", "bearish",
            )):
                problems.append("m0_contains_direction_or_action")
            if calendar.get("is_xshg_trading_day") is True and any(marker in body for marker in (
                "非a股交易日", "非交易日", "状态:skipped", "状态：skipped",
            )):
                problems.append("m0_calendar_context_conflict")
            expected_date = str(calendar.get("date") or "")
            expected_weekday = str(calendar.get("weekday_name_zh") or "")
            if expected_date and expected_weekday:
                expected_labels = {expected_weekday, expected_weekday.replace("星期", "周")}
                wrong_labels = {f"星期{suffix}" for suffix in "一二三四五六日"} | {f"周{suffix}" for suffix in "一二三四五六日"}
                if any(f"{expected_date}为{label}" in body for label in wrong_labels - expected_labels):
                    problems.append("m0_calendar_weekday_conflict")
        if stage == "m1_judgment" and not profile.m1_blind:
            problems.append("m1_packet_contains_human_input")
        snapshot = normalized.snapshot or None
        if stage == "m1_judgment" and snapshot is not None and normalized.qualified != bool(snapshot.get("qualified")):
            problems.append("judgment_qualification_conflicts_with_snapshot")
        if semantic_snapshot_conflicts(normalized):
            problems.append("judgment_semantic_conflicts_with_snapshot")
        if stage in {"m1_judgment", "m2"} and snapshot:
            if snapshot.get("qualified") and snapshot.get("direction") in {"unknown", "unqualified"}:
                problems.append("qualified_snapshot_has_no_direction")
            if snapshot.get("qualified") and (not snapshot.get("triggers") or not snapshot.get("invalidations")):
                problems.append("qualified_snapshot_lacks_execution_boundary")
        return {"passed": not problems, "problems": problems, "profile": profile.as_json()}
