from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion.evidence_snapshot import (
    VERSION,
    build_snapshot,
    replay_snapshot,
    shared_baseline,
)
from ai_trading_companion.store import CompanionStore


AS_OF = "2026-09-20T01:00:00Z"


def _evidence(excerpt: str = "公开事实") -> dict[str, object]:
    return {
        "schema_version": 3,
        "as_of": AS_OF,
        "spoken_summary": "verified",
        "sources": [{
            "evidence_ref": "ev-1",
            "url": "https://example.test/fact",
            "excerpt": excerpt,
            "fact_as_of": AS_OF,
        }],
        "coverage": [],
        "critical_gaps": [],
        "conflicts": [],
        "high_impact_events": [],
    }


def _cycle(store: CompanionStore) -> dict[str, object]:
    return store.ensure_daily_conversation("2026-09-20")


def test_snapshot_identity_hash_and_replay_are_deterministic() -> None:
    first = build_snapshot(
        cycle_id="cycle-1", as_of=AS_OF,
        source_watermarks={"market": "2026-09-20T00:59:00Z", "web": 4},
        evidence=_evidence(),
    )
    second = build_snapshot(
        cycle_id="cycle-1", as_of="2026-09-20T09:00:00+08:00",
        source_watermarks={"web": 4, "market": "2026-09-20T00:59:00Z"},
        evidence=_evidence(),
    )

    assert first == second
    assert first["contract"] == VERSION
    assert first["snapshot_id"]
    assert len(first["content_hash"]) == 64
    replay = replay_snapshot(first)
    replay["evidence"]["sources"][0]["excerpt"] = "mutated replay"
    assert first["evidence"]["sources"][0]["excerpt"] == "公开事实"


def test_runtime_persists_immutable_versions_and_shared_m0_m1_reference(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = _cycle(store)
    first = build_snapshot(
        cycle_id=cycle["cycle_id"], as_of=AS_OF,
        source_watermarks={"market": AS_OF}, evidence=_evidence(),
    )
    second = build_snapshot(
        cycle_id=cycle["cycle_id"], as_of=AS_OF,
        source_watermarks={"market": AS_OF}, evidence=_evidence("later fact"),
        parent_snapshot_id=first["snapshot_id"],
    )
    store.save_evidence_snapshot(first, stage="m0_research", role="m0_baseline")
    store.save_evidence_snapshot(second, stage="m0_research", role="m0_baseline")
    store.reference_evidence_snapshot(
        cycle["cycle_id"], second["snapshot_id"], stage="m1_research", role="shared_baseline",
    )

    assert [row["snapshot_id"] for row in store.evidence_snapshots(cycle["cycle_id"])] == [
        first["snapshot_id"], second["snapshot_id"],
    ]
    assert store.shared_evidence_snapshot(cycle["cycle_id"])["snapshot_id"] == second["snapshot_id"]

    tampered = copy.deepcopy(first)
    tampered["evidence"]["spoken_summary"] = "overwrite"
    tampered["content_hash"] = first["content_hash"]
    with pytest.raises(ValueError, match="content hash mismatch"):
        store.save_evidence_snapshot(tampered, stage="m0_research")


def test_record_evidence_freezes_a_runtime_snapshot_and_schema_is_installed(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = _cycle(store)
    evidence = _evidence()
    store.record_evidence(cycle, "m0_research", evidence, observations=[{
        "backend": "market", "operation": "breadth",
        "acquired_at": "2026-09-20T00:59:30Z",
    }])

    snapshot = store.evidence_snapshot_for_stage(
        cycle["cycle_id"], "m0_research", role="m0_baseline",
    )
    assert snapshot is not None
    assert snapshot["evidence"] == evidence
    assert snapshot["source_watermarks"] == {"market": "2026-09-20T00:59:30Z"}

    schema_path = Path(__file__).parents[2] / "resources/contracts/evidence-snapshot-spec-v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(snapshot)) == []


def test_shared_baseline_rejects_divergent_m1_evidence() -> None:
    first = build_snapshot(
        cycle_id="cycle-1", as_of=AS_OF, source_watermarks={}, evidence=_evidence(),
    )
    second = build_snapshot(
        cycle_id="cycle-1", as_of=AS_OF, source_watermarks={}, evidence=_evidence("new"),
    )
    with pytest.raises(ValueError, match="share one evidence snapshot"):
        shared_baseline(first, second)
