"""Offline qualification replays; these are not live dependency acceptance."""
from __future__ import annotations

import copy
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ai_trading_companion.__main__ import flush, run_scheduled_cycle
from ai_trading_companion.builtin_tools import ensure_builtin_tools
from ai_trading_companion.broker_client import BrokerResponse
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.evidence_gate import EvidenceInsufficient
from ai_trading_companion.exchange import LocalExchange
from ai_trading_companion.local_research import ToolCatalogMarketBackend, _public_failure_message
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.stage_expression import safe_stage_output
from ai_trading_companion.tooling import FactRequest, ToolCatalog, ToolRunner, validate_capability_data
from ai_trading_companion.tool_failures import RETRY_POLICY, TRANSIENT_ERRORS

AS_OF = "2026-09-03T06:30:00Z"
HOLDINGS = ["600487", "002371", "605296"]
RESOURCES = Path(__file__).resolve().parents[2] / "resources"


def publish_route(root, adapter, code):
    version = root / "cn_equity_current_bar" / "adapters" / adapter / "versions" / "test-v1"
    version.mkdir(parents=True)
    (version / "tool.py").write_text(code, encoding="utf-8")
    (version / "manifest.json").write_text(json.dumps({
        "contract": "ai-trading-tool-manifest/v1", "capability": "cn_equity_current_bar",
        "version": "test-v1", "state": "promoted", "command": [sys.executable, "tool.py"],
    }), encoding="utf-8")
    return version


def route_catalog(root, codes):
    for adapter, code in codes.items():
        publish_route(root, adapter, code)
    (root / "cn_equity_current_bar" / "routing.json").write_text(json.dumps({
        "contract": "ai-trading-tool-routing/v1",
        "candidates": [{"adapter": adapter, "version": "test-v1"} for adapter in codes],
    }), encoding="utf-8")
    return ToolCatalog(root)


def failure_code(error, *, exit_code=75):
    return "import sys, json\nprint(json.dumps(" + repr({
        "contract": "ai-trading-tool-failure/v1", "error_code": error, "message": "controlled failure",
    }) + "), file=sys.stderr)\nsys.exit(" + str(exit_code) + ")\n"


def request(cycle="cycle-1430", deadline=4):
    return FactRequest(1, "cn_equity_current_bar", AS_OF, deadline,
                       {"symbols": HOLDINGS, "freq": "1m"}, context={"cycle_id": cycle}, finality="intraday")


@pytest.mark.parametrize("error", [
    "tool_current_bar_result_invalid", "tool_current_bar_identity_invalid",
    "tool_current_bar_symbol_mismatch", "tool_current_bar_time_invalid",
    "tool_current_bar_stale", "tool_current_bar_values_invalid", "tool_current_bar_finality_invalid",
    "tool_process_configuration", "tool_http_client_error", "tool_http_server_error",
])
def test_typed_deterministic_failure_switches_once_and_shares_failure_evidence(tmp_path, error):
    catalog = route_catalog(tmp_path / "tools", {
        "markethub": failure_code(error), "tencent": failure_code("tool_current_bar_result_invalid"),
    })
    needs = []
    runner = ToolRunner(catalog, need_reporter=needs.append)
    result = runner.resolve_with_fallback(request())
    assert result.error_code == "tool_routes_exhausted_deterministic"
    assert len(result.attempts) == 2
    audit = json.loads((catalog.root / ".audit/resolutions.ndjson").read_text(encoding="utf-8"))
    assert audit["adapter_receipts"] == needs[0]["failure_trace"]["adapter_receipts"]
    assert audit["acquired_at"] == needs[0]["failure_trace"]["acquired_at"]
    assert [row["error_code"] for row in audit["adapter_receipts"]] == [error, "tool_current_bar_result_invalid"]
    for receipt in audit["adapter_receipts"]:
        assert receipt["exit_code"] == 75
        assert receipt["acquired_at"] and receipt["retry_policy"] == RETRY_POLICY
        assert receipt["diagnostic_artifact_ref"].startswith("artifact:sha256:")
        assert runner.read_artifact(receipt["diagnostic_artifact_ref"])
    health = json.loads((catalog.root / ".health/cn_equity_current_bar-markethub-test-v1.json").read_text())
    assert health["degraded"] and health["degrade_reason"] == error
    assert runner.resolve_with_fallback(request()).error_code == "tool_circuit_open"
    assert runner.resolve_with_fallback(request("next-cycle")).attempts != ("markethub:circuit_open", "tencent:circuit_open")


@pytest.mark.parametrize("error", sorted(TRANSIENT_ERRORS))
def test_only_enumerated_network_failures_receive_one_retry(tmp_path, error):
    catalog = route_catalog(tmp_path / "tools", {
        "markethub": failure_code(error), "tencent": failure_code("tool_current_bar_result_invalid"),
    })
    runner = ToolRunner(catalog)
    result = runner.resolve_with_fallback(request())
    assert result.attempts == (f"markethub:{error}", f"markethub:{error}", "tencent:tool_current_bar_result_invalid")
    assert result.error_code == "tool_routes_exhausted"
    assert runner.resolve_with_fallback(request()).error_code == "tool_circuit_open"
    health = json.loads((catalog.root / ".health/cn_equity_current_bar-markethub-test-v1.json").read_text())
    assert health["transient_failures"] == 2 and not health.get("degraded")


def native_bar_output():
    return {"contract": "ai-trading-tool-result/v1", "fact_as_of": AS_OF, "data": {
        "finality": "intraday", "bars": [{
            "symbol": code, "exchange": "SZSE" if code.startswith("0") else "SSE", "market": "CN-A",
            "freq": "1m", "trade_time": "2026-09-03T14:29:00+08:00",
            "interval_start": "2026-09-03T14:29:00+08:00", "interval_end": "2026-09-03T14:30:00+08:00",
            "open": 10, "high": 10.2, "low": 10, "close": 10.2, "volume": 2000, "amount": 20200,
            "is_suspended": False, "is_st": False, "is_final": True, "degraded": False,
            "observed_at": AS_OF, "last_trade_at": "2026-09-03T14:29:00+08:00", "freshness_ms": 0,
            "market_status": "trading", "provider": "controlled-native", "source_semantics": "native",
        } for code in HOLDINGS],
    }}


@pytest.mark.parametrize("case,error", [
    ("valid", None),
    ("non_final", "tool_current_bar_finality_invalid"),
    ("future", "tool_current_bar_after_required_at"),
    ("non_final_bad_identity", "tool_current_bar_finality_invalid"),
    ("future_bad_identity", "tool_current_bar_after_required_at"),
    ("missing_exchange", "tool_current_bar_identity_invalid"),
    ("wrong_market", "tool_current_bar_identity_invalid"),
    ("official_close", "tool_current_bar_finality_invalid"),
])
def test_current_bar_preserves_canonical_markethub_contract(case, error):
    from ai_trading_companion.regression_probes import AS_OF as cutoff, _market_data

    data = _market_data()
    bar = data["bars"][0]
    finality = "close"
    if case.startswith("non_final"):
        bar["is_final"] = False
    elif case.startswith("future"):
        bar["observed_at"] = "2026-09-30T02:00:00Z"
    if case.endswith("bad_identity"):
        bar["exchange"] = "invalid"
    elif case == "missing_exchange":
        bar.pop("exchange")
    elif case == "wrong_market":
        bar["market"] = "US"
    elif case == "official_close":
        finality = data["finality"] = "official_close"
    fact_request = FactRequest(1, "cn_equity_current_bar", cutoff, 1.0,
                               {"symbols": ["000001"], "freq": "1m"}, finality=finality)
    assert validate_capability_data(fact_request, cutoff, data) == error


@pytest.mark.parametrize("field,value,error", [
    ("exchange", "SZSE", "tool_current_bar_identity_invalid"),
    ("volume", float("nan"), "tool_current_bar_values_invalid"),
    ("amount", float("inf"), "tool_current_bar_values_invalid"),
    ("close", True, "tool_current_bar_values_invalid"),
    ("low", 11, "tool_current_bar_values_invalid"),
    ("is_st", None, "tool_current_bar_status_invalid"),
    ("is_suspended", "false", "tool_current_bar_status_invalid"),
    ("market_status", "", "tool_current_bar_status_invalid"),
    ("interval_start", "2026-09-02T14:29:00+08:00", "tool_current_bar_time_invalid"),
    ("interval_end", "2026-09-03T14:31:00+08:00", "tool_current_bar_time_invalid"),
    ("observed_at", "2026-09-03T14:31:00+08:00", "tool_current_bar_after_required_at"),
    ("freshness_ms", True, "tool_current_bar_stale"),
])
def test_current_bar_rejects_invalid_values_identity_time_and_metadata(tmp_path, field, value, error):
    output = native_bar_output()
    output["data"]["bars"][0][field] = value
    code = "import sys\nsys.stdout.write(" + repr(json.dumps(output)) + ")"
    runner = ToolRunner(route_catalog(tmp_path / "tools", {"markethub": code}))
    result = runner.resolve_with_fallback(request())
    assert not result.succeeded and result.adapter_receipts[0]["error_code"] == error
    assert result.adapter_receipts[0]["exit_code"] == 0
    assert len(result.attempts) == 1


@pytest.mark.parametrize("case", ["missing", "duplicate", "wrong_symbol", "stale", "fact_time", "finality"])
def test_current_bar_requires_exact_frozen_coverage_and_truthful_cutoff(tmp_path, case):
    output = native_bar_output()
    bars = output["data"]["bars"]
    if case == "missing":
        bars.pop()
    elif case == "duplicate":
        bars.append(copy.deepcopy(bars[0]))
    elif case == "wrong_symbol":
        bars[0]["symbol"] = "600000"
    elif case == "stale":
        for bar in bars:
            bar.update(interval_start="2026-09-03T14:23:00+08:00", interval_end="2026-09-03T14:24:00+08:00",
                       observed_at="2026-09-03T14:24:00+08:00", last_trade_at="2026-09-03T14:23:00+08:00",
                       freshness_ms=0)
        output["fact_as_of"] = "2026-09-03T06:24:00Z"
    elif case == "fact_time":
        output["fact_as_of"] = "2026-09-03T06:29:00Z"
    else:
        output["data"]["finality"] = "official_close"
    code = "import sys\nsys.stdout.write(" + repr(json.dumps(output)) + ")"
    result = ToolRunner(route_catalog(tmp_path / "tools", {"markethub": code})).resolve_with_fallback(request())
    expected = ("tool_current_bar_stale" if case == "stale" else "tool_current_bar_time_invalid" if case == "fact_time"
                else "tool_current_bar_finality_invalid" if case == "finality" else "tool_current_bar_symbol_mismatch")
    assert result.adapter_receipts[0]["error_code"] == expected and not result.succeeded


def proven_minute_contract():
    """Synthetic input for the explicit contract, not live provider evidence."""
    return {
        "contract": "tencent-minute-bar/v1", "date": "20260903",
        "units": {"price": "CNY/share", "volume": "lots_100", "amount": "CNY"},
        "volume_mode": "cumulative", "interval_semantics": "start_labelled_1m",
        "security_status": {"is_st": False, "is_suspended": False, "market_status": "trading",
                            "as_of": AS_OF, "source_url": "https://fixture.example/security-status"},
        "data": [
            {"time": "1428", "open": 10, "high": 10.1, "low": 9.9, "close": 10,
             "cumulative_volume": 100, "cumulative_amount": 100000, "is_final": True,
             "observed_at": "2026-09-03T14:29:00+08:00", "last_trade_at": "2026-09-03T14:28:50+08:00"},
            {"time": "1429", "open": 10.1, "high": 10.6, "low": 9.8, "close": 10.2,
             "cumulative_volume": 120, "cumulative_amount": 120200, "is_final": True,
             "observed_at": AS_OF, "last_trade_at": "2026-09-03T14:29:50+08:00"},
            {"time": "1430", "open": 10.2, "high": 10.3, "low": 10.2, "close": 10.3,
             "cumulative_volume": 125, "cumulative_amount": 125300, "is_final": False,
             "observed_at": AS_OF, "last_trade_at": "2026-09-03T14:30:00+08:00"},
        ],
    }


@pytest.mark.parametrize("case,error", [
    ("sampled_only", "tool_current_bar_source_invalid"),
    ("missing_symbol", "tool_current_bar_symbol_mismatch"),
    ("wrong_symbol", "tool_current_bar_symbol_mismatch"),
    ("duplicate_key", "tool_current_bar_result_invalid"),
    ("missing_date", "tool_current_bar_time_invalid"),
    ("wrong_date", "tool_current_bar_time_invalid"),
    ("future", "tool_current_bar_time_invalid"),
    ("stale", "tool_current_bar_stale"),
    ("nan", "tool_current_bar_values_invalid"),
    ("negative", "tool_current_bar_values_invalid"),
    ("no_amount", "tool_current_bar_values_invalid"),
    ("decreasing_volume", "tool_current_bar_values_invalid"),
    ("decreasing_amount", "tool_current_bar_values_invalid"),
    ("duplicate_minute", "tool_current_bar_values_invalid"),
    ("nonconsecutive", "tool_current_bar_result_invalid"),
    ("incomplete", "tool_current_bar_result_invalid"),
])
def test_generated_tencent_adapter_never_promotes_unproven_minute_facts(tmp_path, case, error):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/stocks/quotes"):
                self.send_error(503)
                return
            vendor = parse_qs(urlsplit(self.path).query)["code"][0]
            minute = {"date": "20260903", "data": ["1428 10.00 100 1000", "1429 10.20 120 1224"]}
            if case == "missing_date":
                minute.pop("date")
            elif case == "wrong_date":
                minute["date"] = "20260902"
            elif case == "future":
                minute["data"].append("1431 10.30 125 1275")
            elif case == "stale":
                minute["data"] = ["1422 10.00 100 1000", "1423 10.20 120 1224"]
            elif case == "nan":
                minute["data"][1] = "1429 10.20 nan 1224"
            elif case == "negative":
                minute["data"][1] = "1429 10.20 120 -1"
            elif case == "no_amount":
                minute["data"][1] = "1429 10.20 120"
            elif case == "decreasing_volume":
                minute["data"][1] = "1429 10.20 90 1224"
            elif case == "decreasing_amount":
                minute["data"][1] = "1429 10.20 120 900"
            elif case == "duplicate_minute":
                minute["data"][1] = "1428 10.20 120 1224"
            elif case == "nonconsecutive":
                minute["data"][0] = "1427 10.00 100 1000"
            elif case == "incomplete":
                minute["data"].pop()
            by_symbol = {vendor: {"data": minute}}
            if case == "missing_symbol":
                by_symbol = {}
            elif case == "wrong_symbol":
                by_symbol = {"sh600000": {"data": minute}}
            body = json.dumps({"data": by_symbol})
            if case == "duplicate_key":
                body = '{"data":' + json.dumps(by_symbol) + ',"data":' + json.dumps(by_symbol) + '}'
            raw = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    ensure_builtin_tools(tmp_path / "tools")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = ToolRunner(ToolCatalog(tmp_path / "tools")).resolve_with_fallback(FactRequest(
            1, "cn_equity_current_bar", AS_OF, 8, {"symbols": HOLDINGS, "freq": "1m",
                "markethub_url": base + "/stocks/quotes", "tencent_minute_url": base + "/minute?code="},
            context={"cycle_id": "tencent-contract"}, finality="intraday",
        ))
        assert not result.succeeded and result.data is None
        assert result.attempts == ("markethub:tool_http_server_error", f"tencent:{error}")
        assert result.adapter_receipts[-1]["error_code"] == error
        assert result.adapter_receipts[-1]["diagnostic_artifact_ref"]
        if case == "sampled_only":
            assert [parse_qs(urlsplit(path).query)["code"][0] for path in calls[1:]] == ["sh600487", "sz002371", "sh605296"]
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("case,error", [
    ("valid_lots", None), ("valid_shares", None), ("valid_st", None), ("valid_suspension", None), ("valid_no_trade", None),
    ("missing_contract", "tool_current_bar_source_invalid"),
    ("missing_units", "tool_current_bar_source_invalid"),
    ("unknown_units", "tool_current_bar_source_invalid"),
    ("wrong_amount_units", "tool_current_bar_source_invalid"),
    ("not_cumulative", "tool_current_bar_source_invalid"),
    ("unknown_labels", "tool_current_bar_source_invalid"),
    ("missing_ohlc", "tool_current_bar_values_invalid"),
    ("wrong_extrema", "tool_current_bar_values_invalid"),
    ("boolean_ohlc", "tool_current_bar_values_invalid"),
    ("infinite_ohlc", "tool_current_bar_values_invalid"),
    ("boolean_volume", "tool_current_bar_values_invalid"),
    ("units_contradict_amount", "tool_current_bar_values_invalid"),
    ("wrong_last_trade", "tool_current_bar_values_invalid"),
    ("future_observed", "tool_current_bar_time_invalid"),
    ("naive_observed", "tool_current_bar_time_invalid"),
    ("missing_status", "tool_current_bar_status_invalid"),
    ("non_boolean_status", "tool_current_bar_status_invalid"),
    ("suspension_contradiction", "tool_current_bar_status_invalid"),
    ("stale_status", "tool_current_bar_time_invalid"),
    ("missing_status_source", "tool_current_bar_source_invalid"),
    ("non_final", "tool_current_bar_finality_invalid"),
    ("premature_final", "tool_current_bar_time_invalid"),
    ("false_official_close", "tool_current_bar_finality_invalid"),
])
def test_generated_tencent_adapter_requires_proven_bar_contract(tmp_path, case, error):
    minute = proven_minute_contract()
    selected = minute["data"][1]
    if case == "valid_shares":
        minute["units"]["volume"] = "shares"
        for row in minute["data"]:
            row["cumulative_volume"] *= 100
    elif case == "valid_st":
        minute["security_status"]["is_st"] = True
    elif case in {"valid_suspension", "valid_no_trade"}:
        if case == "valid_suspension":
            minute["security_status"].update(is_suspended=True, market_status="suspended")
        for row in minute["data"]:
            row.update(cumulative_volume=100, cumulative_amount=100000, open=10, high=10, low=10, close=10,
                       last_trade_at="2026-09-03T14:28:50+08:00")
    elif case == "missing_contract":
        minute.pop("contract")
    elif case == "missing_units":
        minute.pop("units")
    elif case == "unknown_units":
        minute["units"]["volume"] = "lots"
    elif case == "wrong_amount_units":
        minute["units"]["amount"] = "wan_CNY"
    elif case == "not_cumulative":
        minute["volume_mode"] = "interval"
    elif case == "unknown_labels":
        minute["interval_semantics"] = "end_labelled_1m"
    elif case == "missing_ohlc":
        selected.pop("high")
    elif case == "wrong_extrema":
        selected["high"] = 10
    elif case == "boolean_ohlc":
        selected["open"] = True
    elif case == "infinite_ohlc":
        selected["high"] = float("inf")
    elif case == "boolean_volume":
        selected["cumulative_volume"] = True
    elif case == "units_contradict_amount":
        minute["units"]["volume"] = "shares"
    elif case == "wrong_last_trade":
        selected["last_trade_at"] = "2026-09-03T14:28:50+08:00"
    elif case == "future_observed":
        selected["observed_at"] = "2026-09-03T14:31:00+08:00"
    elif case == "naive_observed":
        selected["observed_at"] = "2026-09-03T14:30:00"
    elif case == "missing_status":
        minute.pop("security_status")
    elif case == "non_boolean_status":
        minute["security_status"]["is_st"] = "false"
    elif case == "suspension_contradiction":
        minute["security_status"].update(is_suspended=True, market_status="suspended")
    elif case == "stale_status":
        minute["security_status"]["as_of"] = "2026-09-03T14:29:00+08:00"
    elif case == "missing_status_source":
        minute["security_status"].pop("source_url")
    elif case == "non_final":
        selected["is_final"] = False
    elif case == "premature_final":
        minute["data"][2]["is_final"] = True

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/stocks/quotes"):
                self.send_error(503)
                return
            vendor = parse_qs(urlsplit(self.path).query)["code"][0]
            raw = json.dumps({"data": {vendor: {"data": minute}}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    ensure_builtin_tools(tmp_path / "tools")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        runner = ToolRunner(ToolCatalog(tmp_path / "tools"))
        result = runner.resolve_with_fallback(FactRequest(
            1, "cn_equity_current_bar", AS_OF, 8, {"symbols": HOLDINGS, "freq": "1m",
                "markethub_url": base + "/stocks/quotes", "tencent_minute_url": base + "/minute?code="},
            finality="official_close" if case == "false_official_close" else "intraday",
        ))
        if error:
            assert not result.succeeded and result.data is None
            assert result.attempts == ("markethub:tool_http_server_error", f"tencent:{error}")
            assert result.adapter_receipts[-1]["error_code"] == error
            return
        assert result.succeeded, result.error_code
        assert result.attempts == ("markethub:tool_http_server_error", "tencent:succeeded")
        assert {bar["symbol"] for bar in result.data["bars"]} == set(HOLDINGS)
        assert result.fact_as_of == AS_OF
        for bar in result.data["bars"]:
            no_trade = case in {"valid_suspension", "valid_no_trade"}
            assert (bar["open"], bar["high"], bar["low"], bar["close"]) == ((10, 10, 10, 10) if no_trade else (10.1, 10.6, 9.8, 10.2))
            assert (bar["volume"], bar["amount"]) == ((0, 0) if no_trade else (2000, 20200))
            assert bar["is_st"] == (case == "valid_st")
            assert bar["is_suspended"] == (case == "valid_suspension")
            assert bar["is_final"] and bar["degraded"] and bar["source_semantics"] == "derived"
            assert bar["interval_end"] == "2026-09-03T14:30:00+08:00"
        assert len(result.data["source_evidence"]) == len(HOLDINGS)
        archived = json.loads(runner.read_artifact(result.raw_artifact_ref))
        assert archived["data"]["source_evidence"][0]["raw_minute_contract"] == minute
    finally:
        server.shutdown()
        server.server_close()


def test_transient_retry_recovers_with_truthful_history_and_cache(tmp_path):
    code = "from pathlib import Path\nmarker = Path('called')\nif not marker.exists():\n    marker.touch()\n" + "\n".join(
        "    " + line for line in failure_code("tool_network_connection_reset").splitlines()
    ) + "\nimport json\nprint(json.dumps(" + repr(native_bar_output()) + "))"
    runner = ToolRunner(route_catalog(tmp_path / "tools", {"markethub": code}))
    req = FactRequest(1, "cn_equity_current_bar", AS_OF, 4, {"symbols": HOLDINGS, "freq": "1m"},
                      context={"cycle_id": "retry-success"}, finality="intraday", freshness_seconds=60)
    result = runner.resolve_with_fallback(req)
    assert result.succeeded and result.attempts == ("markethub:tool_network_connection_reset", "markethub:succeeded")
    assert len(result.adapter_receipts) == 2
    assert runner.resolve_with_fallback(req).attempts == ("cache:succeeded",)


def test_process_timeout_reserves_an_independent_fallback_budget(tmp_path):
    runner = ToolRunner(route_catalog(tmp_path / "tools", {
        "markethub": "import time; time.sleep(60)",
        "tencent": "import json; print(json.dumps(" + repr(native_bar_output()) + "))",
    }))
    started = time.monotonic()
    result = runner.resolve_with_fallback(request(deadline=2))
    assert result.succeeded and result.attempts == ("markethub:tool_timeout", "tencent:succeeded")
    assert time.monotonic() - started < 3
    assert result.adapter_receipts[0]["exit_code"] is not None


def test_legacy_exit_75_and_network_prose_never_authorize_retry(tmp_path):
    catalog = route_catalog(tmp_path / "tools", {
        "markethub": "import sys; print('network read failed after retry', file=sys.stderr); sys.exit(75)",
    })
    result = ToolRunner(catalog).resolve_with_fallback(request())
    assert result.attempts == ("markethub:tool_process_failed",)


def test_secret_diagnostics_and_inputs_are_not_archived_or_reported(tmp_path):
    secret = "sk-" + "x" * 48
    catalog = route_catalog(tmp_path / "tools", {"markethub": f"import sys; print({secret!r}, file=sys.stderr); sys.exit(75)"})
    needs = []
    runner = ToolRunner(catalog, need_reporter=needs.append)
    result = runner.resolve_with_fallback(request())
    assert result.adapter_receipts[0]["error_code"] == "tool_secret_rejected"
    assert not list((catalog.root / ".artifacts").glob("*.gz"))
    before = len(needs)
    assert runner.resolve_with_fallback(FactRequest(1, "cn_equity_current_bar", AS_OF, 1,
        {"symbols": HOLDINGS, "api_key": secret})).error_code == "tool_secret_rejected"
    assert len(needs) == before
    assert secret not in (catalog.root / ".audit/resolutions.ndjson").read_text()


def test_current_bar_public_fault_names_only_attempted_routes():
    gap = {"blocking": True, "coverage_state": "missing", "requirement_key": "portfolio_current_bar",
           "target_proposition": "实有持仓当前行情", "attempted_routes": ["markethub"]}
    text = _public_failure_message(AS_OF, [gap])
    assert "主行情服务" in text and "独立分钟数据" not in text
    assert "实有持仓当前行情" in text and "交易动作" in text
    gap["attempted_routes"].append("tencent")
    assert "独立分钟数据回退" in _public_failure_message(AS_OF, [gap])


def seed_portfolio(store):
    portfolio = PortfolioService(store)
    names = dict(zip(HOLDINGS, ["亨通光电", "北方华创", "神农集团"]))
    changes = [{
        "action": "position_correction", "code": code, "name": names[code], "shares": 100,
        "price": 10, "average_cost": 10, "occurred_at": "2026-09-03T06:00:00Z",
        "evidence": {"instrument": code + " " + names[code], "action": "持有",
                     "shares": "100股", "price": "10", "average_cost": "10", "total_assets": None},
    } for code in HOLDINGS]
    result = portfolio.replace_complete_snapshot(
        "这是全部持仓：" + "；".join(code + " " + names[code] + " 100股，成本10" for code in HOLDINGS),
        changes, "replay", "replay-portfolio",
    )
    assert result["state"] == "applied"
    return portfolio


def controlled_non_bar_result(operation, contract):
    as_of = contract["as_of"]
    data = {"summary": "公告、政策及风险核验：本轮市场信息已核对", "finality": "intraday"}
    if operation == "holding_snapshot":
        data["quotes"] = [{"symbol": code, "price": 10, "previous_close": 9.9,
                           "change": 0.1, "change_percent": 1.01, "quote_at": as_of,
                           "trading_date": as_of[:10], "status": "trading"} for code in HOLDINGS]
    elif operation == "market_breadth":
        data["breadth"] = {"up": 3000, "down": 1800, "flat": 200}
    elif operation == "market_snapshot":
        data["indices"] = [{"symbol": "000001", "price": 3800, "change_percent": 0.5}]
    elif operation == "announcement_snapshot":
        data["announcements"] = [{"symbol": code, "issuer": code, "title": "经营信息公告",
            "published_at": as_of, "announcement_date": as_of[:10],
            "source_url": f"https://www.cninfo.com.cn/{code}/notice", "content_verified": True,
            "content": code + "经营情况与风险披露"} for code in HOLDINGS]
    return {"results": [{"url": "https://www.cninfo.com.cn/replay/" + operation,
        "title": "controlled non-Bar observation", "excerpt_text": json.dumps(data, ensure_ascii=False),
        "fact_as_of": as_of, "primary": True}]}


class Weekdays:
    def is_trading_day(self, day):
        return day.weekday() < 5


def test_scheduled_proven_tencent_fallback_qualifies_and_reaches_exchange(tmp_path):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/stocks/quotes"):
                self.send_error(503)
                return
            vendor = parse_qs(urlsplit(self.path).query)["code"][0]
            minute = proven_minute_contract()
            # A longer tape must remain archived without truncating the
            # qualification excerpt into invalid JSON at the 8KB boundary.
            minute["data"] = [{**minute["data"][0], "time": f"09{value:02d}",
                "cumulative_volume": value - 29, "cumulative_amount": (value - 29) * 1000,
                "observed_at": f"2026-09-03T{'10:00' if value == 59 else f'09:{value + 1:02d}'}:00+08:00",
                "last_trade_at": f"2026-09-03T09:{value:02d}:50+08:00",
            } for value in range(30, 60)] + minute["data"]
            raw = json.dumps({"data": {vendor: {"data": minute}}}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    ensure_builtin_tools(tmp_path / "tools")
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    engine = CompanionEngine(store, memory=InMemoryMemoryAdapter(),
                             evidence_contract_factory=EvidenceContractFactory(Weekdays()))
    portfolio = seed_portfolio(store)
    exchange = LocalExchange(tmp_path / "exchange")
    cycle = engine.start_cycle("daily.execution.1430", "2026-09-03T14:30:00+08:00", AS_OF)
    paths = SimpleNamespace(home=tmp_path, tools=tmp_path / "tools", runtime=tmp_path / "runtime",
                            resources=RESOURCES, exchange=exchange.root)
    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    runner = ToolRunner(ToolCatalog(paths.tools), need_reporter=store.submit_capability_need)
    resolve = runner.resolve_with_fallback
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    def local_resolve(request):
        return resolve(replace(request, inputs={**request.inputs, "markethub_url": base + "/stocks/quotes",
                                               "tencent_minute_url": base + "/minute?code="}))
    runner.resolve_with_fallback = local_resolve
    original_backend = ToolCatalogMarketBackend.__call__
    def backend(self, operation, arguments):
        if operation != "current_bar":
            result = controlled_non_bar_result(operation, self.contract)
            if operation == "announcement_snapshot":
                source = result["results"][0]
                data = json.loads(source["excerpt_text"])
                result["results"] = [{**source, "url": f"https://www.cninfo.com.cn/{row['symbol']}/notice",
                    "excerpt_text": json.dumps({**data, "checked_symbol": row["symbol"], "announcements": [row]}, ensure_ascii=False)
                } for row in data["announcements"]]
            return result
        return original_backend(self, operation, arguments)
    planner = Mock()
    planner.outcomes = []
    planner.side_effect = AssertionError("qualified deterministic evidence needs no model repair")
    broker = Mock()
    def compose(request):
        assert request.stage == "m0_compose"
        output = safe_stage_output(request.stage, packet=request.packet)
        return BrokerResponse(json.dumps(output), output, "offline-fixture", "fixture", request.intellect,
                              request.intellect, "offline-fixture", request.effort)
    broker.invoke.side_effect = compose
    try:
        with patch("ai_trading_companion.engine.utc_now", return_value=datetime.fromisoformat(AS_OF)), \
             patch("ai_trading_companion.__main__.PATHS", paths), \
             patch("ai_trading_companion.__main__.load_settings", return_value=settings), \
             patch("ai_trading_companion.__main__.broker_client", return_value=broker), \
             patch("ai_trading_companion.__main__.BrokerResearchPlanner", return_value=planner), \
             patch("ai_trading_companion.__main__.ToolRunner", return_value=runner), \
             patch.object(ToolCatalogMarketBackend, "__call__", backend):
            result = run_scheduled_cycle(engine, store, exchange, portfolio, cycle["cycle_id"], True,
                                         at=datetime(2026, 9, 3, 6, 30, tzinfo=timezone.utc))
            flush(store, exchange)
            flush(store, exchange)
        assert result["state"] == "awaiting_h0"
        attempts = store.attempts(cycle["cycle_id"])
        assert [row["status"] for row in attempts] == ["succeeded", "succeeded"]
        verifier = json.loads(attempts[0]["verifier_json"])
        assert verifier["passed"] and "portfolio_current_bar" not in verifier.get("missing_requirements", [])
        assert len(calls) == 4
        frozen_contract = json.loads(attempts[0]["input_packet_json"])["evidence_contract"]
        frozen = next(row for row in frozen_contract["requirements"] if row["key"] == "portfolio_current_bar")
        assert set(frozen["required_entities"]) == set(HOLDINGS)
        assert [parse_qs(urlsplit(path).query)["code"][0] for path in calls[1:]] == [
            ("sz" if code.startswith("0") else "sh") + code for code in frozen["required_entities"]
        ]
        events = [json.loads(path.read_text(encoding="utf-8")) for path in (exchange.root / "to-client/pending").glob("*.json")]
        assert sum((event.get("type") or event.get("event_type")) == "m0.ready" for event in events) == 1
        assert not any((event.get("type") or event.get("event_type")) in {"research.failed", "m1.ready"} for event in events)
        audit = json.loads((paths.tools / ".audit/resolutions.ndjson").read_text())
        assert audit["attempts"] == ["markethub:tool_http_server_error", "tencent:succeeded"]
        assert len(audit["adapter_receipts"]) == 2
        archived = json.loads(runner.read_artifact(audit["raw_artifact_ref"]))
        for source in archived["data"]["source_evidence"]:
            assert len(source["raw_minute_contract"]["data"]) == 33
            assert len(json.dumps(source["data"], sort_keys=True)) < 8000
    finally:
        server.shutdown()
        server.server_close()


def test_scheduled_all_routes_failed_reaches_exchange_once_without_a_judgment(tmp_path):
    catalog = route_catalog(tmp_path / "tools", {
        "markethub": failure_code("tool_current_bar_result_invalid"),
        "tencent": failure_code("tool_current_bar_time_invalid"),
    })
    store = CompanionStore(tmp_path / "runtime.sqlite3")
    memory = InMemoryMemoryAdapter()
    engine = CompanionEngine(store, memory=memory,
                             evidence_contract_factory=EvidenceContractFactory(Weekdays()))
    portfolio = seed_portfolio(store)
    exchange = LocalExchange(tmp_path / "exchange")
    cycle = engine.start_cycle("daily.execution.1430", "2026-09-03T14:30:00+08:00", AS_OF)
    paths = SimpleNamespace(home=tmp_path, tools=catalog.root, runtime=tmp_path / "runtime",
                            resources=RESOURCES, exchange=exchange.root)
    settings = SimpleNamespace(research={}, broker={"url": "http://broker.test:8817"})
    planner = Mock()
    planner.outcomes = []
    planner.side_effect = AssertionError("an exhausted current-Bar blocker must stop before model repair")
    runner = ToolRunner(catalog, need_reporter=store.submit_capability_need)
    # Non-Bar observations are controlled, while both Bar processes and the
    # complete scheduled EvidenceContract run through the real qualification seam.
    original_backend = ToolCatalogMarketBackend.__call__
    def backend(self, operation, arguments):
        if operation != "current_bar":
            return controlled_non_bar_result(operation, self.contract)
        return original_backend(self, operation, arguments)
    started = time.monotonic()
    with patch("ai_trading_companion.engine.utc_now", return_value=datetime.fromisoformat(AS_OF)), \
         patch("ai_trading_companion.__main__.PATHS", paths), \
         patch("ai_trading_companion.__main__.load_settings", return_value=settings), \
         patch("ai_trading_companion.__main__.broker_client", return_value=Mock()), \
         patch("ai_trading_companion.__main__.BrokerResearchPlanner", return_value=planner), \
         patch("ai_trading_companion.__main__.ToolRunner", return_value=runner), \
         patch.object(ToolCatalogMarketBackend, "__call__", backend):
        with pytest.raises(EvidenceInsufficient):
            run_scheduled_cycle(engine, store, exchange, portfolio, cycle["cycle_id"], True,
                                at=datetime(2026, 9, 3, 6, 30, tzinfo=timezone.utc))
        flush(store, exchange)
    assert time.monotonic() - started < 10
    assert store.get_cycle(cycle["cycle_id"])["state"] == "failed"
    attempts = store.attempts(cycle["cycle_id"])
    assert len(attempts) == 1 and attempts[0]["status"] == "failed"
    contract = json.loads(attempts[0]["input_packet_json"])["evidence_contract"]
    frozen = next(row for row in contract["requirements"] if row["key"] == "portfolio_current_bar")
    assert set(frozen["required_entities"]) == set(HOLDINGS)
    assert frozen["window"]["end"] == AS_OF
    verifier = json.loads(attempts[0]["verifier_json"])
    assert "portfolio_current_bar" in verifier["missing_requirements"]
    assert verifier["stop_reason"] == "current_bar_routes_exhausted"
    artifacts = store.artifacts(cycle["cycle_id"])
    assert [row["kind"] for row in artifacts].count("system_fault") == 1
    assert not any(row["kind"] in {"m0", "m1"} for row in artifacts)
    events = [json.loads(path.read_text(encoding="utf-8")) for path in (exchange.root / "to-client/pending").glob("*.json")]
    failures = [event for event in events if event.get("type") == "research.failed" or event.get("event_type") == "research.failed"]
    assert len(failures) == 1
    text = failures[0]["payload"]["message"]["text_projection"]
    assert "实有持仓当前行情" in text and "交易动作" in text
    assert "独立分钟数据" in text and "盘中观察" in text
    assert not any(event.get("type") in {"m0.ready", "m1.ready"} for event in events)
    assert len(json.loads((catalog.root / ".audit/resolutions.ndjson").read_text())["adapter_receipts"]) == 2
