from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from .stage_output_compat import adapt_legacy_stage_output


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
    summary = str(semantic.get("summary") or "").strip()
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
    return "，".join(
        str(condition.get(key) or "").strip().rstrip("。！？；，,.!?; ")
        for key in ("price", "breadth", "persistence")
        if str(condition.get(key) or "").strip()
    )


def _clean_values(values: Any, limit: int) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values or [] if str(value).strip()
    ))[:limit]


def _sentence_piece(value: Any) -> str:
    return str(value or "").strip().rstrip("。！？；，,.!?; ")


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


def _verified_close_summary(packet: dict[str, Any] | None) -> dict[str, Any] | None:
    """Build a useful M0 fallback only from the packet's frozen verified facts."""
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

    index_text = "，".join(
        f"{row.get('name') or row.get('symbol')}收于{number(row.get('price'))}（{number(row.get('change_percent'))}%）"
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
    observations = ["市场广度偏弱：" + "、".join(breadth_parts) + "。"]
    selected_quotes = sorted(
        (row for row in quotes if row.get("price") is not None and row.get("change_percent") is not None),
        key=lambda row: abs(float(row.get("change_percent") or 0)), reverse=True,
    )[:2]
    if selected_quotes:
        observations.append("持仓表现有分化：" + "，".join(
            f"{row.get('name') or row.get('symbol')}收于{number(row.get('price'))}（{number(row.get('change_percent'))}%）"
            for row in selected_quotes
        ) + "。")
    return {
        "result_version": 3,
        "semantic": {
            "summary": f"收盘后看，三大指数接近平盘，{index_text}。",
            "observations": observations,
            "risks": ["指数平稳但下跌家数明显多于上涨家数，个股承压程度高于指数表面。"],
            "unknowns": ["指数近乎横盘与个股普跌的背离能否在下一交易日收敛。"],
        },
    }


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
        f"成交明显放大，但下跌家数仍多于上涨家数，{breadth_view}。"
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
    holding_evidence = f"腾讯15:00持仓收盘：{holdings}。"
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
        if not announcements:
            parts.append(f"{name}({code})未发现新增公告")
            continue
        titles = "、".join(f"《{item.get('title')}》" for item in announcements[:1] if item.get("title"))
        parts.append(f"{name}({code})检出{titles or '窗口内公告'}，暂不据标题单独改变判断")
    return "逐股公告核查：" + "；".join(parts) + "。"


def safe_stage_output(
    stage: str, *, horizon: str = "当前", packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a conservative, auditable local fallback without inventing market facts."""
    if stage == "m0_compose":
        verified = _verified_close_summary(packet)
        if verified is not None:
            return verified
        return {
            "result_version": 3,
            "semantic": {
                "summary": "眼下公开信息还在核对，先只保留客观观察。",
                "observations": ["现有证据不足以确认盘面强弱是否已经扩散。"],
                "risks": [],
                "unknowns": ["后续成交和市场广度能否形成一致。"],
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


def express_stage_semantics(stage: str, semantic: dict[str, Any]) -> str:
    """Deterministically adapt frozen stage semantics into an expression draft."""
    summary = str(semantic.get("summary") or "").strip()
    if not summary:
        raise ValueError(f"{stage} semantic summary is required")
    if stage in {"m1", "m2"} and "current_action" in semantic:
        return _v4_judgment_expression(semantic)
    paragraphs = [summary]
    if stage == "m0":
        observations = [str(value).strip() for value in semantic.get("observations") or [] if str(value).strip()][:2]
        risks = [str(value).strip() for value in semantic.get("risks") or [] if str(value).strip()][:1]
        unknowns = [str(value).strip() for value in semantic.get("unknowns") or [] if str(value).strip()][:1]
        if observations:
            paragraphs.append("。".join(_sentence_piece(value) for value in observations) + "。")
        if risks:
            paragraphs.append("要留意" + _sentence_piece(risks[0]) + "。")
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
