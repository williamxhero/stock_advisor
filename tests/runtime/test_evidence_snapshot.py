from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.evidence_snapshot import (
    VERSION,
    build_snapshot,
    descriptor,
    validate_snapshot,
)
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


AS_OF = "2026-09-21T01:45:00Z"


def _evidence(*, marker: str = "first") -> dict:
    return {
        "schema_version": 3,
        "as_of": AS_OF,
        "spoken_summary": f"公开证据 {marker}",
        "sources": [{"evidence_ref": f"ev-{marker}", "excerpt": "可核验公开事实", "analysis": "事实"}],
        "coverage": [],
        "critical_gaps": [],
        "conflicts": [],
        "high_impact_events": [],
    }


def test_snapshot_contract_is_deterministic_and_tamper_evident() -> None:
    first = build_snapshot(
        cycle_id="cycle-1", as_of=AS_OF, evidence=_evidence(),
        source_watermarks={"market": {"watermark": "close-20260921"}},
    )
    second = build_snapshot(
        cycle_id="cycle-1", as_of=AS_OF, evidence=json.loads(json.dumps(_evidence())),
        source_watermarks={"market": {"watermark": "close-20260921"}},
    )

    assert first == second
    assert first["contract"] == VERSION
    assert first["snapshot_id"]
    assert first["content_hash"]
    Draft202012Validator(
        json.loads((Path(__file__).parents[2] / "resources/contracts/evidence-snapshot-spec-v1.schema.json").read_text(encoding="utf-8"))
    ).validate(first)
    validate_snapshot(first)

    tampered = copy.deepcopy(first)
    tampered["baseline"]["spoken_summary"] = "被篡改"
    with pytest.raises(ValueError, match="content hash"):
        validate_snapshot(tampered)


def test_persistence_is_append_only_and_replay_is_stable() -> None:
    with TemporaryDirectory() as temporary:
        store = CompanionStore(Path(temporary) / "companion.sqlite3")
        cycle = CompanionEngine(store).start_cycle(
            "daily.execution.0945", "2026-09-21T09:45:00+08:00", AS_OF,
        )
        first = store.create_evidence_snapshot(
            cycle["cycle_id"], _evidence(), as_of=AS_OF,
            source_watermarks={"market": "close-1"},
        )
        replay = store.create_evidence_snapshot(
            cycle["cycle_id"], _evidence(), as_of=AS_OF,
            source_watermarks={"market": "close-1"},
        )
        later = store.create_evidence_snapshot(
            cycle["cycle_id"], _evidence(marker="later"), as_of=AS_OF,
            source_watermarks={"market": "close-2"},
        )

        assert replay == first
        assert later["version"] == 2
        assert later["parent_snapshot_id"] == first["snapshot_id"]
        assert later["snapshot_id"] != first["snapshot_id"]
        assert store.evidence_snapshot(first["snapshot_id"])["baseline"] == _evidence()
        assert [row["version"] for row in store.evidence_snapshots(cycle["cycle_id"])] == [1, 2]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            with store.connection() as connection:
                connection.execute(
                    "UPDATE evidence_snapshot SET baseline_json=? WHERE snapshot_id=?",
                    ("{}", first["snapshot_id"]),
                )


def test_m0_and_m1_packets_share_the_same_public_evidence_baseline() -> None:
    with TemporaryDirectory() as temporary:
        store = CompanionStore(Path(temporary) / "companion.sqlite3")
        cycle = CompanionEngine(store).start_cycle(
            "daily.execution.0945", "2026-09-21T09:45:00+08:00", AS_OF,
        )
        evidence = _evidence()
        snapshot = store.create_evidence_snapshot(
            cycle["cycle_id"], evidence, as_of=AS_OF,
            source_watermarks={"market": "close-1"},
        )
        store.append_artifact(
            cycle["cycle_id"], "evidence", "runtime", json.dumps(evidence, ensure_ascii=False), AS_OF,
            {"public_only": True, "evidence_snapshot_id": snapshot["snapshot_id"]},
        )
        builder = RuntimePacketBuilder(
            Path(__file__).parents[2] / "resources", store,
            memory=InMemoryMemoryAdapter(),
        )

        m0 = builder.build(cycle, "m0_compose", evidence=evidence)
        m1_research = builder.build(cycle, "m1_research", evidence=evidence)
        m1 = builder.build(cycle, "m1_judgment", evidence=evidence)

        assert m0["evidence_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
        assert m1_research["evidence_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
        assert m1["evidence_snapshot"]["snapshot_id"] == snapshot["snapshot_id"]
        assert m1["frozen_public_evidence"]["snapshot_id"] == snapshot["snapshot_id"]
        assert m0["evidence_snapshot"]["content_hash"] == m1["evidence_snapshot"]["content_hash"]
        assert "h0_text" not in json.dumps(m1, ensure_ascii=False)


def test_installation_verification_requires_snapshot_runtime_and_schema() -> None:
    script = (Path(__file__).parents[2] / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "resources\\contracts\\evidence-snapshot-spec-v1.schema.json" in script
    assert "runtime\\ai_trading_companion\\evidence_snapshot.py" in script
