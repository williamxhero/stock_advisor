from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

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
        if stage == "m0_compose":
            contract = packet.get("evidence_contract") if isinstance(packet.get("evidence_contract"), dict) else {}
            portfolio_requirement = next((
                item for item in contract.get("requirements") or []
                if isinstance(item, dict) and item.get("key") == "portfolio_market_state"
            ), {})
            entities = [str(value).lower() for value in portfolio_requirement.get("required_entities") or [] if str(value)]
            body = "".join(normalized.text.split()).lower()
            missing_entities = [entity for entity in entities if entity not in body]
            if missing_entities:
                problems.append("m0_missing_portfolio_market_coverage:" + ",".join(missing_entities))
            if entities and any(marker in body for marker in ("未覆盖", "未被覆盖", "没有持仓行情", "持仓行情未知")):
                problems.append("m0_claims_portfolio_quote_gap_despite_qualified_evidence")
            quotes = _frozen_portfolio_quotes(packet.get("verified_fact_digest"))
            for entity in entities:
                quote = quotes.get(entity)
                if quote is None:
                    problems.append("m0_missing_verified_portfolio_quote:" + entity)
                    continue
                for field in ("price", "previous_close", "change", "change_percent"):
                    value = quote.get(field)
                    if value is not None and _number_text(value) not in body:
                        problems.append(f"m0_portfolio_quote_conflict:{entity}:{field}")
                if not _m0_has_status(body, str(quote.get("status") or "")):
                    problems.append(f"m0_portfolio_quote_status_conflict:{entity}")
                local_time = _china_quote_time(quote.get("quote_at"))
                if local_time and f"北京时间{local_time}" not in body:
                    problems.append(f"m0_portfolio_quote_time_conflict:{entity}")
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


def _frozen_portfolio_quotes(value: Any) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    for row in value or []:
        if not isinstance(row, dict):
            continue
        try:
            parsed = json.loads(str(row.get("excerpt") or ""))
        except json.JSONDecodeError:
            continue
        for item in _walk_quotes(parsed):
            symbol = str(item.get("symbol") or "").lower()
            if symbol:
                quotes[symbol] = item
    return quotes


def _walk_quotes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = [item for item in value.get("quotes") or [] if isinstance(item, dict)]
        return rows + [quote for item in value.values() for quote in _walk_quotes(item)]
    if isinstance(value, list):
        return [quote for item in value for quote in _walk_quotes(item)]
    return []


def _number_text(value: Any) -> str:
    number = float(value)
    return (f"{number:.4f}").rstrip("0").rstrip(".")


def _china_quote_time(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    except ValueError:
        return None


def _m0_has_status(body: str, status: str) -> bool:
    labels = {
        "trading": ("交易状态", "处于交易", "交易中"),
        "suspended": ("停牌", "暂停交易"),
        "unavailable": ("数据不可用", "行情不可用"),
    }
    return not status or any(label in body for label in labels.get(status, (status,)))
