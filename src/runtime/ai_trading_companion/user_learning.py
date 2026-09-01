from __future__ import annotations

import json
import re
from typing import Any


def explicit_expression_preference(message: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    text = str(message["body_text"])
    if "这次" in text or "本次" in text:
        return None
    value = None
    predicate = None
    if "以后少贴原文" in text or "以后少发原文" in text:
        predicate, value = "expression.material_density", "summary_and_link"
    elif "以后多贴原文" in text:
        predicate, value = "expression.material_density", "more_source_excerpt"
    elif any(token in text for token in ("以后简短点", "以后少说点", "以后别太长")):
        predicate, value = "expression.length", "concise"
    elif any(token in text for token in ("以后详细点", "以后多说一点", "以后说详细些")):
        predicate, value = "expression.length", "detailed"
    elif any(token in text for token in ("以后直接点", "以后直说")):
        predicate, value = "expression.tone", "direct"
    elif any(token in text for token in ("以后语气柔和点", "以后委婉点")):
        predicate, value = "expression.tone", "gentle"
    else:
        address = re.search(r"以后叫我([^，。！？!?\s]{1,12})", text)
        if address:
            predicate, value = "expression.address", address.group(1)
    if value is None:
        return None
    prior = previous.get(predicate) if isinstance(previous, dict) and predicate in previous else previous
    return {
        "kind": "user_fact", "subject": "user.expression", "predicate": predicate,
        "object": {"value": value, "scope": "long_term", "explicit": True}, "confidence": 1.0,
        "supersedes_id": prior["proposition_id"] if isinstance(prior, dict) and prior.get("proposition_id") else None,
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
