from __future__ import annotations

from ai_trading_companion.memory_health import MemoryCapabilityPolicy


def healthy() -> dict[str, object]:
    return {
        "ledger": {"state": "ready"}, "index": {"state": "ready"},
        "derivation": {"state": "ready"},
        "sources": {"markethub": {"state": "ready"}, "8815": {"state": "ready"}},
    }


def test_ledger_failure_makes_app_unavailable_without_fallback() -> None:
    value = healthy(); value["ledger"] = {"state": "unavailable"}
    decision = MemoryCapabilityPolicy.evaluate(value)
    assert decision.app_available is False
    assert decision.allow_local_memory_fallback is False


def test_index_derivation_and_source_failures_degrade_only_owned_capabilities() -> None:
    index = healthy(); index["index"] = {"state": "unavailable"}
    derivation = healthy(); derivation["derivation"] = {"state": "unavailable"}
    source = healthy(); source["sources"] = {"markethub": {"state": "ready"}, "8815": {"state": "unavailable"}}

    assert MemoryCapabilityPolicy.evaluate(index).memory_tasks_available is False
    assert MemoryCapabilityPolicy.evaluate(index).history_readable is True
    assert MemoryCapabilityPolicy.evaluate(derivation).memory_tasks_available is True
    assert MemoryCapabilityPolicy.evaluate(derivation).derivation_available is False
    assert MemoryCapabilityPolicy.evaluate(source).blocked_sources == ("8815",)
