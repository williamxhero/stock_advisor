from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from ai_trading_companion.provider_broker import ProviderBroker, TransportResult
from ai_trading_companion.provider_module import GenerateRequest, ProviderModule
from ai_trading_companion.provider_routes import normalize_provider


class _Transport:
    def __init__(self, *, family: str = "openai") -> None:
        self.model = "gpt-5.6-luna" if family == "openai" else "claude-sonnet-5"
        self.probes = 0
        self.payloads: list[dict[str, object]] = []

    def probe(self, _endpoint, _timeout):
        self.probes += 1
        return {"status": "available", "models": [self.model]}

    def complete(self, _endpoint, _route, payload, _timeout, _on_delta, _cancel):
        self.payloads.append(payload)
        return TransportResult(
            "complete language response",
            model=self.model,
            usage={"input_tokens": 4, "output_tokens": 3},
        )


def _provider(*, family: str = "openai") -> dict[str, object]:
    model = "gpt-5.6-luna" if family == "openai" else "claude-sonnet-5"
    intellect = "standard" if family == "openai" else "smart"
    level = "L1" if family == "openai" else "L2"
    return normalize_provider({
        "enabled": True,
        "endpoints": [{
            "id": "provider", "base_url": "https://example.test/v1", "api_key": "secret",
            "provider_kind": "single_family", "model_family": family,
        }],
        "routes": [{
            "id": "model", "endpoint": "provider", "model": model,
            "model_family": family, "intellect": intellect, "target_level": level,
            "stages": ["fast", "research", "judgment"], "effort": "low",
            "capabilities": ["stream", "race"], "cost": {"tier": 0, "mode": "relative"},
        }],
    }, warn_legacy=False)


class ProviderModuleTests(unittest.TestCase):
    def test_generate_exposes_complete_text_as_technical_success(self) -> None:
        audit = []
        module = ProviderModule(ProviderBroker(
            _provider(), _Transport(), audit=lambda kind, payload: audit.append((kind, payload)),
        ))

        result = module.generate(GenerateRequest(prompt="hello", intellect="standard"))

        self.assertEqual("completed", result.status)
        self.assertEqual("complete language response", result.text)
        self.assertEqual("openai", result.model_family)
        self.assertEqual("standard", result.effective_intellect)
        started = next(payload for kind, payload in audit if kind == "provider_invocation_started")
        self.assertEqual("provider_generate", started["stage"])

    def test_expired_deadline_is_not_started_without_a_probe_or_attempt(self) -> None:
        transport = _Transport()
        module = ProviderModule(ProviderBroker(_provider(), transport))

        result = module.generate(GenerateRequest(
            prompt="hello", intellect="standard", deadline=time.monotonic() - 1,
        ))

        self.assertEqual("not_started", result.status)
        self.assertEqual(0, transport.probes)
        self.assertEqual((), result.attempts)

    def test_explicit_effort_overrides_route_and_route_effort_is_the_fallback(self) -> None:
        for family, field in (("openai", "reasoning"), ("anthropic", "reasoning_effort")):
            transport = _Transport(family=family)
            intellect = "standard" if family == "openai" else "smart"
            module = ProviderModule(ProviderBroker(_provider(family=family), transport))
            module.generate(GenerateRequest(prompt="x", intellect=intellect, effort="high"))
            value = transport.payloads[-1][field]
            self.assertEqual("high", value["effort"] if isinstance(value, dict) else value)

            transport = _Transport(family=family)
            module = ProviderModule(ProviderBroker(_provider(family=family), transport))
            module.generate(GenerateRequest(prompt="x", intellect=intellect))
            value = transport.payloads[-1][field]
            self.assertEqual("low", value["effort"] if isinstance(value, dict) else value)

    def test_actual_provider_model_reports_its_intellect_without_rewriting_completion(self) -> None:
        class SubstitutingTransport(_Transport):
            def complete(self, *args, **kwargs):
                result = super().complete(*args, **kwargs)
                result.model = "gpt-5.6-terra"
                return result

        result = ProviderModule(ProviderBroker(_provider(), SubstitutingTransport())).generate(
            GenerateRequest(prompt="x", intellect="standard")
        )

        self.assertEqual("completed", result.status)
        self.assertEqual("gpt-5.6-terra", result.model)
        self.assertEqual("smart", result.effective_intellect)

    def test_known_unfulfilled_model_is_not_requested_again(self) -> None:
        provider = _provider()
        provider["endpoints"][0]["model_fulfillment"] = {
            "gpt-5.6-luna": {"actual_model": "gpt-5.6-terra", "fulfilled": False},
        }
        transport = _Transport()

        result = ProviderModule(ProviderBroker(provider, transport)).generate(
            GenerateRequest(prompt="x", intellect="standard")
        )

        self.assertEqual("unavailable", result.status)
        self.assertEqual([], transport.payloads)

    def test_refresh_models_is_an_explicit_public_operation(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "settings.local.json").write_text(
                json.dumps({"provider": _provider()}), encoding="utf-8",
            )
            response = MagicMock()
            response.read.return_value = b'{"data":[]}'
            response.__enter__.return_value = response
            with patch("ai_trading_companion.config.urlopen", return_value=response):
                result = ProviderModule(
                    ProviderBroker(_provider(), _Transport()), management_home=root,
                ).refresh_models()

        self.assertEqual("provider-management/v1", result["contract"])
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
