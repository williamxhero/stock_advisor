from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any
from zoneinfo import ZoneInfo

from .effort_policy import CognitiveEffortPolicy, EffortPolicyFacts
from .stage_expression import (
    canonical_direction, normalize_stage_output, semantic_snapshot_conflicts,
    verified_weekly_market_comparison,
)


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
            if any(marker in body for marker in (
                "本阶段", "m0客观观察", "冻结工具", "确定性投影", "冻结证据",
                "已检查且无变化", "完整覆盖", "protocol", "requirement",
            )):
                problems.append("m0_exposes_internal_process")
            verified_numbers = {
                _numeric_comparison_key(value) for value in _verified_numeric_tokens(packet)
            }
            unknown_numbers = sorted({
                value for value in _numeric_tokens(body)
                if _numeric_comparison_key(value) not in verified_numbers
            })
            problems.extend(f"m0_contains_unverified_numeric_claim:{value}" for value in unknown_numbers)
            problems.extend(_m0_covered_gap_problems(packet, body))
        if stage == "m0_compose":
            contract = packet.get("evidence_contract") if isinstance(packet.get("evidence_contract"), dict) else {}
            portfolio_requirement = next((
                item for item in contract.get("requirements") or []
                if isinstance(item, dict) and item.get("key") == "portfolio_market_state"
            ), {})
            entities = [str(value).lower() for value in portfolio_requirement.get("required_entities") or [] if str(value)]
            body = "".join(normalized.text.split()).lower()
            if entities and any(marker in body for marker in ("未覆盖", "未被覆盖", "没有持仓行情", "持仓行情未知")):
                problems.append("m0_claims_portfolio_quote_gap_despite_qualified_evidence")
            quotes = _frozen_portfolio_quotes(packet.get("verified_fact_digest"))
            mentioned_entities = [entity for entity in entities if entity in body]
            if len(mentioned_entities) > 2:
                problems.append("m0_overloads_reply_with_holding_quotes")
            for entity in entities:
                if entity not in body:
                    continue
                quote = quotes.get(entity)
                if quote is None:
                    problems.append("m0_missing_verified_portfolio_quote:" + entity)
                    continue
                detail_markers = ("价格", "前收", "变动", "涨幅", "变动幅度")
                if sum(marker in body for marker in detail_markers) < 2:
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
        if stage in {"m0_compose", "m1_judgment", "m2"}:
            evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
            body = "".join(normalized.text.split()).casefold()
            explainable_conflicts = [
                row for row in evidence.get("conflicts") or []
                if isinstance(row, dict) and row.get("resolution") in {"scope_difference", "primary_precedence"}
            ]
            if explainable_conflicts and "口径" not in body:
                problems.append("judgment_omits_explainable_source_scope_conflict")
            unresolved = [
                row for row in evidence.get("conflicts") or []
                if isinstance(row, dict) and row.get("resolution") == "unresolved_equal_tier"
                and row.get("materiality") in {"medium", "high"}
            ]
            if unresolved and isinstance(normalized.semantic, dict) and normalized.semantic.get("qualified") is True:
                problems.append("qualified_judgment_depends_on_unresolved_source_conflict")
            for event in evidence.get("high_impact_events") or []:
                if not isinstance(event, dict) or event.get("propagation_status") != "observed":
                    continue
                truth = str(event.get("truth_status") or "")
                if "传播" not in body:
                    problems.append("judgment_omits_observed_market_propagation")
                if truth == "unverified" and not any(marker in body for marker in ("未证实", "未经证实", "尚未证实")):
                    problems.append("judgment_presents_unverified_event_as_fact")
                if truth == "refuted" and not any(marker in body for marker in ("已否认", "被否认", "已证伪", "被证伪")):
                    problems.append("judgment_omits_event_refutation")
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
        if stage in {"m1_judgment", "m2"} and isinstance(normalized.semantic, dict) and "current_action" in normalized.semantic:
            semantic = normalized.semantic
            action = str(semantic.get("current_action") or "")
            if action not in {"observe", "reduce_risk", "allow_add_risk", "avoid"}:
                problems.append("judgment_current_action_invalid")
            direction = canonical_direction(semantic.get("direction"))
            if semantic.get("qualified") and not action:
                problems.append("qualified_judgment_lacks_current_action")
            if direction in {"unqualified", "unknown"} and action not in {"", "observe"}:
                problems.append("unqualified_judgment_has_nonconservative_action")
            conditions = [item for item in semantic.get("transition_conditions") or [] if isinstance(item, dict)]
            if semantic.get("qualified") and not conditions:
                problems.append("qualified_judgment_lacks_joint_confirmation")
            for condition in conditions:
                if not all(str(condition.get(key) or "").strip() for key in ("price", "breadth", "persistence")):
                    problems.append("judgment_transition_lacks_joint_confirmation")
                    break
            positions = [item for item in semantic.get("position_focus") or [] if isinstance(item, dict)]
            if len(positions) > 2:
                problems.append("judgment_overloads_position_focus")
            priorities = [item.get("priority") for item in positions]
            if priorities and sorted(priorities) != list(range(1, len(priorities) + 1)):
                problems.append("judgment_position_priority_is_not_contiguous")
            if any(
                any(marker in str(item.get("reason") or "") for marker in ("浮亏", "亏损最多", "成本最高", "成本价"))
                for item in positions
            ):
                problems.append("judgment_position_priority_is_cost_anchored")
            problems.extend(_qualified_reply_acquisition_gap_problems(semantic))
            if stage == "m1_judgment":
                problems.extend(_close_review_coverage_problems(packet, semantic))
                problems.extend(_weekend_review_coverage_problems(packet, semantic))
        return {"passed": not problems, "problems": problems, "profile": profile.as_json()}


def _close_review_coverage_problems(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    task_profile = packet.get("task_profile") if isinstance(packet.get("task_profile"), dict) else {}
    if (
        packet.get("task_key") != "daily.review.1520"
        or task_profile.get("evidence_family") != "completed_close"
    ):
        return []

    fields = [
        semantic.get("summary"),
        *(semantic.get("key_evidence") or []),
        *(item.get("reason") for item in semantic.get("position_focus") or [] if isinstance(item, dict)),
        *(semantic.get("risks") or []),
        *(semantic.get("unknowns") or []),
    ]
    body = " ".join(str(value or "") for value in fields)
    compact = "".join(body.split())
    problems: list[str] = []

    if not (
        "成交" in compact
        and any(term in compact for term in ("前一交易日", "上一交易日", "昨日", "较前日", "较上日"))
        and any(term in compact for term in ("亿元", "万亿元", "万亿"))
        and len(_numeric_tokens(body)) >= 2
    ):
        problems.append("close_review_lacks_numeric_turnover_comparison")
    if not (
        any(term in compact for term in ("板块", "题材", "概念"))
        and any(term in compact for term in ("领涨", "涨幅居前", "强势"))
        and any(term in compact for term in ("领跌", "跌幅居前", "弱势"))
    ):
        problems.append("close_review_lacks_theme_leaders_and_laggards")
    if not (
        any(term in compact for term in ("论坛", "讨论热度", "传播", "市场情绪", "情绪"))
        and any(term in compact for term in ("市场宽度", "广度", "上涨家数", "下跌家数", "涨跌停", "传播", "讨论热度"))
        and any(term in compact for term in ("偏强", "偏弱", "分化", "风险偏好", "验证", "支持", "显示"))
    ):
        problems.append("close_review_lacks_forum_or_sentiment_substitute")

    analysis = task_profile.get("analysis") if isinstance(task_profile.get("analysis"), dict) else {}
    goal = str(analysis.get("goal") or "")
    if "来源" in goal and not any(
        term in compact for term in ("来源", "上交所", "深交所", "交易所", "东方财富", "腾讯", "公告")
    ):
        problems.append("close_review_lacks_requested_source_attribution")
    if "时点" in goal and not re.search(
        r"(?:截至|资料时点|数据时点|[0-2]?\d:[0-5]\d|20\d{2}年|\d{1,2}月\d{1,2}日)", compact,
    ):
        problems.append("close_review_lacks_requested_fact_timing")

    if "全部" in goal and "持仓" in goal:
        business_context = packet.get("business_context") if isinstance(packet.get("business_context"), dict) else {}
        private_context = business_context.get("private_context_before_h0") or {}
        for position in private_context.get("positions") or []:
            if not isinstance(position, dict) or float(position.get("shares") or 0) <= 0:
                continue
            code, name = str(position.get("code") or ""), str(position.get("name") or "")
            if not ((code and code in compact) or (name and name in compact)):
                problems.append("close_review_omits_active_position:" + (code or name))
    return problems


def _weekend_review_coverage_problems(packet: dict[str, Any], semantic: dict[str, Any]) -> list[str]:
    task_profile = packet.get("task_profile") if isinstance(packet.get("task_profile"), dict) else {}
    if task_profile.get("evidence_family") != "completed_trading_week":
        return []

    fields = [
        semantic.get("summary"), *(semantic.get("key_evidence") or []),
        *(item.get("reason") for item in semantic.get("position_focus") or [] if isinstance(item, dict)),
        *(semantic.get("risks") or []), *(semantic.get("unknowns") or []),
    ]
    body = " ".join(str(value or "") for value in fields)
    compact = "".join(body.split())
    fund_flow_compact = "".join(
        "".join(str(value or "").split())
        for value in fields
        if any(marker in str(value or "") for marker in ("资金流", "主力资金", "净流入", "净流出"))
    )
    weekly = verified_weekly_market_comparison(packet)
    problems: list[str] = []
    if weekly is None:
        problems.append("weekend_review_lacks_completed_week_comparison")
    elif not (
        any(term in compact for term in ("本周", "整周", "周内", "全周"))
        and weekly["start_label"] in compact
        and weekly["end_label"] in compact
    ):
        problems.append("weekend_review_lacks_completed_week_comparison")
    elif any(
        not re.search(
            rf"{item['name']}[^。；]{{0,24}}(?:周)?{item['direction']}[^。；]{{0,8}}{re.escape(str(item['number']))}(?:%|％)",
            compact,
        )
        for item in weekly["entries"]
    ):
        problems.append("weekend_review_lacks_completed_week_comparison")

    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    coverage = {
        str(item.get("requirement_key") or ""): str(item.get("status") or "")
        for item in evidence.get("coverage") or [] if isinstance(item, dict)
    }
    available = {key for key, status in coverage.items() if status in {"covered", "checked_no_change"}}

    if "themes_and_capacity_cores" in available and not (
        any(term in compact for term in ("行业", "板块", "题材", "概念"))
        and any(term in compact for term in ("领涨", "涨幅", "强势", "净流入"))
        and any(term in compact for term in ("领跌", "跌幅", "弱势", "净流出"))
    ):
        problems.append("weekend_review_lacks_sector_distribution")

    if "market_fund_flow" in available and not (
        any(term in compact for term in ("资金流", "主力资金", "净流入", "净流出"))
        and any(term in compact for term in ("净流入", "净流出"))
        and any(term in compact for term in ("亿元", "万元", "元"))
    ):
        problems.append("weekend_review_lacks_fund_flow_direction")

    directional_fund_flow = False
    for source in evidence.get("sources") or []:
        if not isinstance(source, dict):
            continue
        try:
            payload = json.loads(str(source.get("excerpt") or ""))
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("coverage_level") == "directional_sector":
            directional_fund_flow = True
            break
    if directional_fund_flow:
        has_interpretation = any(term in fund_flow_compact for term in (
            "结构性分化", "板块轮动", "题材轮动", "资金偏向", "资金集中", "方向承压", "电子承压",
            "科技承压", "而非普遍", "不应外推",
        ))
        if not has_interpretation:
            problems.append("weekend_review_lacks_directional_fund_flow_interpretation")
        if (
            re.search(r"全市场[^。；]{0,18}(?:净流入|净流出)[^。；]{0,8}\d", compact)
            or re.search(r"(?:超大单|大单|中单|小单)[^。；]{0,12}(?:净流入|净流出)[^。；]{0,8}\d", compact)
        ):
            problems.append("weekend_review_overclaims_directional_fund_flow")

    if "material_events_and_counterevidence" in available and not (
        any(term in compact for term in ("政策", "市场事件", "风险事件", "市场公告"))
        and any(term in compact for term in ("影响", "扰动", "催化", "压制", "支撑", "未发现"))
    ):
        problems.append("weekend_review_lacks_market_event_impact")
    elif "material_events_and_counterevidence" in available and not any(
        term in compact for term in ("可能", "推断", "仍需", "待验证", "或将")
    ):
        problems.append("weekend_review_event_impact_not_marked_as_inference")

    if "portfolio_events_and_counterevidence" in available:
        announcement_markers = ("公告", "披露", "停复牌", "财报")
        announcement_text = "".join(
            segment for segment in re.split(r"[。；\n]", body)
            if any(marker in segment for marker in announcement_markers)
        )
        announcement_compact = "".join(announcement_text.split())
        requirement = next((
            item for item in (packet.get("evidence_contract") or {}).get("requirements") or []
            if isinstance(item, dict) and item.get("key") == "portfolio_events_and_counterevidence"
        ), {})
        aliases = _portfolio_entity_aliases(packet)
        missing = [
            entity for entity in requirement.get("required_entities") or []
            if not any(alias and alias in announcement_compact for alias in aliases.get(str(entity), (str(entity),)))
        ]
        if not announcement_text or missing:
            problems.append("weekend_review_lacks_portfolio_announcement_checks")
        pending_titles: list[str] = []
        for source in evidence.get("sources") or []:
            if not isinstance(source, dict):
                continue
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            pending_titles.extend(
                str(item.get("title") or "")
                for item in payload.get("announcements") or []
                if isinstance(item, dict) and item.get("content_verified") is not True
            )
        pending_titles = [title for title in pending_titles if title]
        if pending_titles and not any(
            term in announcement_compact for term in ("内容待核验", "影响待核验", "暂不据标题", "不能仅凭标题")
        ):
            problems.append("weekend_review_lacks_unverified_announcement_boundary")
        if any(
            re.search(
                rf"(?:因|根据)[^。；]{{0,20}}{re.escape(title)}[^。；]{{0,20}}(?:加仓|减仓|买入|卖出|清仓)",
                announcement_compact,
            )
            for title in pending_titles
        ):
            problems.append("weekend_review_overclaims_unverified_announcement_title")
    return problems


def _qualified_reply_acquisition_gap_problems(semantic: dict[str, Any]) -> list[str]:
    """Keep source acquisition diagnostics out of a qualified trading judgment."""
    if semantic.get("qualified") is not True:
        return []
    fields = [
        semantic.get("summary"), *(semantic.get("key_evidence") or []),
        *(item.get("reason") for item in semantic.get("position_focus") or [] if isinstance(item, dict)),
        *(
            item.get(key)
            for item in semantic.get("transition_conditions") or [] if isinstance(item, dict)
            for key in ("price", "breadth", "persistence")
        ),
        *(semantic.get("risks") or []), *(semantic.get("unknowns") or []),
    ]
    body = "".join(str(value or "") for value in fields)
    acquisition_objects = "数据|证据|资料|检索结果|查询结果|净额|拆分|明细|序列|行情|公告覆盖|论坛传播"
    acquisition_patterns = (
        rf"(?:仍|尚|当前|本次)?未(?:取得|获取|获得|提供|覆盖|查到)[^。；]{{0,24}}(?:{acquisition_objects})",
        rf"(?:仍|尚|还)?(?:没有|没)(?:取得|获取|获得|拿到|查到|覆盖)[^。；]{{0,24}}(?:{acquisition_objects})",
        rf"(?:当前|目前|本次)?(?:没有|没|缺少|缺失)[^。；]{{0,24}}(?:{acquisition_objects})",
        r"(?:当前|目前|本次)?(?:只|仅)(?:能)?覆盖[^。；]{0,20}(?:板块方向|部分板块|数据|字段|范围)",
        rf"(?:{acquisition_objects})[^。；]{{0,24}}(?:未取得|未获取|未获得|未提供|未覆盖|未查到|缺失|不完整|不可得|无法获取|不包含|拿不到)",
        rf"(?:无法|不能)[^。；]{{0,8}}(?:取得|获取|获得)[^。；]{{0,12}}(?:{acquisition_objects})",
        rf"(?:等|待|在)?(?:拿到|取得|获取|获得|补齐|查到)[^。；]{{0,12}}(?:{acquisition_objects})",
    )
    if any(re.search(pattern, body) for pattern in acquisition_patterns):
        return ["qualified_reply_exposes_noncritical_acquisition_gap"]
    return []


def _portfolio_entity_aliases(packet: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    business = packet.get("business_context") if isinstance(packet.get("business_context"), dict) else {}
    private = business.get("private_context_before_h0") if isinstance(business.get("private_context_before_h0"), dict) else {}
    aliases: dict[str, tuple[str, ...]] = {}
    for position in private.get("positions") or []:
        if not isinstance(position, dict):
            continue
        code, name = str(position.get("code") or ""), str(position.get("name") or "")
        if code:
            aliases[code] = tuple(value for value in (code, name) if value)
    return aliases


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


def _numeric_tokens(value: str) -> list[str]:
    return re.findall(r"(?<![\dA-Za-z])[-+]?\d+(?:\.\d+)?", value)


def _numeric_comparison_key(token: str) -> str:
    """Normalize harmless display syntax without weakening numeric identity."""
    unsigned = token[1:] if token.startswith("+") else token
    if "." not in unsigned:
        return unsigned
    integer, fraction = unsigned.split(".", 1)
    fraction = fraction.rstrip("0")
    return integer if not fraction else f"{integer}.{fraction}"


def _verified_numeric_tokens(packet: dict[str, Any]) -> set[str]:
    exact_tokens = set(_numeric_tokens(json.dumps(packet, ensure_ascii=False, sort_keys=True)))
    tokens = {
        alias
        for value in exact_tokens
        for alias in _numeric_display_aliases(value)
    }
    for key in ("as_of", "scheduled_for"):
        value = packet.get(key)
        if not value:
            continue
        try:
            local = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai"))
        except ValueError:
            continue
        tokens.update(_numeric_tokens(local.isoformat()))
        tokens.update((local.strftime("%H"), local.strftime("%M"), local.strftime("%S")))
    for quote in _frozen_portfolio_quotes(packet.get("verified_fact_digest")).values():
        local_time = _china_quote_time(quote.get("quote_at"))
        if local_time:
            tokens.update(_numeric_tokens(local_time))
    return tokens


def _numeric_display_aliases(token: str) -> set[str]:
    """Allow ordinary display rounding without admitting unrelated numbers."""
    aliases = {token}
    try:
        number = Decimal(token)
    except InvalidOperation:
        return aliases
    canonical = _compact_decimal(number)
    aliases.add(canonical or "0")
    decimal_places = max(0, -number.as_tuple().exponent)
    for places in range(1, min(decimal_places, 3)):
        quantum = Decimal(1).scaleb(-places)
        rounded = _compact_decimal(number.quantize(quantum, rounding=ROUND_HALF_UP))
        aliases.add(rounded or "0")
    if decimal_places and abs(number) >= 10:
        aliases.add(format(number.quantize(Decimal("1"), rounding=ROUND_HALF_UP), "f"))
    return aliases


def _compact_decimal(number: Decimal) -> str:
    rendered = format(number, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _china_quote_time(value: Any) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
    except ValueError:
        return None


def _m0_covered_gap_problems(packet: dict[str, Any], body: str) -> list[str]:
    """Reject absence claims that contradict the frozen evidence ledger."""
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    coverage = {
        str(row.get("requirement_key") or ""): str(row.get("status") or "")
        for row in evidence.get("coverage") or [] if isinstance(row, dict)
    }
    topic_markers = {
        "themes_and_capacity_cores": ("行业", "板块", "题材", "涨跌分布"),
        "market_fund_flow": ("资金流", "主力资金", "资金流向"),
        "portfolio_events_and_counterevidence": ("个股公告", "公告影响", "持仓公告", "公告"),
        "material_events_and_counterevidence": ("市场事件", "政策", "市场公告"),
        "events_and_counterevidence": ("市场事件", "政策", "市场公告"),
    }
    gap_markers = ("未提供", "缺少", "未取得", "未获取", "无法获取", "没有数据", "没有证据", "没有信息")
    if not any(marker in body for marker in gap_markers):
        return []
    problems: list[str] = []
    for key, markers in topic_markers.items():
        if coverage.get(key) not in {"covered", "checked_no_change"}:
            continue
        if any(
            re.search(
                rf"(?:{'|'.join(map(re.escape, gap_markers))})[^。；]{{0,40}}{re.escape(marker)}"
                rf"|{re.escape(marker)}[^。；]{{0,40}}(?:{'|'.join(map(re.escape, gap_markers))})",
                body,
            )
            for marker in markers
        ):
            problems.append(f"m0_claims_covered_evidence_gap:{key}")
    return problems


def _m0_has_status(body: str, status: str) -> bool:
    labels = {
        "trading": ("交易状态", "处于交易", "交易中"),
        "suspended": ("停牌", "暂停交易"),
        "unavailable": ("数据不可用", "行情不可用"),
    }
    return not status or any(label in body for label in labels.get(status, (status,)))
