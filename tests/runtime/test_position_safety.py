from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.cognition import verify_cognition_result
from ai_trading_companion.position_safety import (
    CONTRACT,
    RESULT_CONTRACT,
    assert_no_execution,
    assert_operation,
    build_input,
    build_output,
    frozen_replay,
    install_qualification,
    sizing_problems,
    validate_input,
    validate_output,
)
from ai_trading_companion.m0_observation import sha256


AS_OF = "2026-10-05T06:30:00Z"
SESSION = "2026-10-05"


def snapshot(**overrides):
    value = {
        "positions": [
            {"code": "603179", "shares": 100, "last_price": 10.0, "updated_at": AS_OF},
        ],
        "total_assets": 100_000.0,
        "holdings_as_of": AS_OF,
        "assets_as_of": AS_OF,
        "risk_state": {
            "theme_by_code": {"603179": "auto"}, "peak_assets": 100_000.0,
            "synchronized": True, "review_completed": False,
        },
    }
    value.update(overrides)
    return value


def contract(**kwargs):
    return build_input(
        snapshot(**kwargs), stage="m1_judgment", as_of=AS_OF,
        source_ref="runtime-private-context", source="runtime", verified=True,
        latest_session=SESSION,
    )


def sizing(**overrides):
    value = {"code": "603179", "target_shares": 1000, "stop_price": 9.0, "leverage": False}
    value.update(overrides)
    return value


def test_input_has_exact_versioned_runtime_provenance_and_read_only_permissions():
    value = contract()
    assert value["contract"] == CONTRACT
    assert value["provenance"] == {
        "source": "runtime", "kind": "runtime_database", "verified": True,
        "source_ref": "runtime-private-context", "snapshot_sha256": sha256(value["truth"]),
    }
    assert value["permissions"] == {"write_permissions": []}
    assert value["quantresearch"] == {"access": "read_only", "write_permissions": []}
    assert validate_input(value) == value


@pytest.mark.parametrize("source", ["filesystem", "llm", "quantresearch", "user_text"])
def test_untrusted_position_sources_fail_closed(source):
    with pytest.raises(ValueError, match="provenance|untrusted"):
        build_input(snapshot(), stage="m1_judgment", as_of=AS_OF,
                    source_ref="untrusted", source=source, verified=True, latest_session=SESSION)


def test_unverified_broker_or_user_claim_is_rejected():
    with pytest.raises(ValueError, match="provenance|untrusted"):
        build_input(snapshot(), stage="m1_judgment", as_of=AS_OF,
                    source_ref="broker-receipt", source="broker", verified=False, latest_session=SESSION)


def test_truth_tampering_and_duplicate_position_facts_are_rejected():
    value = contract()
    broken = copy.deepcopy(value)
    broken["truth"]["positions"][0]["shares"] = 999
    with pytest.raises(ValueError, match="provenance"):
        validate_input(broken)

    with pytest.raises(ValueError, match="conflicting"):
        contract(positions=[
            {"code": "603179", "shares": 100, "last_price": 10.0, "updated_at": AS_OF},
            {"code": "603179", "shares": 200, "last_price": 10.0, "updated_at": AS_OF},
        ])


def test_llm_position_writes_and_orders_fail_closed():
    for proposal in (
        {"write_positions": {"603179": 500}},
        {"submit_trade": {"code": "603179", "shares": 100}},
        {"operation": "place_order"},
        {"encoded": '{"order.place": {"shares": 100}}'},
    ):
        with pytest.raises(ValueError, match="write/order"):
            assert_no_execution(proposal)
    with pytest.raises(ValueError, match="only Runtime"):
        assert_operation("llm", "record_verified_fact")
    with pytest.raises(ValueError, match="only Runtime"):
        assert_operation("quantresearch", "record_verified_fact")
    with pytest.raises(ValueError, match="user-decided"):
        assert_operation("runtime", "place_order")


def test_cognition_verifier_rejects_order_action_without_executing_anything():
    result = {"answer": None, "propositions": [], "actions": [{
        "action_type": "order.place", "order": {"code": "603179", "shares": 100},
        "source_span": {"message_id": "m1", "start": 0, "end": 4, "quote": "买入"},
    }]}
    verification = verify_cognition_result([{"message_id": "m1", "body_text": "买入",}], result)
    assert not verification["passed"]
    assert any("position_safety" in problem for problem in verification["problems"])


def test_stale_asset_sizing_is_refused_but_directional_advice_is_allowed():
    stale = contract(assets_as_of="2026-09-30T06:30:00Z")
    sizing_receipt = build_output(stale, {"text": "条件化讨论"}, sizing=sizing())
    assert sizing_receipt["contract"] == RESULT_CONTRACT
    assert sizing_receipt["state"] == "refused"
    assert "stale_or_unknown_assets_as_of" in sizing_receipt["problems"]

    directional = build_output(stale, {"text": "可以继续观察，不给出精确股数。"})
    assert directional["state"] == "directional_only"
    assert directional["problems"] == []


def test_fresh_sizing_passes_only_with_canonical_limits_and_read_only_receipt():
    receipt = build_output(contract(), {"text": "真实交易由用户决定。"}, sizing=sizing())
    assert receipt["state"] == "qualified"
    assert receipt["permissions"] == {"write_permissions": []}
    assert receipt["quantresearch"] == {"access": "read_only", "write_permissions": []}
    assert validate_output(receipt) == receipt

    too_large = build_output(contract(), {"text": "真实交易由用户决定。"}, sizing=sizing(target_shares=3000))
    assert too_large["state"] == "refused"
    assert "single_stock_limit" in too_large["problems"]


def test_drawdown_and_leverage_boundaries_refuse_added_risk():
    value = contract(risk_state={
        "theme_by_code": {"603179": "auto"}, "peak_assets": 100_000.0,
        "synchronized": True, "review_completed": False,
    }, total_assets=84_000.0)
    refused = build_output(value, {"current_action": "allow_add_risk"}, sizing=sizing(target_shares=200))
    assert refused["state"] == "refused"
    assert "drawdown_requires_review" in refused["problems"]

    leveraged = build_output(contract(), {"text": "用户决定交易。"}, sizing=sizing(leverage=True))
    assert "leverage_not_approved" in leveraged["problems"]


def test_m0_exact_sizing_is_never_qualified():
    value = build_input(snapshot(), stage="m0_compose", as_of=AS_OF,
                        source_ref="m0", latest_session=SESSION)
    result = build_output(value, {"text": "配置 10%"}, sizing=sizing())
    assert result["state"] == "refused"
    assert "m0_cannot_advise" in result["problems"]


def test_frozen_replay_is_deterministic_and_does_not_mutate_inputs():
    value, output = contract(), {"text": "只讨论方向，不给精确股数。"}
    original = (copy.deepcopy(value), copy.deepcopy(output))
    first = frozen_replay(value, output)
    second = frozen_replay(copy.deepcopy(value), copy.deepcopy(output))
    assert first == second
    assert first["qualification"] == {"valid": True, "state": "directional_only", "read_only": True}
    first["source_input"]["truth"]["positions"][0]["shares"] = 0
    assert (value, output) == original


def test_install_qualification_is_deterministic_and_schema_accepts_input_result_replay():
    assert install_qualification() == install_qualification()
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/position-safety-spec-v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    value = contract()
    result = build_output(value, {"text": "只讨论方向。"})
    replay = frozen_replay(value, {"text": "只讨论方向。"})
    assert not list(validator.iter_errors(value))
    assert not list(validator.iter_errors(result))
    assert not list(validator.iter_errors(replay))
