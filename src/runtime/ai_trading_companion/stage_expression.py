from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedStageOutput:
    stage: str
    semantic: dict[str, Any]
    snapshot: dict[str, Any]
    text: str
    qualified: bool | None
    legacy: bool


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


def legacy_stage_semantics(stage: str, markdown: str, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only adapter for v1 checkpoints; output still re-enters the v2 expression gate."""
    result: dict[str, Any] = {"summary": str(markdown), "legacy_adapter": True}
    if isinstance(snapshot, dict):
        for key in ("direction", "qualified", "triggers", "invalidations", "risks", "unknowns"):
            if key in snapshot:
                result[key] = snapshot[key]
    return result


def normalize_stage_output(stage: str, output: dict[str, Any]) -> NormalizedStageOutput:
    """Give v2 semantics and read-only v1 results one canonical runtime shape."""
    snapshot = dict(output.get("snapshot") or {}) if isinstance(output.get("snapshot"), dict) else {}
    semantic = output.get("semantic")
    legacy = not isinstance(semantic, dict)
    if legacy:
        field = {"m0_compose": "m0_markdown", "m1_judgment": "m1_markdown", "m2": "m2_markdown"}.get(stage)
        if field is None:
            return NormalizedStageOutput(stage, {}, snapshot, "", None, True)
        semantic = legacy_stage_semantics(stage.removesuffix("_compose").removesuffix("_judgment"), str(output.get(field) or ""), snapshot)
        text = str(output.get(field) or "")
        qualified = (
            bool(output["judgment_qualified"]) if stage == "m1_judgment" and "judgment_qualified" in output
            else bool(snapshot["qualified"]) if stage in {"m1_judgment", "m2"} and "qualified" in snapshot
            else None
        )
    else:
        semantic = dict(semantic)
        text = express_stage_semantics(stage.removesuffix("_compose").removesuffix("_judgment"), semantic)
        qualified = bool(semantic.get("qualified")) if stage in {"m1_judgment", "m2"} else None
    return NormalizedStageOutput(stage, semantic, snapshot, text, qualified, legacy)


def semantic_snapshot_conflicts(result: NormalizedStageOutput) -> tuple[str, ...]:
    if result.legacy or result.stage not in {"m1_judgment", "m2"}:
        return ()
    return tuple(
        key for key in ("direction", "qualified", "triggers", "invalidations", "risks", "unknowns")
        if result.semantic.get(key) != result.snapshot.get(key)
    )
