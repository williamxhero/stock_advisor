from __future__ import annotations

import json
from datetime import datetime, timezone

from ai_trading_companion.provider_stats import ProviderStatistics
from ai_trading_companion.store import CompanionStore


AS_OF = datetime(2026, 8, 27, 7, 20, tzinfo=timezone.utc)


def attempt(index: int, **changes):
    row = {
        "attempt_id": f"a-{index}", "endpoint_id": "endpoint-a", "route_id": "route-a",
        "model": "gpt-test", "model_family": "openai", "stage": "research", "tier": 100,
        "recorded_at": f"2026-08-27T07:{index:02d}:00Z", "protocol_success": 1,
        "product_success": 1, "ttft_ms": index * 100, "duration_ms": index * 100,
        "terminal_error": None, "delayed_start": 0, "winner": 0, "cancellation_class": None,
        "input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 5, "reasoning_tokens": 2,
        "estimated_cost": 0.1, "cost_mode": "token", "preference": 7,
        "actual_cost": None, "currency": "USD",
    }
    row.update(changes)
    return row


def test_statistics_use_nearest_rank_and_keep_sample_sizes():
    rows = [attempt(index) for index in range(1, 5)]

    item = ProviderStatistics(rows, as_of=AS_OF).summary(window="24h")["items"][0]

    assert item["sample_size"] == 4
    assert item["ttft_ms"] == {"p50": 200, "p90": 400, "p95": 400, "samples": 4}
    assert item["duration_ms"] == {"p50": 200, "p90": 400, "p95": 400, "samples": 4}
    assert item["insufficient_data"] is True
    assert item["cached_input_tokens"] == 12
    assert item["reasoning_tokens"] == 8


def test_completion_latency_uses_only_complete_protocol_responses_and_costs_do_not_mix_currencies():
    rows = [
        attempt(1, protocol_success=1, duration_ms=100, actual_cost=0.2, currency="USD"),
        attempt(2, protocol_success=0, duration_ms=9_999, actual_cost=2.0, currency="CNY"),
    ]

    item = ProviderStatistics(rows, as_of=AS_OF).summary(window="24h", sort="duration_p50")["items"][0]

    assert item["duration_ms"] == {"p50": 100, "p90": 100, "p95": 100, "samples": 1}
    assert item["currency"] is None
    assert item["costs_by_currency"] == {
        "CNY": {"estimated": 0.1, "actual": 2.0},
        "USD": {"estimated": 0.1, "actual": 0.2},
    }


def test_suspicious_cancel_is_not_success_or_failure_but_spend_is_visible():
    rows = [
        attempt(1, winner=1),
        attempt(2, protocol_success=0, product_success=0, terminal_error="rate_limited"),
        attempt(3, protocol_success=0, product_success=0,
                cancellation_class="hedge_cancelled_maybe_billed", estimated_cost=0.7, actual_cost=0.6),
    ]

    item = ProviderStatistics(rows, as_of=AS_OF).summary(window="24h")["items"][0]

    assert item["sample_size"] == 2
    assert item["protocol_success_rate"] == 0.5
    assert item["product_success_rate"] == 0.5
    assert item["suspicious_cancel_count"] == 1
    assert item["suspicious_cancel_estimated_cost"] == 0.7
    assert item["suspicious_cancel_actual_cost"] == 0.6
    assert item["error_counts"] == {"rate_limited": 1}
    assert item["error_rates"] == {"rate_limited": 0.5}


def test_store_audit_is_metadata_only_and_export_is_redacted(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    store.record_provider_audit("provider_invocation_started", {
        "invocation_id": "invocation-1", "stage": "research", "mode": "race", "packet_sha256": "f" * 64,
        "absolute_deadline": 20.0, "route_timeout_seconds": 90.0, "prompt": "invocation private prompt",
    }, recorded_at="2026-08-27T07:00:00Z")
    store.record_provider_audit("llm_attempt_started", {
        "attempt_id": "attempt-1", "invocation_id": "invocation-1", "packet_sha256": "f" * 64,
        "stage": "research", "route_id": "route-a", "endpoint_id": "endpoint-a",
        "model": "gpt-test", "model_family": "openai", "tier": 100, "delayed_start": False,
        "started_at": 10.0, "estimated_cost": 0.2, "cost_mode": "token", "preference": 9,
        "requested_level": "L2", "actual_level": "L3", "upgrade_reason": "L2_CANDIDATES_EXHAUSTED",
        "runner_fingerprint": "provider-broker/responses-sse-v1",
        "prompt": "must never persist", "api_key": "sk-secret",
    }, recorded_at="2026-08-27T07:00:00Z")
    store.record_provider_audit("llm_attempt_finished", {
        "attempt_id": "attempt-1", "first_token_at": 10.1, "completed_at": 10.4,
        "protocol_success": True, "product_success": True, "winner": True,
        "usage": {
            "prompt_tokens": 11, "completion_tokens": 7,
            "prompt_tokens_details": {"cached_tokens": 4},
            "completion_tokens_details": {"reasoning_tokens": 3},
        }, "actual_cost": 0.12,
        "currency": "USD", "response_id": "response-safe", "request_id": "request-safe",
        "verifier": {"name": "evidence/v1", "passed": True, "private": "do-not-store"},
        "result": "private business response",
    }, recorded_at="2026-08-27T07:00:01Z")
    store.record_provider_audit("provider_invocation_finished", {
        "invocation_id": "invocation-1", "winner_route": "route-a", "winner_endpoint": "endpoint-a",
        "winner_model": "gpt-test", "winner_family": "openai", "product_disposition": "qualified",
        "attempt_count": 1, "probe_count": 1, "result": "invocation private result",
    }, recorded_at="2026-08-27T07:00:01Z")

    payload = store.provider_quality(window="24h", as_of=AS_OF)
    exported = store.export_provider_quality("json", window="24h", as_of=AS_OF)
    raw_database = (tmp_path / "companion.sqlite3").read_bytes()

    assert payload["items"][0]["product_success_rate"] == 1.0
    assert json.loads(exported)["contract"] == "provider-quality-export/v1"
    with store.connection() as connection:
        persisted = dict(connection.execute(
            "SELECT cost_mode,preference,cached_input_tokens,reasoning_tokens,requested_level,actual_level,"
            "upgrade_reason,runner_fingerprint "
            "FROM provider_llm_attempt WHERE attempt_id='attempt-1'",
        ).fetchone())
    assert persisted == {
        "cost_mode": "token", "preference": 9, "cached_input_tokens": 4, "reasoning_tokens": 3,
        "requested_level": "L2", "actual_level": "L3", "upgrade_reason": "L2_CANDIDATES_EXHAUSTED",
        "runner_fingerprint": "provider-broker/responses-sse-v1",
    }
    for secret in (b"must never persist", b"sk-secret", b"private business response", b"do-not-store",
                   b"invocation private prompt", b"invocation private result"):
        assert secret not in raw_database
        assert secret.decode() not in exported


def test_probe_details_are_separate_and_compacted_after_90_days(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    store.record_provider_audit("provider_probe_attempt", {
        "invocation_id": "old", "endpoint_id": "endpoint-a", "status": "available",
        "started_at": 1.0, "completed_at": 1.2,
    }, recorded_at="2026-05-01T00:00:00Z")
    store.record_provider_audit("provider_probe_attempt", {
        "invocation_id": "new", "endpoint_id": "endpoint-a", "status": "inconclusive",
        "started_at": 2.0, "completed_at": 2.2,
    }, recorded_at="2026-08-27T00:00:00Z")

    result = store.compact_provider_probes(as_of=AS_OF)

    assert result == {"aggregated": 1, "deleted": 1}
    with store.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM provider_probe_attempt").fetchone()[0] == 1
        daily = dict(connection.execute("SELECT * FROM provider_probe_daily").fetchone())
    assert daily["sample_count"] == 1
    assert daily["status"] == "available"


def test_history_score_is_neutral_until_sample_is_sufficient_and_is_bounded(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    route = {"id": "route-a", "endpoint": "endpoint-a", "model": "gpt-test", "model_family": "openai"}

    assert store.provider_history_score(route, "research") == 0.0
    with store.connection() as connection:
        for index in range(20):
            row = attempt(index + 1, attempt_id=f"history-{index}", recorded_at="2026-08-27T07:00:00Z")
            connection.execute(
                """INSERT INTO provider_llm_attempt(
                   attempt_id,invocation_id,stage,route_id,endpoint_id,model,model_family,tier,recorded_at,
                   protocol_success,product_success) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (row["attempt_id"], "inv", row["stage"], row["route_id"], row["endpoint_id"], row["model"],
                 row["model_family"], row["tier"], row["recorded_at"], 1, 1),
            )

    assert 0 < store.provider_history_score(route, "research") <= 0.05


def test_provider_schema_upgrade_is_idempotent_at_version_16(tmp_path):
    store = CompanionStore(tmp_path / "companion.sqlite3")
    store.initialize()
    store.initialize()

    with store.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 16
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {row[1] for row in connection.execute("PRAGMA table_info(provider_llm_attempt)")}

    assert {"provider_invocation", "provider_llm_attempt", "provider_probe_attempt", "provider_probe_daily"} <= tables
    assert {"cost_mode", "preference", "cached_input_tokens", "reasoning_tokens", "runner_fingerprint",
            "requested_level", "actual_level", "upgrade_reason"} <= columns
