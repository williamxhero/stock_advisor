from __future__ import annotations

import ast
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_trading_companion import memory_write
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.memory_port import InMemoryMemoryAdapter, MemoryUnavailable
from ai_trading_companion.memory_write import (
    FORBIDDEN_OPERATIONS,
    PROJECTIONS,
    WRITE_POLICY,
    MemoryWriter,
    frozen_replay,
    install_qualification,
    plan,
    validate_plan,
)
from ai_trading_companion.store import CompanionStore
from trading_memory_hub import MemoryHub

RUNTIME = Path(memory_write.__file__).parent


def episode(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "memory_space_id": "partner-main", "source_system": "stock-advisor",
        "source_event_id": "write-1", "content_hash": "auto",
        "episode_type": "user_message", "body": "我更重视回撤控制。",
        "occurred_at": "2026-09-20T01:00:00Z", "known_at": "2026-09-20T01:01:00Z",
        "submitted_at": "2026-09-20T01:02:00Z", "authority": "user_private_fact",
        "protocol_version": "memoryhub/v1",
    }
    value.update(overrides)
    return value


class CountingMemory(InMemoryMemoryAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.append_calls = 0

    def append(self, value: dict[str, object]) -> dict[str, object]:
        self.append_calls += 1
        return super().append(value)


def test_policy_matrix_accepts_each_formal_kind_and_rejects_unknown_or_projection() -> None:
    writer = MemoryWriter(InMemoryMemoryAdapter())
    samples = {
        "user_message": (episode(), "message"),
        "ai_message": (episode(episode_type="ai_message", authority="published_ai_message"), "judgment"),
        "user_fact": (episode(episode_type="personal_fact"), "user_fact"),
        "evidence": (episode(episode_type="external_evidence", authority="mutable_source_snapshot"), "evidence"),
        "migrated": (episode(episode_type="legacy_workspace_document", authority="migrated_legacy_record"), "operational"),
    }
    assert set(samples) | {"correction"} == set(WRITE_POLICY)
    for kind, (value, semantic_type) in samples.items():
        prepared = writer.prepare(kind, value, semantic_type=semantic_type)
        assert prepared["metadata"]["memory_type"]["semantic_type"] == semantic_type
    with pytest.raises(ValueError, match="unsupported memory write kind"):
        writer.prepare("scratchpad", episode(), semantic_type="message")
    for projection in PROJECTIONS:
        with pytest.raises(ValueError, match="rebuildable projection"):
            writer.prepare(projection, episode(), semantic_type="message")


@pytest.mark.parametrize("override,message", [
    ({"authority": "published_ai_message"}, "authority"),
    ({"episode_type": "ai_message"}, "episode type"),
])
def test_policy_rejects_authority_and_type_drift(override: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MemoryWriter(InMemoryMemoryAdapter()).prepare("user_message", episode(**override), semantic_type="message")
    with pytest.raises(ValueError, match="semantic type"):
        MemoryWriter(InMemoryMemoryAdapter()).prepare("user_message", episode(), semantic_type="judgment")


def test_memoryhub_is_required_and_there_is_no_local_fallback() -> None:
    with pytest.raises(MemoryUnavailable, match="MemoryHub is required"):
        MemoryWriter(None)
    assert not [name for name in dir(MemoryWriter) if not name.startswith("_")
                and any(word in name for word in FORBIDDEN_OPERATIONS)]


def test_secrets_in_body_metadata_or_source_reference_stop_before_the_port() -> None:
    memory = CountingMemory()
    writer = MemoryWriter(memory)
    for field, value in (
        ("body", "密码 token=abcdefgh12345678 不要保存"),
        ("metadata", {"note": "Bearer abcdefghijklmnop1234567890"}),
        ("source_reference", {"url": "https://x.test/?secret=abcdefghijklmnop"}),
    ):
        with pytest.raises(ValueError, match="secret guard blocked"):
            writer.write("user_message", episode(**{field: value}), semantic_type="message")
    assert memory.append_calls == 0


def test_rewriting_a_source_event_is_rejected_and_replay_is_idempotent() -> None:
    memory = InMemoryMemoryAdapter()
    writer = MemoryWriter(memory)
    first = writer.write("user_message", episode(), semantic_type="message")
    assert writer.write("user_message", episode(), semantic_type="message") == first
    with pytest.raises(MemoryUnavailable, match="immutable conflict"):
        writer.write("user_message", episode(body="被改写的历史"), semantic_type="message")
    assert len(memory._episodes) == 1
    assert memory._episodes[0]["content_hash"].startswith("sha256:")


def test_correction_appends_against_the_original_and_never_rewrites_it(tmp_path: Path) -> None:
    hub = MemoryHub(tmp_path / "ledger.sqlite3")
    writer = MemoryWriter(hub)
    original = writer.write("user_message", episode(), semantic_type="message")
    correction = writer.correct(
        original.episode_id,
        episode(source_event_id="write-2", body="更正：我更重视风险调整后的收益。", authority="user_private_fact"),
    )
    exported = hub.export_space("partner-main")["episodes"]
    assert [item["body"] for item in exported][0] == "我更重视回撤控制。"
    assert exported[1]["corrects_episode_id"] == original.episode_id
    assert exported[1]["metadata"]["memory_type"]["semantic_type"] == "correction"
    assert correction.episode_id != original.episode_id
    with pytest.raises(ValueError, match="existing episode"):
        writer.write("correction", episode(source_event_id="write-3", episode_type="correction"), semantic_type="correction")


def test_runtime_message_path_uses_the_writer(tmp_path: Path) -> None:
    memory = CountingMemory()
    engine = CompanionEngine(CompanionStore(tmp_path / "companion.sqlite3"), memory=memory)
    cycle = engine.start_cycle("daily.execution.0945", "2026-09-20T01:00:00Z", "2026-09-20T01:00:00Z")
    engine.record_submitted_messages(cycle["cycle_id"], [{
        "message_id": "m-1", "body_text": "我更重视回撤控制。",
        "occurred_at": "2026-09-20T01:00:00Z", "known_at": "2026-09-20T01:01:00Z",
        "submitted_at": "2026-09-20T01:02:00Z", "staged_at": "2026-09-20T01:01:00Z",
        "provenance_json": "{}", "batch_id": "b-1", "phase": "chat",
    }])
    assert memory.append_calls == 1
    assert memory._episodes[0]["content_hash"].startswith("sha256:")
    with pytest.raises(ValueError, match="secret guard blocked"):
        engine.record_submitted_messages(cycle["cycle_id"], [{
            "message_id": "m-2", "body_text": "token=abcdefgh12345678",
            "known_at": "2026-09-20T01:01:00Z", "submitted_at": "2026-09-20T01:02:00Z",
            "staged_at": "2026-09-20T01:01:00Z", "provenance_json": "{}", "batch_id": "b-1", "phase": "chat",
        }])
    assert memory.append_calls == 1


def _memory_write_calls(path: Path) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in {"append", "append_batch"}:
            continue
        target = node.func.value
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name in {"memory", "_memory", "hub", "memory_hub"}:
            lines.append(node.lineno)
    return lines


def test_no_runtime_module_writes_memoryhub_outside_the_writer() -> None:
    allowed = {"memory_write.py", "memory_port.py"}
    offenders = {
        path.name: _memory_write_calls(path)
        for path in sorted(RUNTIME.glob("*.py"))
        if path.name not in allowed and _memory_write_calls(path)
    }
    assert offenders == {}


def test_plan_replay_schema_and_install_qualification_are_deterministic() -> None:
    root = Path(__file__).parents[2]
    schema = json.loads((root / "resources/contracts/memory-write-spec-v1.schema.json").read_text(encoding="utf-8"))
    value = plan("user_message", episode(), semantic_type="message")
    assert list(Draft202012Validator(schema).iter_errors(value)) == []
    validate_plan(value)
    assert frozen_replay(value) == frozen_replay(copy.deepcopy(value))
    for broken in ({"append_only": False}, {"idempotency_key": ["a", "b"]}, {"projection_kinds": []}):
        with pytest.raises(ValueError):
            validate_plan({**value, **broken})
    qualification = install_qualification()
    assert qualification["contract"] == "MemoryWriteInstallQualification/v1"
    assert qualification["qualified"] is True
    assert all(qualification["checks"].values()), qualification["checks"]
    assert set(qualification["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality",
        "judgment_outcome", "safety_reliability",
    }
