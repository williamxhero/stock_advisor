from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ai_trading_companion.decision_cycle import assert_m1_blind
from ai_trading_companion.__main__ import _m1_should_retry
from ai_trading_companion.broker_client import BrokerError
from ai_trading_companion.judgment_publication import JudgmentUnavailable
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def _cycle(store: CompanionStore) -> dict:
    return CompanionEngine(store).start_cycle(
        "daily.execution.0945",
        "2026-09-20T09:45:00+08:00",
        "2026-09-20T01:45:00Z",
        schedule_id="daily.execution.0945",
        schedule_revision=7,
        schedule_snapshot={"version": 7, "trigger": {"lead_minutes": 0}},
    )


def test_contract_freezes_identity_schedule_and_stage_provenance() -> None:
    with TemporaryDirectory() as temporary:
        store = CompanionStore(Path(temporary) / "companion.sqlite3")
        cycle = _cycle(store)
        contract = store.decision_cycle_contract(cycle["cycle_id"])

        assert contract["contract"] == "companion-decision-cycle/v1"
        assert contract["contract_version"] == 1
        assert contract["cycle_id"] == cycle["cycle_id"]
        assert contract["task_key"] == "daily.execution.0945"
        assert contract["as_of"] == "2026-09-20T01:45:00Z"
        assert contract["schedule"]["revision"] == 7
        assert {item["stage"] for item in contract["stages"]} == {
            "m0", "h0", "m1", "m2", "result", "reflection", "memory",
        }
        assert all(item["provenance"]["contract"] == "companion-decision-cycle-provenance/v1" for item in contract["stages"])


def test_m0_retry_and_m1_rollback_are_append_audited_without_h0_visibility() -> None:
    with TemporaryDirectory() as temporary:
        store = CompanionStore(Path(temporary) / "companion.sqlite3")
        engine = CompanionEngine(store)
        cycle = _cycle(store)
        engine.research_started(cycle["cycle_id"])
        store.fail_stage(cycle["cycle_id"], "m0", "provider timeout", retryable=True)
        assert store.stage_status(cycle["cycle_id"], "m0")["state"] == "retry_wait"
        assert len(store.stage_events(cycle["cycle_id"], "m0")) == 2

        store.freeze_private_context(cycle["cycle_id"])
        m1 = store.start_stage(cycle["cycle_id"], "m1", input_sha256="public-packet", provenance={"source": "test"})
        store.fail_stage(cycle["cycle_id"], "m1", "temporary broker failure", retryable=True)
        retry = store.start_stage(cycle["cycle_id"], "m1", input_sha256="public-packet", provenance={"source": "retry"})
        assert retry["attempt"] == m1["attempt"] + 1
        rolled = store.rollback_cycle_stage(cycle["cycle_id"], "m1", reason="discard unpublished attempt")
        assert rolled["stage"]["state"] == "rolled_back"
        assert len(store.stage_history(cycle["cycle_id"], "m1")) == 3

        assert_m1_blind({"business_context": {"private_context_before_h0": {"positions": []}}}, human_texts=["H0原文"])
        with pytest.raises(ValueError, match="H0 or H0-derived"):
            assert_m1_blind({"cognition_result": {"signal": "derived"}})
        with pytest.raises(ValueError, match="H0 or H0-derived"):
            assert_m1_blind({"validation_context": {"h0_propositions": ["用户方向判断"]}})
        with pytest.raises(ValueError, match="H0 or H0-derived"):
            assert_m1_blind({"validation_context": {"h0_actions": ["用户动作结果"]}})
        with pytest.raises(ValueError, match="current-cycle human"):
            assert_m1_blind({"note": "H0原文"}, human_texts=["H0原文"])


def test_judgment_unavailable_retries_only_transient_broker_causes() -> None:
    transient = JudgmentUnavailable("temporary", BrokerError("timeout", category="broker_timeout"))
    permanent = JudgmentUnavailable("invalid core")
    assert _m1_should_retry(transient, attempt_number=1, remaining_seconds=60) is True
    assert _m1_should_retry(permanent, attempt_number=1, remaining_seconds=60) is False


def test_published_judgment_cannot_be_rolled_back_and_revisions_append() -> None:
    with TemporaryDirectory() as temporary:
        store = CompanionStore(Path(temporary) / "companion.sqlite3")
        cycle = _cycle(store)
        first = store.append_artifact(cycle["cycle_id"], "m1", "model", "原始判断", cycle["as_of"], {"published": True})
        second = store.append_artifact(
            cycle["cycle_id"], "judgment_revision", "model", "追加判断：新证据改变了结论",
            "2026-09-20T02:00:00Z", {"revises_artifact_id": first["artifact_id"]},
        )

        assert first["revision"] == 1
        assert second["revision"] == 1
        assert store.latest_artifact(cycle["cycle_id"], "m1")["body_markdown"] == "原始判断"
        metadata = json.loads(store.latest_artifact(cycle["cycle_id"], "judgment_revision")["metadata_json"])
        assert metadata["revises_artifact_id"] == first["artifact_id"]
        with pytest.raises(ValueError, match="published M1"):
            store.rollback_cycle_stage(cycle["cycle_id"], "m1", reason="too late")
