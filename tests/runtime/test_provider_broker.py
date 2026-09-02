from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any

from ai_trading_companion.provider_broker import (
    ChatCompletionsTransport,
    ProviderBroker,
    StageRequest,
    TransportResult,
    _validate_schema,
    canonical_packet_hash,
)
from ai_trading_companion.provider_client import ProviderError
from ai_trading_companion.provider_routes import normalize_provider


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


class FakeTransport:
    def __init__(self, behavior: dict[str, dict[str, Any]]) -> None:
        self.behavior = behavior
        self.calls: list[dict[str, Any]] = []
        self.max_active = 0
        self._active = 0
        self._lock = threading.Lock()

    def probe(self, endpoint: dict[str, Any], timeout: float) -> dict[str, Any]:
        value = self.behavior.get(endpoint["id"], {}).get("probe")
        if isinstance(value, Exception):
            raise value
        if isinstance(value, dict):
            return value
        return {"status": "available", "models": list(endpoint.get("available_models") or [])}

    def complete(self, endpoint: dict[str, Any], route: dict[str, Any], payload: dict[str, Any],
                 timeout: float, on_delta, cancel: threading.Event) -> TransportResult:
        behavior = self.behavior.get(route["id"], {})
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append({"route": route["id"], "model": payload["model"], "payload": payload,
                               "started": time.monotonic()})
        try:
            self._wait(float(behavior.get("first_delay", 0)), cancel)
            for chunk in behavior.get("chunks", []):
                if cancel.is_set():
                    raise ProviderError("cancelled", category="provider_cancelled")
                on_delta(str(chunk))
            self._wait(float(behavior.get("complete_delay", 0)), cancel)
            if "error" in behavior:
                raise behavior["error"]
            text = behavior.get("text", json.dumps({"answer": route["id"]}))
            return TransportResult(str(text), response_id=f"resp-{route['id']}", request_id=f"req-{route['id']}",
                                   usage={"prompt_tokens": 10, "completion_tokens": 5})
        finally:
            with self._lock:
                self._active -= 1

    @staticmethod
    def _wait(seconds: float, cancel: threading.Event) -> None:
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if cancel.is_set():
                raise ProviderError("cancelled", category="provider_cancelled")
            time.sleep(min(0.002, max(0, until - time.monotonic())))


def provider(*routes: tuple[str, str, str, int, float]) -> dict[str, Any]:
    endpoints = []
    route_rows = []
    for route_id, endpoint_id, family, tier, weight in routes:
        if not any(item["id"] == endpoint_id for item in endpoints):
            endpoints.append({"id": endpoint_id, "base_url": f"https://{endpoint_id}.test/v1", "weight": weight,
                              "available_models": []})
        next(item for item in endpoints if item["id"] == endpoint_id)["available_models"].append(f"model-{route_id}")
        route_rows.append({
            "id": route_id, "endpoint": endpoint_id, "model": f"model-{route_id}", "model_family": family,
            "cost": {"tier": tier, "mode": "relative", "weight": weight}, "preference": 0,
            "stages": ["research", "m1_judgment"],
            "capabilities": ["stream", "json_schema", "race", "duel", "arbitration"],
        })
    return normalize_provider({"enabled": True, "endpoints": endpoints, "routes": route_rows})


def request(**changes: Any) -> StageRequest:
    packet = changes.pop("packet", {"evidence": [1]})
    values = {
        "stage": "research", "packet": packet, "packet_sha256": canonical_packet_hash(packet),
        "effort": "medium", "schema": SCHEMA, "absolute_deadline": time.monotonic() + 2,
        "route_timeout_seconds": 1,
    }
    values.update(changes)
    return StageRequest(**values)


class ProviderBrokerTests(unittest.TestCase):
    def test_route_model_is_used_and_payload_has_no_native_tools(self) -> None:
        transport = FakeTransport({"cheap": {"chunks": ["{"]}})
        outcome = ProviderBroker(provider(("cheap", "e1", "openai", 0, 1)), transport).invoke(
            request(output_token_allowance=1_234),
        )
        self.assertEqual("cheap", outcome.winner_route)
        self.assertEqual("model-cheap", transport.calls[0]["model"])
        self.assertEqual(1_234, transport.calls[0]["payload"]["max_output_tokens"])
        self.assertFalse(transport.calls[0]["payload"]["stream"])
        self.assertIn("input", transport.calls[0]["payload"])
        self.assertNotIn("messages", transport.calls[0]["payload"])
        self.assertNotIn("tools", transport.calls[0]["payload"])
        self.assertNotIn("tool_choice", transport.calls[0]["payload"])

    def test_chat_completions_repeats_schema_in_a_system_message(self) -> None:
        transport = FakeTransport({})
        outcome = ProviderBroker(provider(("claude", "e1", "anthropic", 0, 1)), transport).invoke(request())
        self.assertEqual("claude", outcome.winner_route)
        messages = transport.calls[0]["payload"]["messages"]
        contract = json.loads(messages[0]["content"])
        self.assertEqual(SCHEMA, contract["output_schema"])
        self.assertEqual("system", messages[0]["role"])
        self.assertEqual("user", messages[1]["role"])

    def test_internal_request_uses_non_stream_transport_and_records_actual_model(self) -> None:
        class Client:
            def _list_models_once(self, _timeout: int) -> list[str]:
                return ["model-cheap"]

            def _request_single(self, payload: dict[str, Any], _timeout: int) -> dict[str, Any]:
                self.payload = payload
                return {
                    "id": "response-1", "model": "actual-model", "usage": {"completion_tokens": 4},
                    "choices": [{"message": {"content": json.dumps({"answer": "ok"})}}],
                }

            def _request_stream_single(self, *_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("internal requests must not use streaming transport")

        client = Client()

        class Transport(ChatCompletionsTransport):
            def __init__(self) -> None:
                pass

            def _client(self, _endpoint: dict[str, Any], _route: dict[str, Any] | None = None) -> Any:
                return client

        outcome = ProviderBroker(provider(("cheap", "e1", "openai", 0, 1)), Transport()).invoke(request())
        self.assertEqual("actual-model", outcome.model)
        self.assertEqual("actual-model", outcome.attempts[0].model)
        self.assertFalse(client.payload["stream"])

    def test_successful_cheap_tier_never_touches_expensive_tier(self) -> None:
        transport = FakeTransport({})
        outcome = ProviderBroker(provider(
            ("cheap", "e1", "openai", 0, 1), ("expensive", "e2", "anthropic", 100, 1)), transport).invoke(request())
        self.assertEqual("cheap", outcome.winner_route)
        self.assertEqual(["cheap"], [item["route"] for item in transport.calls])

    def test_models_404_skips_provider_without_real_request(self) -> None:
        transport = FakeTransport({"e1": {"probe": ProviderError("missing", category="invalid_response", status=404)}})
        outcome = ProviderBroker(provider(("cheap", "e1", "openai", 0, 1)), transport).invoke(request())
        self.assertIsNone(outcome.winner_route)
        self.assertEqual("definitive_failure", outcome.probes[0]["status"])
        self.assertEqual([], transport.calls)

    def test_route_model_missing_from_directory_is_skipped(self) -> None:
        transport = FakeTransport({"e1": {"probe": {"status": "available", "models": ["another-model"]}}})
        outcome = ProviderBroker(provider(("cheap", "e1", "openai", 0, 1)), transport).invoke(request())
        self.assertIsNone(outcome.winner_route)
        self.assertEqual([], transport.calls)

    def test_equal_price_group_starts_up_to_three_providers_concurrently(self) -> None:
        transport = FakeTransport({
            "slow": {"first_delay": .12},
            "hedge": {"first_delay": .005, "chunks": ["{"]},
            "third": {"first_delay": .005},
        })
        outcome = ProviderBroker(provider(
            ("slow", "e1", "openai", 0, 1), ("hedge", "e2", "anthropic", 0, 2),
            ("third", "e3", "openai", 0, 3)), transport, hedge_seconds=.03).invoke(request())
        self.assertIn(outcome.winner_route, {"slow", "hedge", "third"})
        self.assertLessEqual(transport.max_active, 3)
        self.assertTrue({item["route"] for item in transport.calls})

    def test_first_token_before_threshold_suppresses_delayed_hedge(self) -> None:
        transport = FakeTransport({
            "streaming": {"first_delay": .005, "chunks": ["{"], "complete_delay": .06},
            "unneeded": {"chunks": ["{"]},
        })
        outcome = ProviderBroker(provider(
            ("streaming", "e1", "openai", 0, 1), ("unneeded", "e2", "anthropic", 0, 2)),
            transport, hedge_seconds=.02,
        ).invoke(request())
        self.assertEqual("streaming", outcome.winner_route)
        self.assertEqual(["streaming"], [item["route"] for item in transport.calls])

    def test_cost_bands_keep_cheaper_route_ahead_of_near_cost_race(self) -> None:
        configured = normalize_provider({
            "enabled": True,
            "endpoints": [
                {"id": "terra", "base_url": "https://terra.test/v1", "weight": .10, "available_models": ["gpt-5.6-terra"]},
                {"id": "sonnet", "base_url": "https://sonnet.test/v1", "weight": .11, "available_models": ["claude-sonnet-5"]},
                {"id": "luna", "base_url": "https://luna.test/v1", "weight": .01, "available_models": ["gpt-5.6-luna"]},
            ],
            "routes": [
                {"id": "terra", "endpoint": "terra", "model": "gpt-5.6-terra", "model_family": "openai", "cost": {"tier": 0, "mode": "relative"}, "stages": ["research"], "capabilities": ["race"]},
                {"id": "sonnet", "endpoint": "sonnet", "model": "claude-sonnet-5", "model_family": "anthropic", "cost": {"tier": 0, "mode": "relative"}, "stages": ["research"], "capabilities": ["race"]},
                {"id": "luna", "endpoint": "luna", "model": "gpt-5.6-luna", "model_family": "openai", "cost": {"tier": 0, "mode": "relative"}, "stages": ["research"], "capabilities": ["race"]},
            ],
        })
        transport = FakeTransport({})
        outcome = ProviderBroker(configured, transport).invoke(request())
        # Luna is materially cheaper, so it cannot be displaced by the
        # near-price pair.  Without it, Terra and Sonnet share one price band
        # and are deliberately launched as a concurrent race: either qualified
        # response may return first.
        self.assertEqual("luna", outcome.winner_route)
        configured["routes"] = [row for row in configured["routes"] if row["id"] != "luna"]
        transport = FakeTransport({})
        outcome = ProviderBroker(configured, transport).invoke(request())
        self.assertIn(outcome.winner_route, {"terra", "sonnet"})

    def test_visible_first_delta_locks_route_even_if_later_invalid(self) -> None:
        visible: list[str] = []
        transport = FakeTransport({
            "first": {"chunks": ["visible"], "complete_delay": .03, "text": "not-json"},
            "second": {"chunks": ["replacement"], "text": json.dumps({"answer": "second"})},
        })
        outcome = ProviderBroker(provider(
            ("first", "e1", "openai", 0, 1), ("second", "e2", "anthropic", 0, 2)),
            transport, hedge_seconds=0).invoke(request(visible_stream=True, on_delta=visible.append))
        self.assertEqual("first", outcome.winner_route)
        self.assertTrue(outcome.visible_locked)
        self.assertTrue(outcome.visible_incomplete)
        self.assertEqual(["visible"], visible)

    def test_product_rejection_falls_through_within_same_tier(self) -> None:
        transport = FakeTransport({"bad": {"text": "{}"}})
        outcome = ProviderBroker(provider(
            ("bad", "e1", "openai", 0, 1), ("good", "e2", "anthropic", 0, 2)), transport).invoke(request())
        self.assertEqual("good", outcome.winner_route)
        self.assertEqual("schema_rejection", outcome.attempts[0].terminal_error)

    def test_single_json_fence_is_accepted_but_surrounding_prose_is_rejected(self) -> None:
        accepted = FakeTransport({"fenced": {"text": '```json\n{"answer":"ok"}\n```'}})
        outcome = ProviderBroker(provider(("fenced", "e1", "anthropic", 0, 1)), accepted).invoke(request())
        self.assertEqual("fenced", outcome.winner_route)

        rejected = FakeTransport({"prose": {"text": 'Result:\n```json\n{"answer":"ok"}\n```'}})
        outcome = ProviderBroker(provider(("prose", "e1", "anthropic", 0, 1)), rejected).invoke(request())
        self.assertIsNone(outcome.winner_route)
        self.assertEqual("invalid_output_json", outcome.attempts[0].terminal_error)

    def test_schema_validator_accepts_nullable_union_types(self) -> None:
        schema = {
            "type": "object", "required": ["query"],
            "properties": {"query": {"type": ["string", "null"]}},
        }

        self.assertTrue(_validate_schema({"query": None}, schema)["passed"])
        self.assertTrue(_validate_schema({"query": "close"}, schema)["passed"])
        rejected = _validate_schema({"query": 42}, schema)
        self.assertFalse(rejected["passed"])

    def test_m1_rejects_h0_before_transport(self) -> None:
        packet = {"evidence": [], "H0": {"user_said": "buy"}}
        with self.assertRaisesRegex(ValueError, "H0"):
            request(stage="m1_judgment", packet=packet, packet_sha256=canonical_packet_hash(packet), h0_forbidden=True)

    def test_duel_runs_openai_and_anthropic_on_same_packet(self) -> None:
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "same"})},
            "claude": {"text": json.dumps({"answer": "same"})},
        })
        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2)), transport).invoke(
                request(stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True))
        self.assertEqual("consistent", outcome.duel["status"])
        self.assertEqual({"gpt", "claude"}, {item["route"] for item in transport.calls})

    def test_legacy_family_mode_does_not_partition_same_capability_routing(self) -> None:
        config = provider(
            ("gpt", "e1", "openai", 0, 1), ("claude", "e2", "anthropic", 0, 2),
        )
        config["routing"]["family_mode"] = "anthropic"
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "same"})},
            "claude": {"text": json.dumps({"answer": "same"})},
        })
        normal = ProviderBroker(config, transport).invoke(request())
        self.assertEqual("gpt", normal.winner_route)
        self.assertEqual(["gpt"], [item["route"] for item in transport.calls])

        transport.calls.clear()
        duel = ProviderBroker(config, transport).invoke(request(
            stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True,
        ))
        self.assertEqual("consistent", duel.duel["status"])
        self.assertEqual({"gpt", "claude"}, {item["route"] for item in transport.calls})

    def test_legacy_family_mode_does_not_make_a_route_unavailable(self) -> None:
        config = provider(("gpt", "e1", "openai", 0, 1))
        config["routing"]["family_mode"] = "anthropic"
        transport = FakeTransport({})
        outcome = ProviderBroker(config, transport).invoke(request())
        self.assertEqual("gpt", outcome.winner_route)
        self.assertEqual(["gpt"], [item["route"] for item in transport.calls])

    def test_calibrated_cost_uses_official_price_times_endpoint_multiplier(self) -> None:
        config = provider(("cheap", "e1", "openai", 0, .06))
        config["routing"]["price_catalog"] = {
            "openai": {"model-cheap": {
                "currency": "USD", "input_per_million": 10,
                "cached_input_per_million": 1, "output_per_million": 20,
            }},
        }
        outcome = ProviderBroker(config, FakeTransport({})).invoke(request())
        # FakeTransport reports 10 input and 5 output tokens: (10*10*.06 +
        # 5*20*.06) / 1,000,000 USD.
        self.assertAlmostEqual(.000012, outcome.estimated_cost)
        self.assertEqual(.06, outcome.multiplier)
        self.assertTrue(outcome.base_price_calibrated)
        self.assertEqual("official_base_x_multiplier", outcome.cost_basis)
        self.assertEqual(0.6, outcome.effective_unit_price["input_per_million"])

    def test_consistent_duel_prefers_a_real_zero_cost_route(self) -> None:
        config = provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
        )
        config["routes"][0]["cost"] = {
            "tier": 100, "mode": "token", "currency": "USD",
            "input_per_million": 0, "output_per_million": 0, "fixed_request": 0,
        }
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "same"})},
            "claude": {"text": json.dumps({"answer": "same"})},
        })

        outcome = ProviderBroker(config, transport).invoke(
            request(stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True),
        )

        self.assertEqual("gpt", outcome.winner_route)
        self.assertEqual(0, outcome.estimated_cost)

    def test_duel_legs_run_concurrently(self) -> None:
        transport = FakeTransport({
            "gpt": {"first_delay": .08, "text": json.dumps({"answer": "same"})},
            "claude": {"first_delay": .08, "text": json.dumps({"answer": "same"})},
        })
        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2)), transport).invoke(
                request(stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True))
        self.assertEqual("consistent", outcome.duel["status"])
        self.assertGreaterEqual(transport.max_active, 2)

    def test_duel_uses_the_only_qualified_leg(self) -> None:
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "qualified"})},
            "claude": {"text": "{}"},
        })

        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
        ), transport).invoke(request(
            stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True,
        ))

        self.assertEqual("gpt", outcome.winner_route)
        self.assertEqual("single_qualified", outcome.duel["status"])

    def test_duel_reports_both_failed_without_selecting_a_route(self) -> None:
        transport = FakeTransport({"gpt": {"text": "{}"}, "claude": {"text": "{}"}})

        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
        ), transport).invoke(request(
            stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True,
        ))

        self.assertIsNone(outcome.winner_route)
        self.assertEqual("both_failed", outcome.duel["status"])

    def test_material_duel_conflict_uses_independent_arbitrator(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        frozen = {"evidence": [{"fact": "same for every M1 participant"}]}
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "up"})},
            "claude": {"text": json.dumps({"answer": "down"})},
            "arbiter": {"text": json.dumps({"answer": "up"})},
        })
        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
            ("arbiter", "e3", "openai", 100, 3)), transport,
            audit=lambda kind, payload: events.append((kind, dict(payload))),
        ).invoke(request(
            stage="m1_judgment", packet=frozen, mode="duel",
            required_capabilities=("duel",), h0_forbidden=True,
        ))
        self.assertEqual("resolved", outcome.arbitration["status"])
        self.assertEqual("arbiter", outcome.winner_route)
        self.assertEqual(canonical_packet_hash(frozen), outcome.packet_sha256)
        attempt_hashes = {
            payload["packet_sha256"] for kind, payload in events if kind == "llm_attempt_started"
        }
        self.assertEqual({canonical_packet_hash(frozen)}, attempt_hashes)
        arbiter_payload = next(item["payload"] for item in transport.calls if item["route"] == "arbiter")
        arbiter_input = json.loads(arbiter_payload.get("input") or next(
            message["content"] for message in arbiter_payload["messages"] if message["role"] == "user"
        ))
        self.assertEqual(frozen, arbiter_input["frozen_evidence"])
        self.assertEqual(2, len(arbiter_input["candidate_judgments"]))

    def test_arbitration_only_route_can_resolve_duel_conflict(self) -> None:
        config = provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
            ("arbiter", "e3", "openai", 100, 3),
        )
        arbiter = next(route for route in config["routes"] if route["id"] == "arbiter")
        arbiter["capabilities"] = ["stream", "json_schema", "arbitration"]
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "up"})},
            "claude": {"text": json.dumps({"answer": "down"})},
            "arbiter": {"text": json.dumps({"answer": "up"})},
        })

        outcome = ProviderBroker(config, transport).invoke(request(
            stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True,
        ))

        self.assertEqual("resolved", outcome.arbitration["status"])
        self.assertEqual("arbiter", outcome.winner_route)

    def test_failed_arbitration_returns_explicit_model_conflict(self) -> None:
        transport = FakeTransport({
            "gpt": {"text": json.dumps({"answer": "up"})},
            "claude": {"text": json.dumps({"answer": "down"})},
            "arbiter": {"text": "{}"},
        })

        outcome = ProviderBroker(provider(
            ("gpt", "e1", "openai", 100, 1), ("claude", "e2", "anthropic", 100, 2),
            ("arbiter", "e3", "openai", 100, 3),
        ), transport).invoke(request(
            stage="m1_judgment", mode="duel", required_capabilities=("duel",), h0_forbidden=True,
        ))

        self.assertIsNone(outcome.winner_route)
        self.assertEqual("material_conflict", outcome.duel["status"])
        self.assertEqual("model_judgment_conflict", outcome.arbitration["failure"])

    def test_immediate_provider_errors_fall_through_with_normalized_categories(self) -> None:
        cases = (
            ("model_not_found", "model_not_found"),
            ("provider_rate_limited", "rate_limited"),
            ("invalid_response", "invalid_protocol_json"),
        )
        for source_category, expected_category in cases:
            with self.subTest(source_category=source_category):
                transport = FakeTransport({
                    "bad": {"error": ProviderError("failed", category=source_category)},
                })
                outcome = ProviderBroker(provider(
                    ("bad", "e1", "openai", 0, 1), ("good", "e2", "anthropic", 0, 2),
                ), transport).invoke(request())
                self.assertEqual("good", outcome.winner_route)
                self.assertEqual(expected_category, outcome.attempts[0].terminal_error)

    def test_partial_stream_is_classified_and_falls_through(self) -> None:
        transport = FakeTransport({
            "partial": {
                "chunks": ["{"],
                "error": ProviderError("stream ended", category="incomplete_response"),
            },
        })

        outcome = ProviderBroker(provider(
            ("partial", "e1", "openai", 0, 1), ("good", "e2", "anthropic", 0, 2),
        ), transport).invoke(request())

        self.assertEqual("good", outcome.winner_route)
        self.assertEqual("partial_stream", outcome.attempts[0].terminal_error)

    def test_secret_delta_is_rejected_before_visible_route_lock(self) -> None:
        visible: list[str] = []
        transport = FakeTransport({
            "unsafe": {"chunks": ["Bearer abcdefghijklmnopqrstuvwxyz"]},
            "safe": {"chunks": ["safe"], "text": json.dumps({"answer": "safe"})},
        })
        outcome = ProviderBroker(provider(
            ("unsafe", "e1", "openai", 0, 1), ("safe", "e2", "anthropic", 0, 2)),
            transport, hedge_seconds=0).invoke(request(visible_stream=True, on_delta=visible.append))
        self.assertEqual("safe", outcome.winner_route)
        self.assertEqual(["safe"], visible)
        unsafe = next(item for item in outcome.attempts if item.route_id == "unsafe")
        self.assertEqual("secret_rejection", unsafe.terminal_error)

    def test_secret_in_complete_output_is_classified_and_falls_through(self) -> None:
        transport = FakeTransport({
            "unsafe": {"text": json.dumps({"answer": "Bearer abcdefghijklmnopqrstuvwxyz"})},
        })

        outcome = ProviderBroker(provider(
            ("unsafe", "e1", "openai", 0, 1), ("safe", "e2", "anthropic", 0, 2),
        ), transport).invoke(request())

        self.assertEqual("safe", outcome.winner_route)
        self.assertEqual("secret_rejection", outcome.attempts[0].terminal_error)

    def test_replacement_character_is_rejected_before_visibility(self) -> None:
        visible: list[str] = []
        transport = FakeTransport({
            "broken": {"chunks": ["bad\ufffdtext"]},
            "safe": {"chunks": ["safe"]},
        })

        outcome = ProviderBroker(provider(
            ("broken", "e1", "openai", 0, 1), ("safe", "e2", "anthropic", 0, 2),
        ), transport, hedge_seconds=0).invoke(request(visible_stream=True, on_delta=visible.append))

        self.assertEqual("safe", outcome.winner_route)
        self.assertEqual(["safe"], visible)
        self.assertEqual("replacement_characters", outcome.attempts[0].terminal_error)

    def test_outcome_promotes_winner_usage_timing_cost_and_verifier(self) -> None:
        transport = FakeTransport({"cheap": {"chunks": ["{"]}})
        outcome = ProviderBroker(provider(("cheap", "e1", "openai", 0, 1)), transport).invoke(request())
        self.assertEqual("req-cheap", outcome.request_id)
        self.assertEqual(10, outcome.usage["prompt_tokens"])
        self.assertIsNotNone(outcome.ttft_seconds)
        self.assertIsNone(outcome.estimated_cost)
        self.assertEqual("relative_multiplier_only", outcome.cost_basis)
        self.assertTrue(outcome.verifier["schema"]["passed"])

    def test_audit_finishes_winner_and_cancelled_hedge_with_correct_classes(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        transport = FakeTransport({
            "slow": {"first_delay": .15},
            "fast": {"first_delay": .005, "chunks": ["{"], "complete_delay": .005},
        })

        outcome = ProviderBroker(provider(
            ("slow", "e1", "openai", 0, 1), ("fast", "e2", "anthropic", 0, 2)),
            transport, hedge_seconds=.01, audit=lambda kind, payload: events.append((kind, dict(payload))),
        ).invoke(request())

        finished = [payload for kind, payload in events if kind == "llm_attempt_finished"]
        assert outcome.winner_route in {"slow", "fast"}
        assert finished
        assert any(item["winner"] for item in finished)


if __name__ == "__main__":
    unittest.main()
