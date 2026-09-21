from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.agent_contract import (
    CONTRACT,
    attach_input,
    build_output,
)


PACKET = {
    "cycle_id": "cycle-1",
    "stage": "m0_research",
    "as_of": "2026-09-21T01:00:00Z",
    "evidence_snapshot": {
        "contract": "evidence-snapshot-spec/v1", "snapshot_id": "snap-1",
        "as_of": "2026-09-21T01:00:00Z", "content_hash": "abc",
    },
    "memories": [{"artifact_id": "memory-1", "as_of": "2026-09-20T08:00:00Z"}],
    "sha256": "packet-hash",
}


def test_contract_is_runtime_owned_and_replay_deterministic() -> None:
    first = attach_input(PACKET, capability="research:m0_research")
    second = attach_input(copy.deepcopy(PACKET), capability="research:m0_research")
    assert first == second
    assert first["agent_contract"]["contract"] == CONTRACT
    assert first["agent_contract"]["memory_references"][0]["reference"] == "memory-1"

    output = build_output(
        first["agent_contract"], capability="research:m0_research", status="succeeded",
        evidence=[{"reference": "https://example.test/evidence", "kind": "source"}],
        propositions=[{
            "id": "p1", "text": "事件已被来源确认", "kind": "conclusion",
            "evidence_refs": ["https://example.test/evidence"], "counterevidence_refs": [],
        }],
    )
    assert output["provenance"]["input_contract_sha256"]
    assert build_output(first["agent_contract"], capability="research:m0_research", status="succeeded") == build_output(
        second["agent_contract"], capability="research:m0_research", status="succeeded",
    )


def test_conclusion_requires_evidence_and_private_reasoning_is_rejected() -> None:
    contract = attach_input(PACKET, capability="research:m0_research")["agent_contract"]
    with pytest.raises(ValueError, match="must cite evidence"):
        build_output(contract, capability="research:m0_research", status="succeeded", propositions=[{
            "id": "p1", "text": "无依据结论", "kind": "conclusion",
            "evidence_refs": [], "counterevidence_refs": [],
        }])

    schema = json.loads((Path(__file__).parents[2] / "resources/contracts/agent-contract-spec-v1.schema.json").read_text(encoding="utf-8"))
    invalid = build_output(contract, capability="research:m0_research", status="succeeded")
    invalid["propositions"] = [{
        "id": "p1", "text": "无依据结论", "kind": "conclusion",
        "evidence_refs": [], "counterevidence_refs": [],
    }]
    assert list(Draft202012Validator(schema).iter_errors(invalid))
    with pytest.raises(ValueError, match="private reasoning"):
        build_output(contract, capability="research:m0_research", status="succeeded", unknowns=[{
            "description": "unknown", "chain_of_thought": "secret trace",
        }])


def test_installed_contract_schema_and_runtime_are_required() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/agent-contract-spec-v1.schema.json").read_text(encoding="utf-8"))
    input_schema = json.loads((root / "resources/contracts/agent-contract-input-v1.schema.json").read_text(encoding="utf-8"))
    input_contract = attach_input(PACKET, capability="research:m0_research")["agent_contract"]
    assert not list(Draft202012Validator(input_schema).iter_errors(input_contract))
    output = build_output(
        attach_input(PACKET, capability="research:m0_research")["agent_contract"],
        capability="research:m0_research", status="blocked", unknowns=[{"description": "未取得"}],
    )
    assert not list(Draft202012Validator(schema).iter_errors(output))
    script = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\agent-contract-spec-v1.schema.json" in script
    assert "resources\\contracts\\agent-contract-input-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\agent_contract.py" in script
