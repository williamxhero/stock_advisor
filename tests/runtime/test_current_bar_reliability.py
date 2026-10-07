"""Offline qualification replays; these are not live dependency acceptance."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from ai_trading_companion.__main__ import flush, run_scheduled_cycle
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.evidence_contract import EvidenceContractFactory
from ai_trading_companion.evidence_gate import EvidenceInsufficient
from ai_trading_companion.exchange import LocalExchange
from ai_trading_companion.local_research import ToolCatalogMarketBackend, _public_failure_message
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.tooling import FactRequest, ToolCatalog, ToolRunner
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
