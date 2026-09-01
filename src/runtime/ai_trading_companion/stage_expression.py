from __future__ import annotations

from typing import Any


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
