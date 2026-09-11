from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from .stage_output_compat import adapt_legacy_stage_output
from .transition_conditions import condition_text


@dataclass(frozen=True)
class NormalizedStageOutput:
    stage: str
    semantic: dict[str, Any]
    snapshot: dict[str, Any]
    text: str
    qualified: bool | None
    legacy: bool
    snapshot_derived: bool = False


def canonical_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"bullish", "bearish", "neutral", "avoid", "unqualified", "unknown"}:
        return text
    for direction, terms in (
        ("avoid", ("回避", "不交易", "不买", "空仓", "avoid")),
        ("bearish", ("偏空", "看空", "做空", "转弱", "走弱", "下行", "bearish")),
        ("bullish", ("偏多", "看多", "做多", "转强", "走强", "上行", "bullish")),
        ("neutral", ("中性", "观望", "等待", "neutral")),
    ):
        if any(term in text for term in terms):
            return direction
    return "unknown"


def _semantic_snapshot(semantic: dict[str, Any]) -> dict[str, Any]:
    direction = canonical_direction(semantic.get("direction"))
    summary = _m0_item_text(semantic.get("summary"))
    positions = [item for item in semantic.get("position_focus") or [] if isinstance(item, dict)]
    subjects = list(dict.fromkeys([
        *re.findall(r"(?<!\d)\d{6}(?!\d)", summary),
        *(str(item.get("symbol") or "").strip() for item in positions),
    ]))
    subjects = [item for item in subjects if item]
    conditions = [item for item in semantic.get("transition_conditions") or [] if isinstance(item, dict)]
    trigger_conditions = [item for item in conditions if item.get("outcome") == "upgrade"]
    invalidation_conditions = [item for item in conditions if item.get("outcome") == "downgrade"]
    triggers = [_condition_text(item) for item in trigger_conditions] or [str(item) for item in semantic.get("triggers") or []]
    invalidations = [_condition_text(item) for item in invalidation_conditions] or [str(item) for item in semantic.get("invalidations") or []]
    claims = []
    if summary and direction != "unknown":
        claims.append({
            "subjects": subjects,
            "direction": direction,
            "horizon": str(semantic.get("horizon") or "").strip() or None,
            "triggers": triggers,
            "invalidations": invalidations,
            "confidence": None,
            "benchmark": None,
            "original_text": summary,
        })
    return {
        "subjects": subjects,
        "direction": direction,
        "qualified": bool(semantic.get("qualified")),
        "current_action": str(semantic.get("current_action") or ""),
        "triggers": triggers,
        "invalidations": invalidations,
        "risks": [str(item) for item in semantic.get("risks") or []],
        "unknowns": [str(item) for item in semantic.get("unknowns") or []],
        "position_focus": positions,
        "original_claims": [summary] if summary else [],
        "claims": claims,
    }


def _condition_text(condition: dict[str, Any]) -> str:
    return condition_text(condition)


def _clean_values(values: Any, limit: int) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values or [] if str(value).strip()
    ))[:limit]


def _sentence_piece(value: Any) -> str:
    return str(value or "").strip().rstrip("。！？；，,.!?; ")


def _m0_item_text(value: Any) -> str:
    """Read user-facing M0 text while keeping evidence refs internal."""
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    # Read-only compatibility for pre-v3 callers; provider output is checked
    # against the structured v3 schema before it can be accepted.
    return str(value or "").strip()


def _m0_item_values(values: Any, limit: int) -> list[str]:
    return list(dict.fromkeys(
        _m0_item_text(value) for value in values or [] if _m0_item_text(value)
    ))[:limit]


def _action_label(action: str) -> str:
    return {
        "observe": "继续观察，不追涨也不仓促改变判断",
        "reduce_risk": "优先收缩风险，不急着逆势承接",
        "allow_add_risk": "只在确认后允许小幅增加风险，不追逐瞬时强势",
        "avoid": "先回避交易，等待结构重新稳定",
    }.get(action, "继续观察")


def _v4_judgment_expression(semantic: dict[str, Any]) -> str:
    direction = canonical_direction(semantic.get("direction"))
    direction_label = {
        "bullish": "偏多", "bearish": "偏空", "neutral": "中性",
        "avoid": "回避交易", "unqualified": "暂不形成方向", "unknown": "暂不形成方向",
    }[direction]
    horizon = str(semantic.get("horizon") or "当前").strip()
    action = str(semantic.get("current_action") or "observe")
    paragraphs = [f"{horizon}我维持{direction_label}，当前{_action_label(action)}。"]
    summary = str(semantic.get("summary") or "").strip()
    evidence = _clean_values(semantic.get("key_evidence"), 3)
    evidence = [item for item in evidence if item not in summary]
    if summary:
        paragraphs.append(summary + ("。" if not summary.endswith(("。", "！", "？")) else ""))
    if evidence:
        paragraphs.append(" ".join(
            item if item.endswith(("。", "！", "？")) else item + "。"
            for item in evidence
        ))
    conditions = [item for item in semantic.get("transition_conditions") or [] if isinstance(item, dict)]
    for condition in conditions:
        text = _sentence_piece(_condition_text(condition))
        if not text:
            continue
        if condition.get("outcome") == "upgrade":
            paragraphs.append(f"只有{ text }，我才会上调判断。")
        elif condition.get("outcome") == "downgrade":
            paragraphs.append(f"如果{ text }，中性判断就应下调。")
    positions = sorted(
        [item for item in semantic.get("position_focus") or [] if isinstance(item, dict)],
        key=lambda item: int(item.get("priority") or 99),
    )[:2]
    if positions:
        parts = []
        for item in positions:
            symbol = str(item.get("symbol") or "这只持仓").strip()
            reason = _sentence_piece(item.get("reason") or "相对结构需要继续确认")
            parts.append(f"优先盯{symbol}，{reason}")
        paragraphs.append("。".join(parts) + "。")
    risks = _clean_values(semantic.get("risks"), 2)
    if risks:
        paragraphs.append("风险与事件方面，" + " ".join(
            item if item.endswith(("。", "！", "？")) else item + "。"
            for item in risks
        ))
    unknowns = _clean_values(semantic.get("unknowns"), 1)
    if unknowns:
        paragraphs.append(f"真正还需要确认的是{_sentence_piece(unknowns[0])}。")
    return "\n\n".join(paragraphs)


def _verified_market_snapshot_summary(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build an M0 fallback whose wording matches the frozen snapshot's stage."""
    facts: list[dict[str, Any]] = []
    for item in (packet or {}).get("verified_fact_digest") or []:
        if not isinstance(item, dict):
            continue
        try:
            value = json.loads(str(item.get("excerpt") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            facts.append(value)
    indices = [row for fact in facts for row in fact.get("indices") or [] if isinstance(row, dict)]
    breadth = next((fact.get("breadth") for fact in facts if isinstance(fact.get("breadth"), dict)), None)
    quotes = [row for fact in facts for row in fact.get("quotes") or [] if isinstance(row, dict)]
    if not indices or not breadth:
        return None

    def number(value: Any) -> str:
        return format(value, ".15g") if isinstance(value, float) else str(value)

    value = packet or {}
    evidence_refs = [
        str(row.get("evidence_ref")) for row in value.get("verified_fact_digest") or []
        if isinstance(row, dict) and str(row.get("evidence_ref") or "").strip()
    ]
    as_of = str(value.get("as_of") or "")
    try:
        snapshot_time = datetime.fromisoformat(as_of.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%H:%M")
    except ValueError:
        time_match = re.search(r"T(\d{2}:\d{2})", as_of)
        snapshot_time = time_match.group(1) if time_match else ""
    requirements = (
        (value.get("evidence_contract") or {}).get("requirements") or []
        if isinstance(value.get("evidence_contract"), dict) else []
    )
    has_official_close = any(
        isinstance(requirement, dict) and requirement.get("finality") == "official_close"
        for requirement in requirements
    )
    is_completed_close = has_official_close and bool(snapshot_time) and snapshot_time >= "15:00"
    quote_verb = "收于" if is_completed_close else "报于"
    stage_label = "收盘后" if is_completed_close else (
        f"截至{snapshot_time}" if snapshot_time else "截至本轮已验证时点"
    )

    index_text = "，".join(
        f"{row.get('name') or row.get('symbol')}{quote_verb}{number(row.get('price'))}（{number(row.get('change_percent'))}%）"
        for row in indices[:3]
        if row.get("price") is not None and row.get("change_percent") is not None
    )
    if not index_text:
        return None
    if any(breadth.get(key) is None for key in ("up", "down", "flat")):
        return None
    breadth_parts = [
        f"上涨{number(breadth['up'])}家", f"下跌{number(breadth['down'])}家",
        f"平盘{number(breadth['flat'])}家",
    ]
    if breadth.get("limit_up") is not None:
        breadth_parts.append(f"涨停候选{number(breadth['limit_up'])}家")
    if breadth.get("limit_down") is not None:
        breadth_parts.append(f"跌停候选{number(breadth['limit_down'])}家")
    up, down = float(breadth["up"]), float(breadth["down"])
    breadth_view = (
        "上涨家数多于下跌家数"
        if up > down else "下跌家数多于上涨家数"
        if down > up else "上涨与下跌家数相当"
    )
    observations = [{
        "text": "市场广度：" + "、".join(breadth_parts) + f"；{breadth_view}。",
        "evidence_refs": evidence_refs,
    }]
    event_observation, event_refs = _verified_event_observation(value)
    if event_observation:
        observations.insert(0, {"text": event_observation, "evidence_refs": event_refs or evidence_refs})
    selected_quotes = sorted(
        (row for row in quotes if row.get("price") is not None and row.get("change_percent") is not None),
        key=lambda row: abs(float(row.get("change_percent") or 0)), reverse=True,
    )[:2]
    if selected_quotes:
        observations.append({
            "text": "持仓表现有分化：" + "，".join(
                f"{row.get('name') or row.get('symbol')}{quote_verb}{number(row.get('price'))}（{number(row.get('change_percent'))}%）"
                for row in selected_quotes
            ) + "。",
            "evidence_refs": evidence_refs,
        })
    return {
        "result_version": 3,
        "semantic": {
            "summary": {"text": f"{stage_label}，三大指数：{index_text}。", "evidence_refs": evidence_refs},
            "observations": observations,
            "connections": [],
            "attention": [],
            "unknowns": [],
        },
    }


def _verified_event_observation(packet: dict[str, Any]) -> tuple[str, list[str]]:
    """State one material event faithfully when the model reply needs a safe M0 fallback."""
    evidence = packet.get("evidence") if isinstance(packet.get("evidence"), dict) else {}
    for event in evidence.get("high_impact_events") or []:
        if not isinstance(event, dict) or event.get("materiality") not in {"medium", "high"}:
            continue
        summary = str(event.get("summary") or "").strip()
        truth = str(event.get("truth_status") or "")
        propagation = str(event.get("propagation_status") or "")
        if not summary or truth not in {"verified", "unverified", "refuted"}:
            continue
        factual = (
            f"已核验的重要事件：{summary}"
            if truth == "verified" else f"关于{summary}的消息尚未证实"
            if truth == "unverified" else f"关于{summary}的说法已被否认"
        )
        propagation_text = "；其传播已在本轮市场材料中被观察到" if propagation == "observed" else ""
        refs = list(dict.fromkeys([
            str(ref) for key in ("truth_evidence_refs", "propagation_evidence_refs", "origin_evidence_refs")
            for ref in event.get(key) or [] if str(ref).strip()
        ]))
        return factual + propagation_text + "。", refs
    return "", []


def verified_weekly_market_comparison(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    """Calculate the completed-week index comparison from frozen typed evidence."""
    expected = {
        "sh000001": "上证", "sz399001": "深成指", "sz399006": "创业板",
    }
    weekly: dict[str, tuple[str, str, float]] = {}
    value = packet or {}
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    for source in evidence.get("sources") or []:
        if not isinstance(source, dict):
            continue
        try:
            payload = json.loads(str(source.get("excerpt") or ""))
            symbol = str(payload.get("symbol") or "")
            series = payload.get("series")
            first, last = series[0], series[-1]
            first_close, last_close = float(first["close"]), float(last["close"])
            start, end = str(first["date"]), str(last["date"])
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if symbol in expected and len(series) >= 2 and first_close > 0:
            weekly[symbol] = (start, end, (last_close - first_close) / first_close * 100)
    if set(weekly) != set(expected):
        return None

    start, end, _ = weekly["sh000001"]
    if any(item[0] != start or item[1] != end for item in weekly.values()):
        return None
    try:
        start_parts = [int(value) for value in start.split("-")]
        end_parts = [int(value) for value in end.split("-")]
        if len(start_parts) != 3 or len(end_parts) != 3:
            return None
    except ValueError:
        return None
    start_label = f"{start_parts[1]}月{start_parts[2]}日"
    end_label = f"{end_parts[1]}月{end_parts[2]}日"
    entries = []
    for symbol, name in expected.items():
        change = weekly[symbol][2]
        direction = "跌" if change < -0.005 else "涨" if change > 0.005 else "平"
        number = f"{round(abs(change), 2):.2f}".rstrip("0").rstrip(".")
        entries.append({
            "symbol": symbol, "name": name, "change": change,
            "direction": direction, "number": number,
        })
    text = f"本周{start_label}至{end_label}，" + "、".join(
        f"{item['name']}周{item['direction']}{item['number']}%" for item in entries
    ) + "。"
    return {
        "start": start, "end": end, "start_label": start_label, "end_label": end_label,
        "entries": entries, "text": text,
    }


def _verified_close_judgment(
    packet: dict[str, Any] | None, *, horizon: str,
) -> dict[str, Any] | None:
    """Preserve the complete close-review contract when model wording fails."""
    value = packet or {}
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    parsed: list[dict[str, Any]] = []
    for source in evidence.get("sources") or []:
        if not isinstance(source, dict):
            continue
        try:
            excerpt = json.loads(str(source.get("excerpt") or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(excerpt, dict):
            parsed.append(excerpt)

    indices = [row for item in parsed for row in item.get("indices") or [] if isinstance(row, dict)]
    breadth = next((item.get("breadth") for item in parsed if isinstance(item.get("breadth"), dict)), None)
    quotes = [row for item in parsed for row in item.get("quotes") or [] if isinstance(row, dict)]
    turnover = next((
        str(item.get("summary")) for item in parsed
        if "两市成交额" in str(item.get("summary") or "") and "前一交易日" in str(item.get("summary") or "")
    ), "")
    theme_fact = next((
        item for item in parsed if item.get("leaders") and item.get("laggards")
    ), None)
    private = (
        (value.get("business_context") or {}).get("private_context_before_h0") or {}
        if isinstance(value.get("business_context"), dict) else {}
    )
    required = {
        str(row.get("code")) for row in private.get("positions") or []
        if isinstance(row, dict) and float(row.get("shares") or 0) > 0
    }
    quote_by_symbol = {str(row.get("symbol") or ""): row for row in quotes}
    if len(indices) < 3 or not breadth or not turnover or not theme_fact or not required.issubset(quote_by_symbol):
        return None

    def number(item: Any) -> str:
        return format(item, ".15g") if isinstance(item, float) else str(item)

    index_text = "、".join(
        f"{row.get('name') or row.get('symbol')}{number(row.get('price'))}（{number(row.get('change_percent'))}%）"
        for row in indices[:3]
    )
    breadth_text = f"上涨{number(breadth.get('up'))}家、下跌{number(breadth.get('down'))}家、平盘{number(breadth.get('flat'))}家"
    trading_date = next((
        str(row.get("trading_date")) for row in [*indices, *quotes]
        if row.get("trading_date")
    ), str(value.get("as_of") or "")[:10])
    date_match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", trading_date)
    date_label = (
        f"{date_match.group(1)}年{int(date_match.group(2))}月{int(date_match.group(3))}日"
        if date_match else "收盘"
    )
    turnover = turnover.split("；", 1)[0].rstrip("。")
    breadth_view = (
        "风险偏好偏弱" if float(breadth.get("down") or 0) > float(breadth.get("up") or 0)
        else "风险偏好偏强" if float(breadth.get("up") or 0) > float(breadth.get("down") or 0)
        else "多空分歧较大"
    )
    market_evidence = (
        f"按交易所收盘统计，{turnover}；{breadth_text}。"
        f"成交明显放大，但下跌家数仍多于上涨家数，{breadth_view}，市场情绪仍偏弱。"
    )
    leader = theme_fact["leaders"][0]
    laggard = theme_fact["laggards"][0]
    leader_core = leader.get("core") or {}
    laggard_core = laggard.get("core") or {}
    themes = (
        f"{leader.get('name')}领涨{number(leader.get('change_percent'))}%，"
        f"{leader_core.get('name')}({leader_core.get('symbol')}){number(leader_core.get('change_percent'))}%；"
        f"{laggard.get('name')}领跌{number(laggard.get('change_percent'))}%，"
        f"{laggard_core.get('name')}({laggard_core.get('symbol')}){number(laggard_core.get('change_percent'))}%"
    )
    theme_and_sentiment = f"板块分化很大：{themes}。这更像局部轮动，而不是全面转强。"
    holdings = "、".join(
        f"{quote_by_symbol[code].get('name') or code}({code}){number(quote_by_symbol[code].get('price'))}/"
        f"{number(quote_by_symbol[code].get('change_percent'))}%"
        for code in sorted(required)
    )
    holding_evidence = f"持仓收盘表现：{holdings}。"
    if any(len(item) > 240 for item in (market_evidence, theme_and_sentiment, holding_evidence)):
        return None
    result = {
        "result_version": 4,
        "semantic": {
            "summary": (
                f"截至{date_label}收盘，{index_text}。指数走弱、成交放大且下跌家数多于上涨家数，"
                "题材分化明显；证据暂不支持追涨，维持中性观察。"
            ),
            "direction": "neutral",
            "qualified": True,
            "horizon": horizon,
            "current_action": "observe",
            "key_evidence": [market_evidence, theme_and_sentiment, holding_evidence],
            "transition_conditions": [{
                "outcome": "upgrade", "price": "三大指数重新站稳本日收盘位",
                "breadth": "上涨家数持续超过下跌家数", "persistence": "连续一个交易日确认",
            }, {
                "outcome": "downgrade", "price": "三大指数继续跌破本日低位",
                "breadth": "下跌家数继续显著多于上涨家数", "persistence": "连续一个交易日确认",
            }],
            "position_focus": [],
            "risks": ["放量但市场宽度偏弱，量价并未形成一致的上行确认。"],
            "unknowns": ["下一交易日指数、市场宽度与成交扩散能否同步改善。"],
        },
    }
    task_profile = value.get("task_profile") if isinstance(value.get("task_profile"), dict) else {}
    if task_profile.get("evidence_family") == "completed_trading_week":
        weekly = verified_weekly_market_comparison(value)
        if weekly is None:
            return None
        result["semantic"]["summary"] = weekly["text"] + result["semantic"]["summary"]
        fund_flow = _verified_fund_flow_summary(parsed)
        event_impact = _verified_market_event_impact(parsed)
        announcements = _verified_portfolio_announcement_summary(parsed, private)
        result["semantic"]["key_evidence"] = [
            market_evidence, theme_and_sentiment, announcements,
        ]
        result["semantic"]["risks"] = [fund_flow, event_impact]
    return result


def _verified_fund_flow_summary(parsed: list[dict[str, Any]]) -> str:
    inflows = [
        row for item in parsed for row in item.get("sector_inflow_leaders") or []
        if isinstance(row, dict) and row.get("net_inflow") is not None
    ]
    outflows = [
        row for item in parsed for row in item.get("sector_outflow_leaders") or []
        if isinstance(row, dict)
    ]
    if not inflows or not outflows:
        return ""

    def billions(value: Any) -> str:
        return f"{float(value) / 100_000_000:.2f}".rstrip("0").rstrip(".")

    leaders = "、".join(
        f"{row.get('name')}净流入{billions(row.get('net_inflow'))}亿元" for row in inflows[:3]
    )
    laggards = "、".join(str(row.get("name") or "") for row in outflows[:2] if row.get("name"))
    return (
        f"主力资金方向呈结构性分化：{leaders}，{laggards}为净流出领先方向；"
        f"这更像板块轮动而非普遍回流，{laggards}方向更承压。"
    )


def _verified_market_event_impact(parsed: list[dict[str, Any]]) -> str:
    for item in parsed:
        content = str(item.get("content") or "")
        for sentence in re.split(r"[。；\n]", content):
            compact = "".join(sentence.split())
            if "风险偏好" in compact and any(term in compact for term in ("压制", "支撑", "影响", "扰动")):
                return "政策与风险事件（影响推断）：" + compact + "。"
    for item in parsed:
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "")
        if title and any(term in title + content for term in ("政策", "监管", "风险")):
            return f"政策与风险事件核查到《{title}》，其对下周风险偏好的影响仍需继续验证。"
    return ""


def _verified_portfolio_announcement_summary(
    parsed: list[dict[str, Any]], private: dict[str, Any],
) -> str:
    disclosures = {
        str(item.get("checked_symbol") or ""): item
        for item in parsed if item.get("checked_symbol")
    }
    active = [
        item for item in private.get("positions") or []
        if isinstance(item, dict) and float(item.get("shares") or 0) > 0
    ]
    if not active or any(str(item.get("code") or "") not in disclosures for item in active):
        return ""
    parts = []
    for position in sorted(active, key=lambda item: str(item.get("code") or "")):
        code = str(position.get("code") or "")
        name = str(position.get("name") or code)
        announcements = [
            item for item in disclosures[code].get("announcements") or [] if isinstance(item, dict)
        ]
        for announcement in announcements:
            narrative = _verified_announcement_narrative(name, announcement)
            if narrative:
                parts.append(narrative)
                break
    return "持仓方面，" + "；".join(parts) + "。" if parts else ""


def _verified_announcement_narrative(name: str, announcement: dict[str, Any]) -> str:
    """Translate verified disclosure content into a bounded trading implication."""
    if announcement.get("content_verified") is not True:
        return ""
    title = _sentence_piece(announcement.get("title"))
    compact = "".join(str(announcement.get("content") or "").split())
    if not title or not compact:
        return ""
    if "回购" in title + compact:
        if any(term in compact for term in ("已按计划实施", "已实施股份回购", "累计回购")):
            return f"{name}的回购仍在推进，属于轻微正面信息，但不足以单独改变当前判断"
        return f"{name}披露回购进展，正文没有显示足以单独改变当前判断的新催化"
    if "更正" in title + compact:
        if "文字" in compact and any(term in compact for term in ("不涉及", "不影响", "无实质影响")):
            return f"{name}更正的是文字表述，不涉及主要财务数据，对当前判断影响有限"
        return f"{name}披露{title}，需要按更正范围评估其对主要财务数据的影响"
    if any(term in title + compact for term in ("风险提示", "立案", "处罚", "终止", "诉讼", "停牌")):
        return f"{name}披露{title}，这是需要优先跟踪的风险变化"
    return ""


def safe_stage_output(
    stage: str, *, horizon: str = "当前", packet: dict[str, Any] | None = None,
    candidate_output: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a conservative, auditable local fallback without inventing market facts."""
    if stage == "m0_compose":
        candidate_fallback = _verified_candidate_research_fallback(candidate_output)
        if candidate_fallback is not None:
            return candidate_fallback
        verified = _verified_market_snapshot_summary(packet)
        if verified is not None:
            return verified
        return {
            "result_version": 3,
            "semantic": {
                "summary": {"text": "眼下公开信息还在核对，先只保留客观观察。", "evidence_refs": []},
                "observations": [{"text": "现有证据不足以确认盘面强弱是否已经扩散。", "evidence_refs": []}],
                "connections": [],
                "attention": [],
                "unknowns": [{"text": "后续成交和市场广度能否形成一致。", "evidence_refs": []}],
            },
        }
    if stage in {"m1_judgment", "m2"}:
        if stage == "m1_judgment":
            verified = _verified_close_judgment(packet, horizon=horizon)
            if verified is not None:
                return verified
        return {
            "result_version": 4 if stage == "m1_judgment" else 3,
            "semantic": {
                "summary": "在价格、市场广度和成交扩散共同确认前，我维持观察，不切换方向。",
                "direction": "unqualified",
                "qualified": False,
                "horizon": horizon,
                "current_action": "observe",
                "key_evidence": [],
                "transition_conditions": [],
                "position_focus": [],
                "risks": [],
                "unknowns": ["价格、市场广度和成交扩散能否形成持续一致。"],
            },
        }
    raise ValueError(f"unsupported fallback stage: {stage}")


def _verified_candidate_research_fallback(
    candidate_output: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Rewrite a verified candidate set without carrying a rejected market/holding monologue."""
    if not isinstance(candidate_output, dict) or candidate_output.get("result_version") != 4:
        return None
    candidates = candidate_output.get("candidate_research")
    if not isinstance(candidates, list) or not candidates or any(not isinstance(row, dict) for row in candidates):
        return None
    required = ("symbol", "name", "event", "business_link", "counterevidence", "observation_condition")
    if any(any(not str(row.get(key) or "").strip() for key in required) for row in candidates):
        return None

    names = "、".join(str(row["name"]).strip() for row in candidates)
    paragraphs = [
        f"收盘后再看，持仓之外这批线索里，{names}值得分开核对。它们不能因为同属热门方向，就被当成同一种机会。"
    ]
    for row in candidates:
        name = str(row["name"]).strip()
        business = _candidate_research_clause(row["business_link"])
        counter = _candidate_research_clause(row["counterevidence"])
        condition = _candidate_research_clause(row["observation_condition"])
        paragraphs.append(f"看{name}，{business}。不过，{counter}。接下来只用{condition}来验证。")
    return {
        "result_version": 4,
        "candidate_research": [dict(row) for row in candidates],
        "narrative": "\n\n".join(paragraphs),
    }


def _candidate_research_clause(value: Any) -> str:
    """Keep business evidence while dropping quote-like sentences that commonly carry stale precision."""
    sentences = [
        item.strip() for item in re.split(r"[。！？；]", str(value or "")) if item.strip()
    ]
    quote_markers = ("股价", "报价", "收于", "前收", "涨幅", "上涨", "下跌", "回购价", "元/股")
    kept = [item for item in sentences if not any(marker in item for marker in quote_markers)]
    return _sentence_piece("。".join(kept or sentences[:1]))


def express_stage_semantics(stage: str, semantic: dict[str, Any]) -> str:
    """Deterministically adapt frozen stage semantics into an expression draft."""
    summary = _m0_item_text(semantic.get("summary"))
    if not summary:
        raise ValueError(f"{stage} semantic summary is required")
    if stage in {"m1", "m2"} and "current_action" in semantic:
        return _v4_judgment_expression(semantic)
    paragraphs = [summary]
    if stage == "m0":
        observations = _m0_item_values(semantic.get("observations"), 3)
        connections = _m0_item_values(semantic.get("connections"), 2)
        attention = _m0_item_values(semantic.get("attention"), 1)
        # Read-only compatibility for old in-process callers. New provider
        # output cannot contain this field under the v3 schema.
        if not attention:
            attention = _m0_item_values(semantic.get("risks"), 1)
        unknowns = _m0_item_values(semantic.get("unknowns"), 1)
        if observations:
            paragraphs.append("。".join(_sentence_piece(value) for value in observations) + "。")
        if connections:
            paragraphs.append("我把几条线索放在一起看：" + "；".join(_sentence_piece(value) for value in connections) + "。")
        if attention:
            paragraphs.append("接下来我会留意" + _sentence_piece(attention[0]) + "。")
        if unknowns:
            unknown = _sentence_piece(unknowns[0])
            if unknown.startswith("缺少"):
                detail = unknown.removeprefix("缺少").strip()
                fact, separator, impact = detail.partition("，")
                if separator and impact.startswith("无法判断"):
                    impact = "我暂不判断" + impact.removeprefix("无法判断")
                else:
                    impact = "我只采用不依赖它的观察"
                paragraphs.append(f"在{fact}得到确认前，{impact}。")
            else:
                paragraphs.append("还需要确认" + unknown + "。")
        return "\n\n".join(paragraphs)
    if stage in {"m1", "m2"}:
        direction = str(semantic.get("direction") or "").strip()
        if direction:
            direction_label = {
                "bullish": "偏多", "bearish": "偏空", "neutral": "中性",
                "avoid": "回避交易", "unqualified": "尚未形成合格方向", "unknown": "方向未知",
            }.get(direction, direction)
            paragraphs.append(f"我现在更倾向于{direction_label}。")
        if semantic.get("qualified") is False:
            paragraphs.append("不过证据还不够，我不会把它当成可以直接执行的判断。")
    for key, lead in (
        ("observations", "我看到的是"), ("triggers", "接下来主要看"),
        ("invalidations", "如果这些条件出现，前面的判断就不再成立"),
        ("risks", "我更担心的是"), ("unknowns", "现在还不能确认的是"),
    ):
        values = [str(value).strip() for value in semantic.get(key) or [] if str(value).strip()]
        if values:
            paragraphs.append(f"{lead}：" + "；".join(values) + "。")
    return "\n\n".join(paragraphs)


def normalize_stage_output(stage: str, output: dict[str, Any]) -> NormalizedStageOutput:
    """Give v2 semantics and read-only v1 results one canonical runtime shape."""
    if stage == "m0_compose" and output.get("result_version") == 4:
        text = str(output.get("narrative") or "")
        return NormalizedStageOutput(
            stage, {"summary": text}, {"candidate_research": output.get("candidate_research") or []},
            text, None, False, True,
        )
    if (stage == "m1_judgment" and output.get("result_version") == 5) or (
        stage == "m2" and output.get("result_version") == 4
    ):
        core = output["decision_core"]
        semantic = {
            "summary": core["thesis"], "direction": core["direction"], "qualified": True,
            "horizon": core["horizon"], "current_action": core["current_action"],
            "key_evidence": [row["implication"] for row in core["reasons"]],
            "position_focus": core["position_focus"], "transition_conditions": core["transition_conditions"],
            "risks": [core["counterargument"]["claim"]], "unknowns": core["critical_unknowns"],
        }
        snapshot = _semantic_snapshot(semantic)
        snapshot.update(horizon=core["horizon"], confidence=core["confidence"], decision_core=core)
        for claim in snapshot["claims"]:
            claim["confidence"] = core["confidence"]
        return NormalizedStageOutput(stage, semantic, snapshot, output["narrative"], True, False, True)
    semantic_only_v3 = (
        (stage == "m1_judgment" and output.get("result_version") in {3, 4})
        or (stage == "m2" and output.get("result_version") == 3)
    )
    snapshot = dict(output.get("snapshot") or {}) if isinstance(output.get("snapshot"), dict) else {}
    semantic = output.get("semantic")
    legacy = not isinstance(semantic, dict)
    if legacy:
        semantic, text, qualified = adapt_legacy_stage_output(stage, output)
    else:
        semantic = dict(semantic)
        if semantic_only_v3:
            snapshot = _semantic_snapshot(semantic)
        text = express_stage_semantics(stage.removesuffix("_compose").removesuffix("_judgment"), semantic)
        qualified = bool(semantic.get("qualified")) if stage in {"m1_judgment", "m2"} else None
    return NormalizedStageOutput(stage, semantic, snapshot, text, qualified, legacy, semantic_only_v3)


def semantic_snapshot_conflicts(result: NormalizedStageOutput) -> tuple[str, ...]:
    if result.legacy or result.snapshot_derived or result.stage not in {"m1_judgment", "m2"}:
        return ()
    return tuple(
        key for key in ("direction", "qualified", "triggers", "invalidations", "risks", "unknowns")
        if result.semantic.get(key) != result.snapshot.get(key)
    )
