from __future__ import annotations

import json
from pathlib import Path

from ai_trading_companion.local_research import freeze_evidence_bundle
from ai_trading_companion.provider_awg_smoke import _broker, missing_luna_terra_endpoints, normalize_usage, write_smoke_report
from ai_trading_companion.provider_broker import ChatCompletionsTransport, ProviderBroker
from ai_trading_companion.provider_routes import normalize_provider


def test_smoke_broker_is_the_formal_runtime_broker(tmp_path: Path):
    provider = normalize_provider({
        "endpoints": [{"id": "p", "base_url": "https://provider.test/v1"}],
        "routes": [{
            "id": "terra", "endpoint": "p", "model": "gpt-5.6-terra",
            "model_family": "openai", "cost": {"tier": 0, "mode": "relative"},
            "stages": ["research"], "capabilities": ["race"],
        }],
    }, warn_legacy=False)

    broker = _broker(provider, {}, tmp_path)

    assert isinstance(broker, ProviderBroker)
    assert isinstance(broker.transport, ChatCompletionsTransport)


def test_frozen_bundle_stays_in_memory_and_report_omits_bodies_and_secrets(tmp_path: Path):
    secret = "sk-test-secret-value"
    evidence = {"search_results": [{"url": "https://example.test"}], "read_results": [{"text": "evidence body"}]}
    bundle_bytes, bundle_hash = freeze_evidence_bundle(evidence)
    report_path = tmp_path / "smoke-report.json"

    write_smoke_report(report_path, {
        "status": "passed", "evidence": {"bundle_sha256": bundle_hash, "bundle_bytes": len(bundle_bytes)},
        "body": "evidence body", "diagnostic": f"upstream said {secret}", "api_key": secret,
    }, forbidden_values=[secret])

    text = report_path.read_text(encoding="utf-8")
    assert len(bundle_hash) == 64
    assert "evidence body" not in text
    assert secret not in text
    assert not (tmp_path / "evidence-bundle.json").exists()


def test_usage_prefers_nonzero_native_family_counts():
    usage = normalize_usage({
        "input_tokens": 278, "prompt_tokens": 278, "output_tokens": 0,
        "completion_tokens": 57, "prompt_tokens_details": {"cached_tokens": 40},
    })
    assert usage == {"input_tokens": 278, "output_tokens": 57, "cached_input_tokens": 40}


def test_script_is_only_a_formal_runtime_cli_wrapper():
    root = Path(__file__).resolve().parents[2]
    text = (root / "scripts" / "provider_awg_smoke.py").read_text(encoding="utf-8")
    assert "from ai_trading_companion.provider_awg_smoke import" in text
    assert "urlopen" not in text
    assert "ProviderBroker(" not in text


def test_upgrade_proof_uses_only_real_missing_luna_terra_inventory():
    provider = {"routes": [
        {"id": "has-luna-terra", "endpoint": "a", "model": "gpt-5.6-terra", "enabled": True, "cost": {"tier": 0, "weight": .01}},
        {"id": "missing-luna-terra", "endpoint": "b", "model": "gpt-5.6-terra", "enabled": True, "cost": {"tier": 100, "weight": .02}},
        {"id": "invented-terra", "endpoint": "c", "model": "gpt-5.6-terra", "enabled": True, "cost": {"tier": 0, "weight": .01}},
    ]}
    probes = [
        {"endpoint_id": "a", "probe_scope": "health_gate", "probe_round": 2, "status": "available", "models": ["gpt-5.6-luna", "gpt-5.6-terra"]},
        {"endpoint_id": "b", "probe_scope": "health_gate", "probe_round": 2, "status": "available", "models": ["gpt-5.6-terra"]},
        {"endpoint_id": "c", "probe_scope": "health_gate", "probe_round": 2, "status": "available", "models": ["gpt-5.6-sol"]},
    ]

    assert missing_luna_terra_endpoints(provider, probes) == ["b"]
