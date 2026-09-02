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
    subjects = list(dict.fromkeys(re.findall(r"(?<!\d)\d{6}(?!\d)", summary)))
    claims = []
    if summary and direction != "unknown":
        claims.append({
            "subjects": subjects,
            "direction": direction,
            "horizon": None,
            "triggers": [str(item) for item in semantic.get("triggers") or []],
            "invalidations": [str(item) for item in semantic.get("invalidations") or []],
            "confidence": None,
            "benchmark": None,
            "original_text": summary,
        })
    return {
        "subjects": subjects,
        "direction": direction,
        "qualified": bool(semantic.get("qualified")),
        "triggers": [str(item) for item in semantic.get("triggers") or []],
        "invalidations": [str(item) for item in semantic.get("invalidations") or []],
        "risks": [str(item) for item in semantic.get("risks") or []],
        "unknowns": [str(item) for item in semantic.get("unknowns") or []],
        "original_claims": [summary] if summary else [],
        "claims": claims,
    }


def express_stage_semantics(stage: str, semantic: dict[str, Any]) -> str:
    """Deterministically adapt frozen stage semantics into an expression draft."""
    summary = str(semantic.get("summary") or "").strip()
    if not summary:
        raise ValueError(f"{stage} semantic summary is required")
    paragraphs = [summary]
    if stage in {"m1", "m2"}:
        direction = str(semantic.get("direction") or "").strip()
        if direction:
            paragraphs.append(f"我现在更倾向于{direction}。")
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
    semantic_only_v3 = stage == "m1_judgment" and output.get("result_version") == 3
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
