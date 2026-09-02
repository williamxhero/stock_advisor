from __future__ import annotations

from dataclasses import dataclass
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
            snapshot = {
                key: semantic.get(key)
                for key in ("direction", "qualified", "triggers", "invalidations", "risks", "unknowns")
            }
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
