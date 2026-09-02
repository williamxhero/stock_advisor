from __future__ import annotations

import unittest

from ai_trading_companion.provider_broker import ProviderBroker, TransportResult
from ai_trading_companion.provider_module import GenerateRequest, ProviderModule
from ai_trading_companion.provider_routes import normalize_provider
from tempfile import TemporaryDirectory
from pathlib import Path
import json
from unittest.mock import MagicMock, patch


class _Transport:
    def probe(self, _endpoint, _timeout):
        return {"status": "available", "models": ["gpt-5.6-luna"]}

    def complete(self, _endpoint, _route, _payload, _timeout, _on_delta, _cancel):
        return TransportResult("complete language response", model="gpt-5.6-luna",
                               usage={"input_tokens": 4, "output_tokens": 3})


class ProviderModuleTests(unittest.TestCase):
    def _provider(self, *, family="openai"):
        model = "gpt-5.6-luna" if family == "openai" else "claude-sonnet-5"
        return normalize_provider({
            "enabled": True,
            "endpoints": [{"id": "provider", "base_url": "https://example.test/v1", "api_key": "secret",
                           "provider_kind": "single_family", "model_family": family}],
            "routes": [{"id": "model", "endpoint": "provider", "model": model,
                        "model_family": family, "intellect": "standard", "target_level": "L1", "stages": ["fast", "research", "judgment"], "effort": "low",
                        "capabilities": ["stream", "race"], "cost": {"tier": 0, "mode": "relative"}}],
        }, warn_legacy=False)

    def test_generate_exposes_complete_text_as_technical_success(self):
        module = ProviderModule(ProviderBroker(self._provider(), _Transport()))
        result = module.generate(GenerateRequest(prompt="hello", intellect="standard"))
        self.assertEqual("completed", result.status)
        self.assertEqual("complete language response", result.text)
        self.assertEqual("openai", result.model_family)
        self.assertEqual("standard", result.effective_intellect)

    def test_intellect_defaults_to_capability_level_without_exposing_model_or_family(self):
        module = ProviderModule(ProviderBroker(self._provider(), _Transport()))
        self.assertEqual("standard", module.generate(GenerateRequest(prompt="x", intellect="standard")).effective_intellect)
        # These calls have no matching route, but prove the request owns an
        # intellect slot, never a business workflow or provider family.
        self.assertEqual("smart", module.generate(GenerateRequest(prompt="x", intellect="smart")).requested_intellect)
        self.assertEqual("expert", module.generate(GenerateRequest(prompt="x", intellect="expert")).requested_intellect)

    def test_explicit_effort_is_used_by_both_protocols_and_slot_is_the_fallback(self):
        for family, field in (("openai", "reasoning"), ("anthropic", "reasoning_effort")):
            captured = {}
            class CapturingTransport(_Transport):
                def probe(self, _endpoint, _timeout):
                    return {"status": "available", "models": ["gpt-5.6-luna" if family == "openai" else "claude-sonnet-5"]}
                def complete(self, endpoint, route, payload, timeout, on_delta, cancel):
                    captured.update(payload)
                    return super().complete(endpoint, route, payload, timeout, on_delta, cancel)
            module = ProviderModule(ProviderBroker(self._provider(family=family), CapturingTransport()))
            module.generate(GenerateRequest(prompt="x", intellect="standard", effort="high"))
            self.assertEqual("high", captured[field]["effort"] if field == "reasoning" else captured[field])
            module = ProviderModule(ProviderBroker(self._provider(family=family), CapturingTransport()))
            module.generate(GenerateRequest(prompt="x", intellect="standard"))
            self.assertEqual("low", captured[field]["effort"] if field == "reasoning" else captured[field])

    def test_actual_provider_model_reports_its_own_intellect_without_rewriting_completion(self):
        class SubstitutingTransport(_Transport):
            def complete(self, *args, **kwargs):
                result = super().complete(*args, **kwargs)
                result.model = "gpt-5.6-terra"
                return result
        result = ProviderModule(ProviderBroker(self._provider(), SubstitutingTransport())).generate(
            GenerateRequest(prompt="x", intellect="standard"))
        self.assertEqual("completed", result.status)
        self.assertEqual("gpt-5.6-terra", result.model)
        self.assertEqual("smart", result.effective_intellect)

    def test_refresh_models_is_an_explicit_public_operation(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary); (root / "config").mkdir()
            (root / "config" / "settings.local.json").write_text(json.dumps({"provider": self._provider()}), encoding="utf-8")
            response = MagicMock(); response.read.return_value = b'{"data":[]}' ; response.__enter__.return_value = response
            with patch("ai_trading_companion.config.urlopen", return_value=response):
                result = ProviderModule(ProviderBroker(self._provider(), _Transport()), management_home=root).refresh_models()
            self.assertEqual("provider-management/v1", result["contract"])

    def test_non_cpa_multiple_families_is_migration_blocked(self):
        value = normalize_provider({
            "endpoints": [{"id": "legacy", "base_url": "https://example.test/v1", "api_key": "secret",
                           "families": ["openai", "anthropic"]}],
            "routes": [{"id": "gpt", "endpoint": "legacy", "model": "gpt-5.6-luna", "model_family": "openai",
                        "stages": ["chat"], "cost": {"tier": 0, "mode": "relative"}},
                       {"id": "claude", "endpoint": "legacy", "model": "claude-sonnet-5", "model_family": "anthropic",
                        "stages": ["chat"], "cost": {"tier": 0, "mode": "relative"}}],
        }, warn_legacy=False)
        endpoint = value["endpoints"][0]
        self.assertTrue(endpoint["needs_provider_kind_correction"])
        self.assertFalse(endpoint["enabled"])

    def test_cpa_is_the_only_multi_family_provider_kind(self):
        value = normalize_provider({
            "endpoints": [{"id": "relay", "provider_kind": "cpa", "base_url": "https://example.test/v1", "api_key": "secret",
                           "supported_families": ["openai", "anthropic"]}],
            "routes": [{"id": "gpt", "endpoint": "relay", "model": "gpt-5.6-luna", "model_family": "openai",
                        "stages": ["chat"], "cost": {"tier": 0, "mode": "relative"}},
                       {"id": "claude", "endpoint": "relay", "model": "claude-sonnet-5", "model_family": "anthropic",
                        "stages": ["chat"], "cost": {"tier": 0, "mode": "relative"}}],
        }, warn_legacy=False)
        self.assertEqual("cpa", value["endpoints"][0]["provider_kind"])
        self.assertEqual(["anthropic", "openai"], value["endpoints"][0]["supported_families"])


if __name__ == "__main__":
    unittest.main()
