from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ai_trading_companion.evidence_snapshot import build, validate
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore


def _evidence(text: str) -> dict[str, object]:
    return {
        "sources": [{
            "url": "https://example.test/fact",
            "title": "事实",
            "excerpt": text,
            "fact_as_of": "2026-09-20T01:00:00Z",
        }],
        "as_of": "2026-09-20T01:00:00Z",
    }


def test_snapshot_identity_and_content_hash_are_deterministic() -> None:
    first = build(
        "cycle-1", "2026-09-20T01:00:00Z", [
            {"evidence_id": "e-2", "content_sha256": "b"},
            {"evidence_id": "e-1", "content_sha256": "a"},
        ], {"market": {"known_at": "2026-09-20T01:05:00Z", "content_hash": "watermark"}},
    )
    replay = build(
        "cycle-1", "2026-09-20T01:00:00Z", [
            {"evidence_id": "e-1", "content_sha256": "a"},
            {"evidence_id": "e-2", "content_sha256": "b"},
        ], {"market": {"content_hash": "watermark", "known_at": "2026-09-20T01:05:00Z"}},
    )

    assert replay == first
    validate(first)
    changed = build(
        "cycle-1", "2026-09-20T01:00:00Z", [{"evidence_id": "e-3", "content_sha256": "c"}],
        first["source_watermarks"], version=2, previous_snapshot_id=first["snapshot_id"],
    )
    assert changed["snapshot_id"] != first["snapshot_id"]
    assert changed["content_hash"] != first["content_hash"]


def test_runtime_persists_versions_without_overwriting_shared_baseline(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.sqlite3")
    cycle = store.ensure_daily_conversation("2026-09-20")
    store.record_evidence(cycle, "m0_research", _evidence("第一版"))
    baseline = store.shared_evidence_snapshot(cycle["cycle_id"])
    assert baseline is not None

    store.record_evidence(cycle, "m0_research", _evidence("第二版"))
    snapshots = store.evidence_snapshots(cycle["cycle_id"])
    assert len(snapshots) == 2
    assert snapshots[0]["snapshot_id"] == baseline["snapshot_id"]
    assert store.evidence_snapshot(baseline["snapshot_id"]) == baseline
    assert snapshots[1]["previous_snapshot_id"] == baseline["snapshot_id"]
    assert store.shared_evidence_snapshot(cycle["cycle_id"])["snapshot_id"] == baseline["snapshot_id"]


def test_snapshot_installation_schema_is_created_and_contract_is_present(tmp_path: Path) -> None:
    contract_path = Path(__file__).parents[2] / "resources/contracts/evidence-snapshot-v1.schema.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["title"] == "EvidenceSnapshotSpec/v1"
    store = CompanionStore(tmp_path / "installed.sqlite3")
    store.initialize()
    with sqlite3.connect(store.database) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('evidence_snapshot','evidence_snapshot_use')"
            )
        }
    assert tables == {"evidence_snapshot", "evidence_snapshot_use"}


def test_m0_and_m1_packets_reference_the_same_frozen_baseline(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "packet.sqlite3")
    cycle = CompanionEngine(store).start_cycle(
        "daily.execution.0945", "2026-09-20T09:45:00+08:00", "2026-09-20T01:00:00Z",
    )
    evidence = _evidence("共享基线")
    store.record_evidence(cycle, "m0_research", evidence)
    builder = RuntimePacketBuilder(
        Path(__file__).parents[2] / "resources", store, memory=InMemoryMemoryAdapter(),
    )
    m0 = builder.build(cycle, "m0_compose", evidence=evidence)
    m1 = builder.build(cycle, "m1_judgment", evidence=evidence)
    assert m0["evidence_snapshot"]["snapshot_id"] == m1["evidence_snapshot"]["snapshot_id"]
    assert m0["evidence_snapshot"]["content_hash"] == m1["evidence_snapshot"]["content_hash"]
