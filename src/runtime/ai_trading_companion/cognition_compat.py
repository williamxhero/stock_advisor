from __future__ import annotations

from typing import Any


def adapt_legacy_cognition_result(result: dict[str, Any]) -> dict[str, Any]:
    """Read an old cognition result into v2 semantics without publishing it directly."""
    if "answer" in result:
        return result
    legacy = result.get("reply_markdown")
    return {
        **{key: value for key, value in result.items() if key != "reply_markdown"},
        "answer": None if legacy is None else {"points": [str(legacy)], "material_ids": []},
    }
