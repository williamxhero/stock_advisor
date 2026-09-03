from __future__ import annotations

from dataclasses import dataclass
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
        str(condition.get(key) or "").strip()
        for key in ("price", "breadth", "persistence")
        if str(condition.get(key) or "").strip()
    )


def _clean_values(values: Any, limit: int) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip() for value in values or [] if str(value).strip()
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
    elif evidence:
        paragraphs.append("，".join(evidence) + "。")
    conditions = [item for item in semantic.get("transition_conditions") or [] if isinstance(item, dict)]
    for condition in conditions:
        text = _condition_text(condition)
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
            reason = str(item.get("reason") or "相对结构需要继续确认").strip()
            parts.append(f"优先盯{symbol}，{reason}")
        paragraphs.append("。".join(parts) + "。")
    unknowns = _clean_values(semantic.get("unknowns"), 1)
    if unknowns:
        paragraphs.append(f"真正还需要确认的是{unknowns[0]}。")
    return "\n\n".join(paragraphs)


def safe_stage_output(stage: str, *, horizon: str = "当前") -> dict[str, Any]:
    """Return a conservative, auditable local fallback without inventing market facts."""
    if stage == "m0_compose":
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
        return {
            "result_version": 4 if stage == "m1_judgment" else 3,
            "semantic": {
                "summary": "现有可靠证据还不足以支持方向切换。",
                "direction": "unqualified",
                "qualified": False,
                "horizon": horizon,
                "current_action": "observe",
                "key_evidence": ["尚未取得可通过质量校验的完整判断结果。"],
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
            paragraphs.append("。".join(observations) + "。")
        if risks:
            paragraphs.append("要留意" + risks[0] + "。")
        if unknowns:
            paragraphs.append("还需要确认" + unknowns[0] + "。")
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
