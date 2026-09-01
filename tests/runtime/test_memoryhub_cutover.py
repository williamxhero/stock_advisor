from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from ai_trading_companion.memory_port import MemoryUnavailable
from ai_trading_companion.packet_builder import RuntimePacketBuilder
from ai_trading_companion.store import CompanionStore


ROOT = Path(__file__).resolve().parents[2]


def test_production_source_has_no_local_long_term_memory_bypass() -> None:
    source = ROOT / "src" / "runtime" / "ai_trading_companion"
    forbidden = ("SqliteMemoryRetriever", "MemoryLibrary", ".relevant_propositions(")
    findings = {
        str(path.relative_to(ROOT)): token
        for path in source.glob("*.py") if path.name not in {"memoryhub_migration.py"}
        for token in forbidden if token in path.read_text(encoding="utf-8")
    }
    assert findings == {}
    assert not (source / "memory.py").exists()


def test_packet_builder_refuses_to_read_local_memory_when_memoryhub_is_missing() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        store = CompanionStore(root / "companion.sqlite3")
        cycle = store.create_cycle("daily.execution.1030", "2026-09-01T10:30:00+08:00", "2026-09-01T02:30:00Z")
        with pytest.raises(MemoryUnavailable, match="MemoryHub"):
            RuntimePacketBuilder(ROOT / "resources", root, store).build(cycle, "m0_research")
