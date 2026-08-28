from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from ai_trading_companion.config import load_settings, provider_management
from ai_trading_companion.provider_routes import SLOT_STAGES


def endpoint(provider_id: str, families: list[str], *, weight: float = 0.3, enabled: bool = True) -> dict[str, object]:
    return {
        "id": provider_id,
        "base_url": f"https://{provider_id}.example/v1/chat/completions",
        "families": families,
        "weight": weight,
        "enabled": enabled,
    }


def routes(provider_id: str, families: list[str]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for family in families:
        for slot, stages in SLOT_STAGES.items():
            result.append({
                "id": f"{provider_id}-{family}-{slot}", "endpoint": provider_id,
                "slot": slot, "model": f"{family}-{slot}-model", "model_family": family,
                "enabled": True, "stages": stages,
                "cost": {"tier": 0, "mode": "relative"}, "preference": 2,
                "effort": "medium", "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"],
            })
    return result


class ProviderManagementTests(unittest.TestCase):
    def _home(self, directory: str) -> Path:
        home = Path(directory)
        path = home / "config" / "settings.local.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "provider": {
                "schema_version": 3, "enabled": False,
                "routing": {"family_mode": "auto", "default_weight": 0.3, "price_catalog": {}},
                "endpoints": [], "routes": [],
            },
        }), encoding="utf-8")
        return home

    def test_create_updates_endpoint_multiplier_and_redacts_key(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            result = provider_management(home, {
                "action": "upsert", "endpoint": endpoint("alpha", ["openai", "anthropic"], weight=0.06),
                "routes": routes("alpha", ["openai", "anthropic"]), "api_key": "test-local-key",
                "family_mode": "anthropic",
            })
            self.assertNotIn("test-local-key", json.dumps(result))
            self.assertEqual("tes...key", result["provider"]["endpoints"][0]["api_key_hint"])
            saved = load_settings(home).provider
            self.assertEqual("anthropic", saved["routing"]["family_mode"])
            self.assertEqual(0.06, saved["endpoints"][0]["weight"])
            self.assertEqual("test-local-key", saved["endpoints"][0]["api_key"])
            self.assertEqual({0.06}, {row["cost"]["weight"] for row in saved["routes"]})
            self.assertEqual({"responses", "chat_completions"}, {row["transport"] for row in saved["routes"]})

    def test_clone_requires_new_key_and_does_not_copy_secret(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            provider_management(home, {
                "action": "upsert", "endpoint": endpoint("alpha", ["openai"]),
                "routes": routes("alpha", ["openai"]), "api_key": "first-key",
            })
            with self.assertRaisesRegex(ValueError, "new API key"):
                provider_management(home, {
                    "action": "clone", "source_id": "alpha",
                    "endpoint": endpoint("beta", ["openai"]),
                })
            result = provider_management(home, {
                "action": "clone", "source_id": "alpha",
                "endpoint": endpoint("beta", ["openai"], weight=0.2), "api_key": "second-key",
            })
            self.assertEqual("clone", result["changes"][0]["action"])
            self.assertNotIn("second-key", json.dumps(result))
            saved = {item["id"]: item for item in load_settings(home).provider["endpoints"]}
            self.assertEqual("first-key", saved["alpha"]["api_key"])
            self.assertEqual("second-key", saved["beta"]["api_key"])

    def test_archive_restore_and_confirmed_delete_keep_statistics_independent(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            provider_management(home, {
                "action": "upsert", "endpoint": endpoint("alpha", ["openai"]),
                "routes": routes("alpha", ["openai"]), "api_key": "test-key",
            })
            provider_management(home, {"action": "archive", "id": "alpha"})
            archived = load_settings(home).provider
            self.assertTrue(archived["endpoints"][0]["archived"])
            self.assertFalse(archived["endpoints"][0]["enabled"])
            self.assertFalse(any(row["enabled"] for row in archived["routes"]))
            provider_management(home, {"action": "restore", "id": "alpha"})
            self.assertTrue(all(row["enabled"] for row in load_settings(home).provider["routes"]))
            with self.assertRaisesRegex(ValueError, "confirmation"):
                provider_management(home, {"action": "permanent_delete", "id": "alpha"})
            provider_management(home, {"action": "permanent_delete", "id": "alpha", "confirmed": True})
            self.assertEqual([], load_settings(home).provider["endpoints"])

    def test_invalid_save_has_no_partial_write(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            path = home / "config" / "settings.local.json"
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "http\\(s\\)"):
                provider_management(home, {
                    "action": "upsert",
                    "endpoint": {**endpoint("alpha", ["openai"]), "base_url": "ftp://alpha.example/v1"},
                    "routes": routes("alpha", ["openai"]), "api_key": "test-key",
                })
            self.assertEqual(before, path.read_bytes())

    def test_refresh_models_keeps_key_private_and_records_discovered_identifiers(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            provider_management(home, {
                "action": "upsert", "endpoint": endpoint("alpha", ["openai"]),
                "routes": routes("alpha", ["openai"]), "api_key": "test-local-key",
            })
            response = MagicMock()
            response.read.return_value = b'{"data":[{"id":"codex-z"},{"id":"codex-a"}]}'
            response.__enter__.return_value = response
            with patch("ai_trading_companion.config.urlopen", return_value=response) as request:
                result = provider_management(home, {"action": "refresh_models", "id": "alpha"})
            self.assertNotIn("test-local-key", json.dumps(result))
            self.assertEqual(["codex-a", "codex-z"], result["provider"]["endpoints"][0]["available_models"])
            self.assertEqual("tes...key", result["provider"]["endpoints"][0]["api_key_hint"])
            self.assertIn("/v1/models", request.call_args.args[0].full_url)

    def test_draft_slots_are_generated_for_every_selected_family(self) -> None:
        with TemporaryDirectory() as directory:
            result = provider_management(self._home(directory), {
                "action": "draft_slots", "id": "alpha", "families": ["openai", "anthropic"],
            })
            self.assertEqual(6, len(result["draft_routes"]))
            self.assertEqual({"research", "judgment", "fast"}, {row["slot"] for row in result["draft_routes"]})

    def test_refresh_404_disables_routes_without_a_model_directory(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            provider_management(home, {"action": "upsert", "endpoint": endpoint("alpha", ["openai"]),
                                       "routes": routes("alpha", ["openai"]), "api_key": "test-key"})
            with patch("ai_trading_companion.config.urlopen", side_effect=HTTPError("https://alpha.example/models", 404, "missing", {}, None)):
                result = provider_management(home, {"action": "refresh_models", "id": "alpha"})
            self.assertFalse(any(item["enabled"] for item in result["provider"]["routes"]))
            self.assertEqual([], result["provider"]["endpoints"][0]["available_models"])
            self.assertEqual("unknown_http_404", result["provider"]["endpoints"][0]["model_directory_status"])

    def test_empty_model_directory_disables_configured_slots(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            provider_management(home, {"action": "upsert", "endpoint": endpoint("alpha", ["openai"]),
                                       "routes": routes("alpha", ["openai"]), "api_key": "test-key"})
            response = MagicMock(); response.read.return_value = b'{"data":[]}'
            response.__enter__.return_value = response
            with patch("ai_trading_companion.config.urlopen", return_value=response):
                result = provider_management(home, {"action": "refresh_models", "id": "alpha"})
            self.assertFalse(any(item["enabled"] for item in result["provider"]["routes"]))
            self.assertEqual([], result["provider"]["endpoints"][0]["available_models"])
            self.assertEqual("empty", result["provider"]["endpoints"][0]["model_directory_status"])

    def test_model_catalog_recalculates_auto_tier_but_not_manual_route(self) -> None:
        with TemporaryDirectory() as directory:
            home = self._home(directory)
            rows = routes("alpha", ["openai"])
            for row in rows:
                row["model"] = "gpt-5.6-terra"
                row["tier_mode"] = "auto"
            rows[0]["tier_mode"] = "manual"; rows[0]["cost"] = {"tier": 200, "mode": "relative"}
            result = provider_management(home, {"action": "upsert", "endpoint": endpoint("alpha", ["openai"], weight=.02), "routes": rows, "api_key": "test-key"})
            saved = {row["id"]: row for row in result["provider"]["routes"]}
            self.assertEqual(200, saved["alpha-openai-research"]["cost"]["tier"])
            self.assertEqual(0, saved["alpha-openai-judgment"]["cost"]["tier"])


if __name__ == "__main__":
    unittest.main()
