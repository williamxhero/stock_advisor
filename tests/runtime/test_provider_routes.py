from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_trading_companion.config import DEFAULT_PROVIDER, load_settings, migrate_embedded_provider_credentials
from ai_trading_companion.provider_routes import normalize_provider, route_cost_index


class ProviderRouteConfigTests(unittest.TestCase):
    def test_legacy_priority_191_migrates_to_tier_and_preference(self) -> None:
        provider = normalize_provider({
            "enabled": True,
            "models": {
                "research": {"id": "gpt-r"},
                "judgment": {"id": "gpt-j"},
                "fast": {"id": "gpt-f"},
            },
            "endpoints": [{
                "id": "legacy", "enabled": True, "priority": 191,
                "base_url": "https://example.test/v1",
                "credential_target": "AITradingCompanion/Test",
            }],
        }, warn_legacy=False)
        self.assertEqual({100}, {route["cost"]["tier"] for route in provider["routes"]})
        self.assertEqual({91}, {route["preference"] for route in provider["routes"]})

    def test_known_product_seed_routes_shift_before_claude_tier_is_added(self) -> None:
        provider = normalize_provider({
            "enabled": True,
            "models": {
                "research": {"id": "gpt-r"},
                "judgment": {"id": "gpt-j"},
                "fast": {"id": "gpt-f"},
            },
            "endpoints": [{
                "id": "direct-provider-example", "enabled": True, "priority": 100,
                "base_url": "https://example.test/v1",
                "credential_target": "AITradingCompanion/Test",
            }],
        }, warn_legacy=False)

        self.assertEqual({200}, {route["cost"]["tier"] for route in provider["routes"]})
        self.assertEqual(
            {"direct-provider-example-research", "direct-provider-example-judgment", "direct-provider-example-fast"},
            {route["id"] for route in provider["routes"]},
        )

    def test_custom_legacy_tier_is_not_repriced_by_seed_migration(self) -> None:
        provider = normalize_provider({
            "models": {"fast": {"id": "gpt-f"}},
            "endpoints": [{
                "id": "custom", "priority": 200, "base_url": "https://example.test/v1",
                "credential_target": "AITradingCompanion/Test",
            }],
        }, warn_legacy=False)

        self.assertEqual(200, provider["routes"][0]["cost"]["tier"])

    def test_product_defaults_place_claude_and_shifted_gpt_in_separate_tiers(self) -> None:
        routes = {route["id"]: route for route in DEFAULT_PROVIDER["routes"]}
        self.assertEqual({100}, {
            routes[route_id]["cost"]["tier"] for route_id in
            {"example-claude-research", "example-claude-judgment", "example-claude-fast"}
        })
        self.assertEqual({200}, {
            routes[route_id]["cost"]["tier"] for route_id in
            {"direct-provider-example-research", "direct-provider-example-judgment", "direct-provider-example-fast"}
        })

    def test_seed_template_is_normalized_and_matches_product_route_tiers(self) -> None:
        root = Path(__file__).resolve().parents[2]
        template = json.loads((root / "resources" / "seeds" / "companion.settings.template.json").read_text(encoding="utf-8"))
        provider = normalize_provider(template["provider"], warn_legacy=False)
        self.assertEqual(4, provider["schema_version"])
        self.assertEqual(9, len(provider["routes"]))
        self.assertFalse(provider["enabled"])
        self.assertFalse(any(route["enabled"] for route in provider["routes"]))
        self.assertEqual(2, template["provider"]["hedge"]["max_parallel"])

    def test_legacy_zero_max_parallel_is_normalized_to_two(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / "config" / "settings.local.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"provider": {"hedge": {"max_parallel": 0}}}), encoding="utf-8")

            settings = load_settings(home)

        self.assertEqual(2, settings.provider["hedge"]["max_parallel"])

    def test_route_owns_model_and_endpoint_owns_connection(self) -> None:
        provider = normalize_provider({
            "enabled": True,
            "endpoints": [{"id": "relay", "base_url": "https://example.test/v1", "credential_target": "cred"}],
            "routes": [{
                "id": "claude", "endpoint": "relay", "model": "claude-opus-5",
                "model_family": "anthropic", "cost": {"tier": 100, "mode": "relative", "weight": 1},
                "stages": ["research"],
            }],
        })
        self.assertNotIn("model", provider["endpoints"][0])
        self.assertEqual("claude-opus-5", provider["routes"][0]["model"])

    def test_unknown_family_is_valid_for_race(self) -> None:
        provider = normalize_provider({
            "endpoints": [{"id": "relay", "base_url": "https://example.test/v1", "credential_target": "cred"}],
            "routes": [{
                "id": "other", "endpoint": "relay", "model": "model-x", "model_family": "vendor-x",
                "cost": {"tier": 0, "mode": "relative", "weight": 1}, "stages": ["fast"],
            }],
        })
        self.assertEqual("vendor-x", provider["routes"][0]["model_family"])

    def test_endpoint_api_key_is_preserved_for_local_runtime_configuration(self) -> None:
        provider = normalize_provider({
            "endpoints": [{"id": "local", "base_url": "https://example.test/v1", "api_key": "secret"}],
            "routes": [],
        })
        self.assertEqual("secret", provider["endpoints"][0]["api_key"])

    def test_embedded_credentials_stay_in_local_runtime_configuration(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / "config" / "settings.local.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "provider": {
                    "enabled": True,
                    "models": {"fast": {"id": "gpt-fast", "effort": "low"}},
                    "endpoints": [{
                        "id": "relay", "enabled": True, "priority": 191,
                        "base_url": "https://relay.example/v1", "api_key": "local-secret",
                    }],
                },
                "research": {"web_access_gateway": {"mcp_url": "http://research.test/mcp"}},
            }), encoding="utf-8")
            result = migrate_embedded_provider_credentials(home)

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(0, result["migrated_endpoint_count"])
            self.assertEqual("local-secret", saved["provider"]["endpoints"][0]["api_key"])
            self.assertEqual(4, saved["provider"]["schema_version"])
            self.assertEqual(100, saved["provider"]["routes"][0]["cost"]["tier"])
            self.assertEqual(91, saved["provider"]["routes"][0]["preference"])
            self.assertEqual("http://research.test/mcp", saved["research"]["web_access_gateway"]["mcp_url"])

    def test_auto_tier_uses_official_price_multiplier_and_keeps_manual_lock(self) -> None:
        provider = normalize_provider({
            "routing": {"default_weight": .3},
            "endpoints": [{"id": "cheap", "base_url": "https://example.test/v1", "weight": .02}],
            "routes": [
                {"id": "auto", "endpoint": "cheap", "model": "gpt-5.6-terra", "model_family": "openai",
                 "tier_mode": "auto", "cost": {"tier": 200, "mode": "relative"}, "stages": ["research"]},
                {"id": "locked", "endpoint": "cheap", "model": "gpt-5.6-sol", "model_family": "openai",
                 "tier_mode": "manual", "cost": {"tier": 200, "mode": "relative"}, "stages": ["judgment"]},
            ],
        }, warn_legacy=False)
        routes = {row["id"]: row for row in provider["routes"]}
        self.assertEqual(0, routes["auto"]["cost"]["tier"])
        self.assertEqual(200, routes["locked"]["cost"]["tier"])
        self.assertAlmostEqual(.02, route_cost_index(provider, routes["auto"]))

    def test_cached_price_falls_back_to_normal_input_price(self) -> None:
        provider = normalize_provider({"routing": {"model_catalog": {
            "openai": {"gpt-x": {"aliases": [], "price": {"currency": "USD", "input_per_million": 1, "output_per_million": 2}, "quality": {}}},
        }}, "endpoints": [], "routes": []}, warn_legacy=False)
        price = provider["routing"]["model_catalog"]["openai"]["gpt-x"]["price"]
        self.assertEqual(1, price["cached_input_per_million"])

    def test_credential_writer_is_ignored_and_settings_are_written_locally(self) -> None:
        with TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / "config" / "settings.local.json"
            path.parent.mkdir(parents=True)
            original = json.dumps({
                "provider": {
                    "enabled": True,
                    "models": {"fast": {"id": "gpt-fast"}},
                    "endpoints": [{
                        "id": "relay", "base_url": "https://relay.example/v1", "api_key": "local-secret",
                    }],
                },
            })
            path.write_text(original, encoding="utf-8")

            result = migrate_embedded_provider_credentials(home, credential_writer=lambda _target, _value: (_ for _ in ()).throw(OSError("must not run")))
            self.assertEqual(0, result["migrated_endpoint_count"])
            self.assertEqual("local-secret", json.loads(path.read_text(encoding="utf-8"))["provider"]["endpoints"][0]["api_key"])


if __name__ == "__main__":
    unittest.main()
