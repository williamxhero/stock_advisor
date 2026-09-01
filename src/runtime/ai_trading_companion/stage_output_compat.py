from __future__ import annotations

from typing import Any


def adapt_legacy_stage_output(stage: str, output: dict[str, Any]) -> tuple[dict[str, Any], str, bool | None]:
    """Read a sealed v1 checkpoint without granting it direct publication rights."""
    snapshot = dict(output.get("snapshot") or {}) if isinstance(output.get("snapshot"), dict) else {}
    field = {"m0_compose": "m0_markdown", "m1_judgment": "m1_markdown", "m2": "m2_markdown"}.get(stage)
    if field is None:
        return {}, "", None
    text = str(output.get(field) or "")
    semantic: dict[str, Any] = {"summary": text, "legacy_adapter": True}
    for key in ("direction", "qualified", "triggers", "invalidations", "risks", "unknowns"):
        if key in snapshot:
            semantic[key] = snapshot[key]
    qualified = (
        bool(output["judgment_qualified"])
        if stage == "m1_judgment" and "judgment_qualified" in output
        else bool(snapshot["qualified"])
        if stage in {"m1_judgment", "m2"} and "qualified" in snapshot
        else None
    )
    return semantic, text, qualified
