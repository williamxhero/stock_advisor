from __future__ import annotations

import json
from typing import Any


def explicit_expression_preference(message: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    text = str(message["body_text"])
    if "这次" in text or "本次" in text:
        return None
    value = None
    if "以后少贴原文" in text or "以后少发原文" in text:
        value = "summary_and_link"
    elif "以后多贴原文" in text:
        value = "more_source_excerpt"
    if value is None:
        return None
    predicate = "expression.material_density"
    return {
        "kind": "user_fact", "subject": "user.expression", "predicate": predicate,
        "object": {"value": value, "scope": "long_term", "explicit": True}, "confidence": 1.0,
        "supersedes_id": previous["proposition_id"] if previous else None,
        "source_span": {"message_id": message["message_id"], "start": 0, "end": len(text), "quote": text},
    }


def user_method_claim(message: dict[str, Any]) -> dict[str, Any] | None:
    text = str(message["body_text"])
    marker = next((token for token in ("因为", "依据", "逻辑是") if token in text), None)
    if marker is None or not any(token in text for token in ("看多", "看空", "诱多", "诱空", "反包", "承接")):
        return None
    claim, reason = text.split(marker, 1)
    if not reason.strip():
        return None
    return {
        "kind": "user_view", "subject": "user.market_method", "predicate": "hypothesis",
        "object": {"claim": claim.strip(), "reason": reason.strip(), "status": "unverified", "evidence_count": 1},
        "confidence": 0.25, "supersedes_id": None,
        "source_span": {"message_id": message["message_id"], "start": 0, "end": len(text), "quote": text},
    }


def expression_profile(rows: list[dict[str, Any]]) -> dict[str, Any]:
    for row in rows:
        if row["subject"] == "user.expression" and row["predicate"] == "expression.material_density":
            return json.loads(row["object_json"])
    return {}
