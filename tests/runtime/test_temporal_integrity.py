from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.acquisition import AcquisitionBoundary
from ai_trading_companion.evidence_qualification import qualify_record
from ai_trading_companion.temporal_integrity import (
    VERSION,
    qualify_temporal,
    replay_records,
    resolve_temporal,
    validate_replay,
)


AS_OF = "2026-09-21T01:30:00Z"


def _observe(row: dict) -> dict:
    boundary = AcquisitionBoundary("temporal-test")
    observation, _ = boundary.observe(
        "fixture", {}, {"backend": "market", "results": [row]}, True,
    )
    return observation["evidence_items"][0]["evidence_spec"]


def test_runtime_owns_known_at_and_source_publication_is_retained_separately():
    record = _observe({
        "url": "https://example.test/news", "title": "公告",
        "excerpt_text": "公司发生事件", "evidence_kind": "news_disclosure",
        "effective_time": "2026-09-21T01:20:00Z",
        "published_at": "2026-09-21T01:25:00Z",
    })

    temporal = record["temporal_integrity"]
    assert temporal["contract"] == VERSION
    assert record["occurred_at"] == "2026-09-21T01:20:00Z"
    assert record["published_at"] == "2026-09-21T01:25:00Z"
    assert temporal["known_at_source"].startswith("runtime_observation.")
    assert temporal["known_at"] == record["known_at"]


@pytest.mark.parametrize(
    ("kind", "row", "expected"),
    [
        ("market_fact", {"market_time": "2026-09-21T01:00:00Z", "fact_as_of": "2026-09-21T01:01:00Z"}, "2026-09-21T01:00:00Z"),
        ("derived_calculation", {"calculation_time": "2026-09-21T01:02:00Z", "fact_as_of": "2026-09-21T01:03:00Z"}, "2026-09-21T01:02:00Z"),
        ("quant_research", {"research_as_of": "2026-09-21T01:04:00Z", "published_at": "2026-09-21T01:05:00Z"}, "2026-09-21T01:04:00Z"),
        ("news_disclosure", {"effective_time": "2026-09-21T01:06:00Z", "published_at": "2026-09-21T01:07:00Z"}, "2026-09-21T01:06:00Z"),
    ],
)
def test_kind_specific_occurrence_precedence_is_deterministic(kind, row, expected):
    resolved = resolve_temporal({"evidence_kind": kind, **row}, {
        "acquired_at": "2026-09-21T01:10:00Z",
    })
    assert resolved["occurred_at"] == expected
    assert resolved["occurred_at_source"]


def test_conflicting_source_clocks_are_degraded_and_replay_is_stable():
    resolved = resolve_temporal({
        "evidence_kind": "news_disclosure",
        "effective_time": "2026-09-21T01:20:00Z",
        "fact_as_of": "2026-09-21T01:21:00Z",
        "published_at": "2026-09-21T01:22:00Z",
    }, {"acquired_at": "2026-09-21T01:25:00Z"})
    assert resolved["state"] == "degraded"
    assert "occurred_time_conflict_precedence_applied" in resolved["reasons"]
    assert qualify_temporal(resolved, as_of=AS_OF)["state"] == "degraded"

    first = replay_records([{"record_id": "b", "temporal_integrity": resolved}, {"record_id": "a", "temporal_integrity": resolved}], as_of=AS_OF)
    second = replay_records([{"record_id": "a", "temporal_integrity": resolved}, {"record_id": "b", "temporal_integrity": resolved}], as_of=AS_OF)
    assert first == second
    validate_replay(first)


def test_future_known_publication_and_occurrence_are_all_rejected():
    late_known = _observe({
        "url": "https://example.test/late", "excerpt_text": "later",
        "fact_as_of": "2026-09-21T01:00:00Z",
        "published_at": "2026-09-21T01:10:00Z",
    })
    late_known["temporal_integrity"] = resolve_temporal(
        {"evidence_kind": "news_disclosure", "fact_as_of": "2026-09-21T01:00:00Z", "published_at": "2026-09-21T01:10:00Z"},
        {"acquired_at": "2026-09-21T02:00:00Z"},
    )
    late_known["known_at"] = late_known["temporal_integrity"]["known_at"]
    late_known["occurred_at"] = late_known["temporal_integrity"]["occurred_at"]
    late_known["published_at"] = late_known["temporal_integrity"]["published_at"]
    late_known["record_id"] = __import__("hashlib").sha256(
        json.dumps({k: v for k, v in late_known.items() if k != "record_id"}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    qualification = qualify_record(late_known, as_of=AS_OF)
    assert qualification["state"] == "rejected"
    assert "known_at_after_as_of" in qualification["reasons"]

    future = resolve_temporal({
        "evidence_kind": "news_disclosure",
        "effective_time": "2026-09-21T02:00:00Z",
        "published_at": "2026-09-21T02:05:00Z",
    }, {"acquired_at": "2026-09-21T02:06:00Z"})
    replay = replay_records([{"record_id": "future", "temporal_integrity": future}], as_of=AS_OF)
    assert replay["passed"] is False
    assert replay["records"][0]["state"] == "rejected"
    assert {"occurred_at_after_as_of", "published_at_after_as_of", "known_at_after_as_of"}.issubset(
        set(replay["records"][0]["reasons"])
    )


def test_temporal_schema_and_installation_qualification_are_bound():
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/temporal-integrity-spec-v1.schema.json").read_text(encoding="utf-8"))
    envelope = resolve_temporal({"evidence_kind": "market_fact", "market_time": AS_OF}, {"acquired_at": AS_OF})
    Draft202012Validator(schema).validate(envelope)
    script = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\temporal-integrity-spec-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\temporal_integrity.py" in script


def test_temporal_replay_rejects_tampering_without_mutating_input():
    envelope = resolve_temporal({"evidence_kind": "market_fact", "market_time": AS_OF}, {"acquired_at": AS_OF})
    original = copy.deepcopy(envelope)
    replay = replay_records([{"record_id": "one", "temporal_integrity": envelope}], as_of=AS_OF)
    replay["records"][0]["known_at"] = "2026-09-20T00:00:00Z"
    with pytest.raises(ValueError, match="hash"):
        validate_replay(replay)
    assert envelope == original
