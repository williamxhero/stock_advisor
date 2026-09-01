from __future__ import annotations

from typing import Any


def express_cognition_answer(answer: dict[str, Any]) -> str:
    points = [str(point).strip() for point in answer.get("points") or [] if str(point).strip()]
    materials = [f"[[material:{material_id}]]" for material_id in answer.get("material_ids") or []]
    return "\n\n".join(points + materials)
