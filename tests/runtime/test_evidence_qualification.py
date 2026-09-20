import copy
import json
from pathlib import Path

from ai_trading_companion.acquisition import AcquisitionBoundary
from ai_trading_companion.evidence_qualification import (
    POLICY_VERSION,
    VERSION,
    qualification_for_records,
    qualify_record,
    validate_qualification,
)
from ai_trading_companion.evidence_spec import from_observation
from ai_trading_companion.memory_evidence import MemoryEvidenceRegistrar
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from jsonschema import Draft202012Validator

AS_OF = "2026-09-20T08:00:00Z"


def make_record(**changes):
    item = {
        "evidence_ref": "ev-1",
        "url": "https://source.test/item",
        "title": "Market item",
        "source_identity": "source.test",
        "excerpt_text": "The observed market event.",
        "fact_as_of": "2026-09-20T07:00:00Z",
        "known_at": "2026-09-20T07:05:00Z",
        "factual_status": "verified",
        "market_propagation": "not_observed",
        "claims": [],
    }
    item.update(changes)
    observation = {
        "attempt_id": "attempt-1", "observation_id": "observation-1",
        "operation": "search", "backend": "web",
        "acquired_at": "2026-09-20T07:05:00Z",
    }
    return from_observation(item, observation)


def test_qualification_is_standalone_versioned_and_schema_valid():
    record = make_record()
    qualification = qualify_record(record, as_of=AS_OF, source_refs=("ev-1", "https://source.test/item"))

    assert qualification["contract"] == VERSION
    assert qualification["qualification_policy_version"] == POLICY_VERSION
    assert qualification["state"] == "qualified"
    assert qualification["input_record_refs"][0]["record_id"] == record["record_id"]
    assert qualification["qualification_id"]
    validate_qualification(qualification)
    schema = json.loads((Path(__file__).parents[2] / "resources/contracts/evidence-qualification-spec-v1.schema.json").read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(qualification)) == []


def test_missing_occurrence_and_screenshot_are_deterministically_degraded():
    missing_time = qualify_record(make_record(fact_as_of=None, published_at=None))
    screenshot = qualify_record(make_record(screenshot_only=True))

    assert missing_time["state"] == "degraded"
    assert "occurrence_unknown" in missing_time["reasons"]
    assert screenshot["state"] == "degraded"
    assert "weak_source_screenshot_only" in screenshot["reasons"]
    assert screenshot["permitted_use"] == "context_only"


def test_unverified_content_can_only_qualify_observed_propagation():
    record = make_record(
        factual_status="unverified", market_propagation="observed",
        propagation_impact={"price_response": "observed", "evidence_refs": ["ev-1"]},
    )
    qualification = qualify_record(record)

    assert qualification["state"] == "degraded"
    assert qualification["permitted_use"] == "propagation_only"
    assert "content_unverified" in qualification["reasons"]
    assert "market_propagation_observed_despite_unverified_content" in qualification["reasons"]
    assert record["external_fact"] is False


def test_conflict_expiry_and_replay_are_stable():
    conflicted = qualify_record(make_record(), source_conflict_refs=("ev-1", "ev-2"))
    expired = qualify_record(make_record(expires_at="2026-09-20T07:30:00Z"), as_of=AS_OF)
    first = qualification_for_records([make_record()], as_of=AS_OF)[0]
    second = qualification_for_records([make_record()], as_of=AS_OF)[0]

    assert conflicted["state"] == "conflicted"
    assert "source_conflict" in conflicted["reasons"]
    assert expired["state"] == "expired"
    assert expired["permitted_use"] == "none"
    assert first == second


def test_acquisition_and_memoryhub_receipts_carry_qualification():
    boundary = AcquisitionBoundary("attempt-1")
    observation, _ = boundary.observe(
        "web.search", {}, {"backend": "web", "results": [{
            "url": "https://source.test/item", "title": "Market item",
            "excerpt_text": "Observed market event.", "fact_as_of": "2026-09-20T07:00:00Z",
            "factual_status": "verified",
        }]}, True,
    )
    item = observation["evidence_items"][0]
    assert item["evidence_qualification"]["contract"] == VERSION

    memory = InMemoryMemoryAdapter()
    registered = MemoryEvidenceRegistrar(memory, clock=lambda: "2026-09-20T07:05:00Z").register_web_snapshot(
        memory_space_id="research", source_event_id="event-1", url=item["url"],
        title=item["title"], body=item["excerpt_text"], occurred_at="2026-09-20T07:00:00Z",
        evidence_spec=copy.deepcopy(item["evidence_spec"]),
    )
    episode = memory._episodes[0]
    assert episode["metadata"]["evidence_qualification"]["contract"] == VERSION
    assert registered.context["evidence_qualification"]["input_record_refs"]
