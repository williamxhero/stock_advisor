from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ai_trading_companion.builtin_tools import ensure_builtin_tools
from ai_trading_companion.tooling import FactRequest, ToolCatalog, ToolRunner


class ToolRunnerTests(unittest.TestCase):
    def test_builtin_upgrade_promotes_only_a_previous_builtin_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            previous = root / "generic_web_search" / "current.json"
            previous.parent.mkdir(parents=True)
            previous.write_text(json.dumps({
                "contract": "ai-trading-tool-current/v1", "version": "1.1.0",
            }), encoding="utf-8")
            custom = root / "generic_web_read" / "current.json"
            custom.parent.mkdir(parents=True)
            custom.write_text(json.dumps({
                "contract": "ai-trading-tool-current/v1", "version": "custom-1",
            }), encoding="utf-8")
            turnover_routing = root / "cn_market_turnover_compare" / "routing.json"
            turnover_routing.parent.mkdir(parents=True)
            turnover_routing.write_text(json.dumps({
                "contract": "ai-trading-tool-routing/v1",
                "candidates": [
                    {"adapter": "eastmoney", "version": "1.1.9"},
                    {"adapter": "markethub", "version": "1.1.9"},
                ],
            }), encoding="utf-8")

            ensure_builtin_tools(root)

            self.assertEqual("1.1.13", json.loads(previous.read_text(encoding="utf-8"))["version"])
            self.assertEqual("custom-1", json.loads(custom.read_text(encoding="utf-8"))["version"])
            routing = json.loads(turnover_routing.read_text(encoding="utf-8"))
            self.assertEqual(
                ["eastmoney_history", "official_exchanges", "eastmoney_spot_markethub", "tencent_spot_markethub"],
                [row["adapter"] for row in routing["candidates"]],
            )
            official_manifest = json.loads((
                root / "cn_market_turnover_compare" / "adapters" / "official_exchanges"
                / "versions" / "1.1.13" / "manifest.json"
            ).read_text(encoding="utf-8"))
            self.assertEqual({
                "allowed_domains": ["query.sse.com.cn", "www.szse.cn"],
            }, official_manifest["egress"])

    def publish_tool(self, root: Path, capability: str, script: str, *, state: str = "promoted") -> Path:
        version_root = root / capability / "versions" / "1.0.0"
        version_root.mkdir(parents=True)
        entry = version_root / "tool.py"
        entry.write_text(textwrap.dedent(script), encoding="utf-8")
        (version_root / "manifest.json").write_text(json.dumps({
            "contract": "ai-trading-tool-manifest/v1",
            "capability": capability,
            "version": "1.0.0",
            "state": state,
            "command": [sys.executable, "tool.py"],
        }), encoding="utf-8")
        (root / capability / "current.json").write_text(json.dumps({
            "contract": "ai-trading-tool-current/v1", "version": "1.0.0",
        }), encoding="utf-8")
        return version_root

    def request(self, capability: str = "cn_equity_identity", *, context: dict | None = None) -> FactRequest:
        return FactRequest(
            contract_version=1,
            capability=capability,
            required_at="2026-09-01T01:30:00Z",
            deadline_seconds=2.0,
            inputs={"symbols": ["600000"]},
            context=context or {},
        )

    def test_resolves_a_published_capability_and_preserves_raw_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", """
                import json, sys
                request = json.load(sys.stdin)
                assert request["capability"] == "cn_equity_identity"
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1",
                    "fact_as_of": "2026-09-01T01:30:00Z",
                    "data": {"symbol": request["inputs"]["symbols"][0], "exchange": "SSE"},
                }))
            """)

            runner = ToolRunner(ToolCatalog(root))
            result = runner.resolve(self.request())

            self.assertTrue(result.succeeded, result.error_code)
            self.assertEqual("1.0.0", result.tool_version)
            self.assertEqual("SSE", result.data["exchange"])
            self.assertEqual("2026-09-01T01:30:00Z", result.fact_as_of)
            self.assertTrue(result.acquired_at.endswith("Z"))
            self.assertTrue(result.raw_artifact_ref.startswith("artifact:sha256:"))
            self.assertIn("tool_result_schema_valid", result.technical_validation)
            self.assertIn(b'"exchange": "SSE"', runner.read_artifact(result.raw_artifact_ref))
            self.assertEqual([], list((root / ".runs").glob("*")))

    def test_returns_a_deterministic_error_for_non_json_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "print('diagnostic on stdout')")

            result = ToolRunner(ToolCatalog(root)).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_stdout_invalid_json", result.error_code)

    def test_returns_a_deterministic_error_for_oversized_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "print('x' * 5000)")

            result = ToolRunner(ToolCatalog(root), max_stdout_bytes=128).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_stdout_too_large", result.error_code)

    def test_returns_a_deterministic_error_for_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "import sys; print('failure', file=sys.stderr); sys.exit(7)")

            result = ToolRunner(ToolCatalog(root)).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_process_failed", result.error_code)
            self.assertEqual(7, result.exit_code)

    def test_refuses_a_current_version_that_is_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "print('{}')", state="candidate")

            result = ToolRunner(ToolCatalog(root)).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_not_published", result.error_code)

    def test_times_out_and_cleans_its_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", """
                import time
                time.sleep(5)
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(
                FactRequest(
                    contract_version=1,
                    capability="cn_equity_identity",
                    required_at="2026-09-01T01:30:00Z",
                    deadline_seconds=0.05,
                    inputs={},
                )
            )

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_timeout", result.error_code)
            self.assertEqual([], list((root / ".runs").glob("*")))

    def test_passes_ordinary_context_and_deduplicates_compressed_raw_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", """
                import json, sys
                request = json.load(sys.stdin)
                assert request["context"]["portfolio"]["shares"] == 300
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1",
                    "fact_as_of": "2026-09-01T01:30:00Z",
                    "data": {"identity": "600000.SSE"},
                }, sort_keys=True))
            """)
            runner = ToolRunner(ToolCatalog(root))

            first = runner.resolve(self.request(context={
                "portfolio": {"shares": 300, "cost": 12.5}, "message": "review current holding",
            }))
            second = runner.resolve(self.request(context={
                "portfolio": {"shares": 300, "cost": 12.5}, "message": "review current holding",
            }))

            self.assertTrue(first.succeeded)
            self.assertEqual(first.raw_artifact_ref, second.raw_artifact_ref)
            self.assertEqual(1, len(list((root / ".artifacts").glob("*.gz"))))
            self.assertIn(b'"identity": "600000.SSE"', runner.read_artifact(first.raw_artifact_ref))

    def test_rejects_secrets_before_starting_or_archiving_a_tool(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            version_root = self.publish_tool(root, "cn_equity_identity", """
                from pathlib import Path
                Path("started.txt").write_text("started", encoding="utf-8")
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(self.request(context={
                "note": "token: 1234567890abcdef",
            }))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_secret_rejected", result.error_code)
            self.assertFalse((version_root / "started.txt").exists())
            self.assertFalse((root / ".artifacts").exists())
            self.assertNotIn("1234567890abcdef", str(result))

    def test_refuses_new_calls_after_archive_capacity_is_reached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            version_root = self.publish_tool(root, "cn_equity_identity", """
                from pathlib import Path
                Path("started.txt").write_text("started", encoding="utf-8")
            """)

            result = ToolRunner(ToolCatalog(root), archive_max_bytes=0).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_archive_capacity_exceeded", result.error_code)
            self.assertFalse((version_root / "started.txt").exists())

    def test_archives_nonsecret_failure_output_for_auditing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", """
                import sys
                print("source returned no result")
                print("upstream timeout", file=sys.stderr)
                sys.exit(3)
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(self.request())

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_process_failed", result.error_code)
            self.assertIsNotNone(result.raw_artifact_ref)
            self.assertIsNotNone(result.diagnostic_artifact_ref)
            reader = ToolRunner(ToolCatalog(root))
            self.assertIn(b"source returned no result", reader.read_artifact(result.raw_artifact_ref))

    def test_archives_partial_timeout_output_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", """
                import sys, time
                print("source still working", flush=True)
                print("diagnostic pending", file=sys.stderr, flush=True)
                time.sleep(5)
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                contract_version=1,
                capability="cn_equity_identity",
                required_at="2026-09-01T01:30:00Z",
                deadline_seconds=0.05,
                inputs={},
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_timeout", result.error_code)
            self.assertIsNotNone(result.raw_artifact_ref)
            self.assertIsNotNone(result.diagnostic_artifact_ref)
            reader = ToolRunner(ToolCatalog(root))
            self.assertIn(b"source still working", reader.read_artifact(result.raw_artifact_ref))

    def test_fallback_resolver_retries_a_transient_failure_then_uses_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "import sys; sys.exit(75)")
            backup = root / "cn_equity_identity" / "adapters" / "backup" / "versions" / "1.0.0"
            backup.mkdir(parents=True)
            (backup / "tool.py").write_text(textwrap.dedent("""
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1",
                    "fact_as_of": "2026-09-01T01:30:00Z",
                    "data": {"symbol": "600000", "source": "backup"},
                }))
            """), encoding="utf-8")
            (backup / "manifest.json").write_text(json.dumps({
                "contract": "ai-trading-tool-manifest/v1", "capability": "cn_equity_identity",
                "version": "1.0.0", "state": "promoted", "command": [sys.executable, "tool.py"],
            }), encoding="utf-8")
            (root / "cn_equity_identity" / "routing.json").write_text(json.dumps({
                "contract": "ai-trading-tool-routing/v1",
                "candidates": [{"adapter": "default", "version": "1.0.0"}, {"adapter": "backup", "version": "1.0.0"}],
            }), encoding="utf-8")

            runner = ToolRunner(ToolCatalog(root))
            result = runner.resolve_with_fallback(self.request())

            self.assertTrue(result.succeeded, result.error_code)
            self.assertEqual("backup", result.data["source"])
            self.assertEqual(["default:tool_process_failed", "backup:succeeded"], list(result.attempts))
            self.assertTrue((root / ".audit" / "resolutions.ndjson").exists())

    def test_deterministic_route_failure_is_audited_and_circuit_broken_per_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_identity", "import sys; print('invalid contract shape', file=sys.stderr); sys.exit(64)")
            runner = ToolRunner(ToolCatalog(root))
            request = self.request(context={"cycle_id": "scheduled-1430"})

            first = runner.resolve_with_fallback(request)
            second = runner.resolve_with_fallback(request)

            self.assertEqual("tool_routes_exhausted_deterministic", first.error_code)
            self.assertEqual("tool_circuit_open", second.error_code)
            audit = [json.loads(line) for line in (root / ".audit" / "resolutions.ndjson").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(64, audit[0]["exit_code"])
            self.assertTrue(audit[0]["diagnostic_artifact_ref"].startswith("artifact:sha256:"))
            self.assertEqual({"adapter": "default", "version": "1.0.0"}, audit[0]["route"])
            self.assertTrue((root / ".health" / "cn_equity_identity-default-1.0.0.json").exists())

    def test_fallback_cache_requires_the_same_fact_time_and_finality(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            version_root = self.publish_tool(root, "cn_equity_identity", """
                import json
                from pathlib import Path
                calls = Path("calls.txt")
                calls.write_text(calls.read_text() + "x" if calls.exists() else "x", encoding="utf-8")
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1",
                    "fact_as_of": "2026-09-01T01:30:00Z",
                    "data": {"symbol": "600000"},
                }))
            """)
            runner = ToolRunner(ToolCatalog(root))
            request = FactRequest(1, "cn_equity_identity", "2026-09-01T01:30:00Z", 2.0, {}, freshness_seconds=60.0, finality="official_close")

            first = runner.resolve_with_fallback(request)
            second = runner.resolve_with_fallback(request)
            changed_time = runner.resolve_with_fallback(FactRequest(
                1, "cn_equity_identity", "2026-09-01T01:31:00Z", 2.0, {}, freshness_seconds=60.0, finality="official_close",
            ))

            self.assertTrue(first.succeeded and second.succeeded and changed_time.succeeded)
            self.assertEqual("xx", (version_root / "calls.txt").read_text(encoding="utf-8"))

    def test_exhausted_tool_resolution_reports_a_nonblocking_capability_need(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reported: list[dict] = []
            runner = ToolRunner(ToolCatalog(Path(directory) / "tools"), need_reporter=reported.append)

            result = runner.resolve_with_fallback(FactRequest(
                1, "missing_public_fact", "2026-09-01T07:01:00Z", 2.0,
                {"symbol": "600000"}, context={"capability_need_urgency": "high"},
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_not_found", result.error_code)
            self.assertEqual(1, len(reported))
            self.assertEqual("ai-trading-capability-need/v1", reported[0]["contract"])
            self.assertEqual("missing_public_fact", reported[0]["capability"])
            self.assertEqual("high", reported[0]["urgency"])

    def test_builtin_generic_http_and_web_capabilities_are_read_only_cli_tools(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/api/cninfo/search"):
                    body = b'{"announcements":[{"title":"disclosure","published_at":"2026-09-01T01:00:00Z"}]}'
                elif self.path.startswith("/api/articles/range"):
                    body = b'{"articles":[{"title":"market report","published_at":"2026-09-01T01:00:00Z"}]}'
                else:
                    body = b'{"market":"open","items":[1,2]}' if self.path == "/json" else b"<html><title>Market</title><body>market breadth 1234</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if self.path == "/json" else "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                runner = ToolRunner(ToolCatalog(root))
                json_result = runner.resolve(FactRequest(1, "generic_http_json", "2026-09-01T01:30:00Z", 2.0, {"url": f"{base}/json"}))
                web_result = runner.resolve(FactRequest(1, "generic_web_read", "2026-09-01T01:30:00Z", 2.0, {"url": f"{base}/page"}))
                capture_result = runner.resolve(FactRequest(1, "generic_browser_capture", "2026-09-01T01:30:00Z", 8.0, {"url": f"{base}/page"}))
                disclosures = runner.resolve(FactRequest(1, "cninfo_search", "2026-09-01T01:30:00Z", 2.0, {"base_url": base, "q": "600000"}))
                articles = runner.resolve(FactRequest(1, "article_range", "2026-09-01T01:30:00Z", 2.0, {
                    "base_url": base, "source": "cninfo_disclosure", "start_date": "2026-08-31", "end_date": "2026-09-01",
                }))

                self.assertEqual("open", json_result.data["json"]["market"])
                self.assertIn("market breadth 1234", web_result.data["text"])
                self.assertEqual("dynamic", capture_result.data["capture_mode"])
                self.assertEqual("disclosure", disclosures.data["announcements"][0]["title"])
                self.assertEqual("market report", articles.data["articles"][0]["title"])
                self.assertTrue(json_result.raw_artifact_ref)
            finally:
                server.shutdown()
                server.server_close()

    def test_builtin_tools_refuse_login_and_credential_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)

            result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                1, "generic_web_read", "2026-09-01T01:30:00Z", 2.0,
                {"url": "https://example.test/login"},
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_access_restricted", result.error_code)

    def test_builtin_web_search_uses_searxng_json_results(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({"results": [{
                    "url": "https://example.test/market-news",
                    "title": "Market <b>news</b>",
                    "content": "Policy &amp; risk update",
                }]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "generic_web_search", "2026-09-01T01:30:00Z", 3.0,
                    {"query": "A-share policy \udcb4 risk", "base_url": f"http://127.0.0.1:{server.server_port}"},
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("https://example.test/market-news", result.data["results"][0]["url"])
                self.assertEqual("Market news", result.data["results"][0]["title"])
                self.assertEqual("Policy & risk update", result.data["results"][0]["snippet"])
                self.assertNotEqual("2026-09-01T01:30:00Z", result.fact_as_of)
            finally:
                server.shutdown()
                server.server_close()

    def test_browser_capture_executes_public_page_javascript_in_an_ephemeral_browser(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"<html><body><script>document.body.insertAdjacentHTML('beforeend','<p>dynamic market breadth 3210</p>')</script></body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "generic_browser_capture", "2026-09-01T01:30:00Z", 8.0,
                    {"url": f"http://127.0.0.1:{server.server_port}/dynamic"},
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("dynamic", result.data["capture_mode"])
                self.assertIn("dynamic market breadth 3210", result.data["text"])
                self.assertEqual(("default:succeeded",), result.attempts)
            finally:
                server.shutdown()
                server.server_close()

    def test_browser_capture_falls_back_to_static_read_when_browser_is_unavailable(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = b"<html><body>static market breadth fallback</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with mock.patch.dict(os.environ, {"AI_TRADING_COMPANION_DISABLE_DYNAMIC_BROWSER": "1"}):
                    result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                        1, "generic_browser_capture", "2026-09-01T01:30:00Z", 4.0,
                        {"url": f"http://127.0.0.1:{server.server_port}/page"},
                    ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("static", result.data["capture_mode"])
                self.assertIn("static market breadth fallback", result.data["text"])
            finally:
                server.shutdown()
                server.server_close()

    def test_builtin_quote_tools_validate_a_share_identity_and_close_semantics(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/minute"):
                    body = json.dumps({"data": {"data": ["1500 10.50 100 1000.0"]}}).encode("utf-8")
                else:
                    body = (
                        'v_sh600000="1~浦发银行~600000~10.50~10.00~~~~~~~~~~~~20260901150100";\n'
                        'v_sz000001="51~平安银行~000001~11.20~11.00~~~~~~~~~~~~20260901150100";\n'
                        'v_bj830001="47~北交所样本~830001~21.00~20.00~~~~~~~~~~~~20260901150100";\n'
                    ).encode("gb18030")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=gb18030")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                runner = ToolRunner(ToolCatalog(root))
                identities = runner.resolve(FactRequest(
                    1, "cn_equity_identity", "2026-09-01T07:01:00Z", 2.0,
                    {"symbols": ["600000", "000001", "830001"]},
                ))
                quotes = runner.resolve_with_fallback(FactRequest(
                    1, "cn_equity_quote_batch", "2026-09-01T07:01:00Z", 2.0,
                    {
                        "symbols": ["600000", "000001", "830001"],
                        "quote_url": f"http://127.0.0.1:{server.server_port}/quotes?q=",
                        "tencent_minute_url": f"http://127.0.0.1:{server.server_port}/minute?code=",
                    },
                    finality="official_close",
                ))

                self.assertTrue(identities.succeeded, identities.error_code)
                self.assertEqual(["SSE", "SZSE", "BSE"], [item["exchange"] for item in identities.data["identities"]])
                self.assertTrue(quotes.succeeded, quotes.error_code)
                self.assertEqual("official_close", quotes.data["finality"])
                self.assertEqual(["600000", "000001", "830001"], [item["symbol"] for item in quotes.data["quotes"]])
                self.assertEqual("2026-09-01", quotes.data["quotes"][0]["trading_date"])
            finally:
                server.shutdown()
                server.server_close()

    def test_quote_result_with_a_stale_date_or_nonclose_time_is_rejected_and_degraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_quote_batch", """
                import json
                import sys
                request = json.load(sys.stdin)
                intraday = request["inputs"].get("case") == "intraday"
                date = "2026-09-01" if intraday else "2026-08-31"
                quote_time = f"{date}T14:30:00+08:00" if intraday else f"{date}T15:01:00+08:00"
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1",
                    "fact_as_of": "2026-09-01T06:30:00Z" if intraday else "2026-08-31T07:01:00Z",
                    "data": {"finality": "official_close", "quotes": [{
                        "symbol": "600000", "name": "浦发银行", "exchange": "SSE", "market": "CN-A",
                        "price": 10.5, "quote_at": quote_time, "trading_date": date,
                        "status": "trading" if intraday else "closed", "source": "test",
                    }]},
                }))
            """)
            runner = ToolRunner(ToolCatalog(root))
            result = runner.resolve_with_fallback(FactRequest(
                1, "cn_equity_quote_batch", "2026-09-01T07:01:00Z", 2.0,
                {"symbols": ["600000"]}, finality="official_close",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_quote_trading_date_mismatch", result.error_code)
            health = json.loads((root / ".health" / "cn_equity_quote_batch-default-1.0.0.json").read_text(encoding="utf-8"))
            self.assertTrue(health["degraded"])

            intraday = runner.resolve(FactRequest(
                1, "cn_equity_quote_batch", "2026-09-01T07:01:00Z", 2.0,
                {"symbols": ["600000"], "case": "intraday"}, finality="official_close",
            ))
            self.assertFalse(intraday.succeeded)
            self.assertEqual("tool_quote_finality_invalid", intraday.error_code)

    def test_quote_tool_falls_back_from_tencent_to_sina_with_source_and_close_time(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/tencent"):
                    body, content_type = b'v_sh600000="broken";', "text/plain; charset=utf-8"
                else:
                    body = ('var hq_str_sh600000="浦发银行,9.130,9.160,9.280,9.290,9.100,9.280,9.290,65362772,604326301.000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-09-01,15:01:00,00,";').encode("gb18030")
                    content_type = "text/plain; charset=gb18030"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_equity_quote_batch", "2026-09-01T07:01:00Z", 4.0,
                    {
                        "symbols": ["600000"],
                        "tencent_quote_url": f"http://127.0.0.1:{server.server_port}/tencent?q=",
                        "sina_quote_url": f"http://127.0.0.1:{server.server_port}/sina?list=",
                    },
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("sina_quote", result.data["source"])
                self.assertEqual("2026-09-01T15:01:00+08:00", result.data["quotes"][0]["quote_at"])
                self.assertEqual(("tencent:tool_process_failed", "sina:succeeded"), result.attempts)
            finally:
                server.shutdown()
                server.server_close()

    def test_market_tools_return_indices_breadth_and_theme_snapshot_with_fact_time(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/index"):
                    body = (
                        'v_sh000001="1~上证指数~000001~3500.0~3490.0~~~~~~~~~~~~20260901150100";\n'
                        'v_sz399001="51~深证成指~399001~12000.0~11900.0~~~~~~~~~~~~20260901150100";\n'
                    ).encode("gb18030")
                    content_type = "text/plain; charset=gb18030"
                elif self.path.startswith("/minute"):
                    body = json.dumps({"data": {"data": ["1500 3500.0 100 1000.0"]}}).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                else:
                    body = json.dumps({
                        "fact_as_of": "2026-09-01T15:01:00+08:00", "trading_date": "2026-09-01", "source": "public_snapshot",
                        "indices": [{"symbol": "000001", "name": "上证指数", "exchange": "SSE", "price": 3500.0}],
                        "breadth": {"up": 3210, "down": 1100, "flat": 120, "limit_up": 58, "limit_down": 4},
                        "industries": [{"id": "801780", "name": "银行", "strength": 1.2}],
                        "themes": [{"id": "ai", "name": "人工智能", "strength": 2.1}],
                    }, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                runner = ToolRunner(ToolCatalog(root))
                indexes = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_index_batch", "2026-09-01T07:01:00Z", 2.0,
                    {
                        "symbols": ["000001", "399001"], "index_url": f"{base}/index?q=",
                        "tencent_minute_url": f"{base}/minute?code=",
                    }, finality="official_close",
                ))
                snapshot = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_snapshot", "2026-09-01T07:01:00Z", 2.0,
                    {"url": f"{base}/snapshot"}, finality="official_close",
                ))

                self.assertTrue(indexes.succeeded, indexes.error_code)
                self.assertEqual(["000001", "399001"], [item["symbol"] for item in indexes.data["indices"]])
                self.assertTrue(snapshot.succeeded, snapshot.error_code)
                self.assertEqual(3210, snapshot.data["breadth"]["up"])
                self.assertEqual("人工智能", snapshot.data["themes"][0]["name"])
                self.assertEqual("2026-09-01T07:01:00Z", snapshot.fact_as_of)
            finally:
                server.shutdown()
                server.server_close()

    def test_default_market_snapshot_and_breadth_collect_public_facts_without_a_caller_snapshot_url(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/index"):
                    body = (
                        'v_sh000001="1~上证指数~000001~3500.0~3490.0~~~~~~~~~~~~20260901143000";\n'
                        'v_sz399001="51~深证成指~399001~12000.0~11900.0~~~~~~~~~~~~20260901143000";\n'
                        'v_sz399006="51~创业板指~399006~2600.0~2590.0~~~~~~~~~~~~20260901143000";\n'
                    ).encode("gb18030")
                    content_type = "text/plain; charset=gb18030"
                else:
                    body = json.dumps({"data": {"total": 3, "diff": [
                        {"f12": "600000", "f14": "浦发银行", "f2": 10.5, "f3": 1.2, "f124": 1788244200},
                        {"f12": "000001", "f14": "平安银行", "f2": 11.2, "f3": -2.0, "f124": 1788244200},
                        {"f12": "300001", "f14": "特锐德", "f2": 20.0, "f3": 0.0, "f124": 1788244200},
                    ]}}).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                inputs = {
                    "index_url": f"http://127.0.0.1:{server.server_port}/index?q=",
                    "breadth_url": f"http://127.0.0.1:{server.server_port}/breadth",
                }
                runner = ToolRunner(ToolCatalog(root))
                snapshot = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_snapshot", "2026-09-01T06:30:00Z", 6.0, inputs, finality="intraday",
                ))
                breadth = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_breadth", "2026-09-01T06:30:00Z", 6.0,
                    {"breadth_url": inputs["breadth_url"]}, finality="intraday",
                ))

                self.assertTrue(snapshot.succeeded, snapshot.error_code)
                self.assertEqual("tencent_quote+eastmoney_breadth", snapshot.data["source"])
                self.assertEqual(1, snapshot.data["breadth"]["up"])
                self.assertEqual(3, snapshot.data["breadth"]["universe_count"])
                self.assertEqual(["000001", "399001", "399006"], [item["symbol"] for item in snapshot.data["indices"]])
                self.assertTrue(breadth.succeeded, breadth.error_code)
                self.assertEqual(1, breadth.data["breadth"]["down"])
            finally:
                server.shutdown()
                server.server_close()

    def test_intraday_equity_quote_uses_the_last_minute_not_later_than_the_freeze(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/quote"):
                    body = b'v_sh600000="1~Test~600000~9.20~9.00~~~~~~~~~~~~20260901094800";'
                    content_type = "text/plain; charset=utf-8"
                else:
                    body = json.dumps({"data": {"sh600000": {"data": {"data": ["0945 9.10", "0948 9.20"]}}}}).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_equity_quote_batch", "2026-09-01T01:45:00Z", 4.0,
                    {"symbols": ["600000"], "tencent_quote_url": base + "/quote?q=", "tencent_minute_url": base + "/minute?code="},
                    finality="intraday",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(9.10, result.data["quotes"][0]["price"])
                self.assertEqual("2026-09-01T01:45:00Z", result.data["quotes"][0]["quote_at"])
                self.assertLessEqual(result.fact_as_of, "2026-09-01T01:45:00Z")
            finally:
                server.shutdown()
                server.server_close()

    def test_official_close_equity_quote_uses_the_1500_minute_not_the_later_spot_timestamp(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/quote"):
                    body = b'v_sh600000="1~Test~600000~9.20~9.00~~~~~~~~~~~~~~~~~~~~~~~~~~20260901154000";'
                    content_type = "text/plain; charset=utf-8"
                else:
                    body = json.dumps({"data": {"sh600000": {"data": {"data": [
                        "1459 9.10 100 1000.0", "1500 9.15 120 1200.0",
                    ]}}}}).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_equity_quote_batch", "2026-09-01T07:00:00Z", 4.0,
                    {"symbols": ["600000"], "tencent_quote_url": base + "/quote?q=", "tencent_minute_url": base + "/minute?code="},
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                quote = result.data["quotes"][0]
                self.assertEqual(9.15, quote["price"])
                self.assertEqual("2026-09-01T07:00:00Z", quote["quote_at"])
                self.assertEqual("closed", quote["status"])
                self.assertEqual("official_close", result.data["finality"])
            finally:
                server.shutdown()
                server.server_close()

    def test_current_equity_bar_returns_a_fresh_forming_market_hub_interval(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path != "/stocks/quotes?codes=600000&freq=1m&datetime=now&count=1&adjust=none":
                    self.send_error(400, "unexpected MarketHub current-Bar request")
                    return
                body = json.dumps({
                    "items": [{
                        "code": "600000", "trade_time": "2026-09-01T09:45:00+08:00", "freq": "1m",
                        "open": 9.1, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 1200.0,
                        "amount": 11040.0, "adjust": "none", "is_suspended": False, "is_st": False,
                        "interval_start": "2026-09-01T09:45:00+08:00",
                        "interval_end": "2026-09-01T09:46:00+08:00", "is_final": False,
                        "observed_at": "2026-09-01T09:45:30+08:00",
                        "last_trade_at": "2026-09-01T09:45:00+08:00", "provider": "mootdx",
                        "source_semantics": "native", "freshness_ms": 0, "degraded": False,
                        "market_status": "trading",
                    }],
                    "meta": {"complete": True}, "errors": [],
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_equity_current_bar", "2026-09-01T01:46:00Z", 4.0,
                    {"symbols": ["600000"], "freq": "1m", "markethub_url": f"http://127.0.0.1:{server.server_port}/stocks/quotes"},
                    finality="intraday",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("2026-09-01T01:45:30Z", result.fact_as_of)
                self.assertEqual("600000", result.data["bars"][0]["symbol"])
                self.assertFalse(result.data["bars"][0]["is_final"])
                self.assertEqual("native", result.data["bars"][0]["source_semantics"])
                self.assertEqual(
                    "http://127.0.0.1:%d/stocks/quotes?codes=600000&freq=1m&datetime=now&count=1&adjust=none" % server.server_port,
                    result.data["source_urls"][0],
                )
            finally:
                server.shutdown()
                server.server_close()

    def test_current_equity_bar_uses_derived_tencent_minutes_after_markethub_failure(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/stocks/quotes"):
                    self.send_error(503, "MarketHub unavailable")
                    return
                if self.path.startswith("/minute?code=sh600000"):
                    body = json.dumps({"data": {"sh600000": {"data": [
                        "1428 10.00 100 1000", "1429 10.20 120 1224",
                        # Tencent exposes the still-forming current minute too.
                        # A 14:30:13 cutoff must keep the completed 14:29 bar.
                        "1430 10.30 125 1275",
                    ]}}}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(404)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_equity_current_bar", "2026-09-01T06:30:00Z", 4.0,
                    {"symbols": ["600000"], "freq": "1m", "markethub_url": f"{base}/stocks/quotes",
                     "tencent_minute_url": f"{base}/minute?code="},
                    finality="intraday",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(("markethub:tool_process_failed", "tencent:succeeded"), result.attempts)
                bar = result.data["bars"][0]
                self.assertEqual("derived", bar["source_semantics"])
                self.assertEqual("tencent_minute", bar["provider"])
                self.assertEqual(10.2, bar["close"])
                self.assertEqual("2026-09-01T14:30:00+08:00", bar["interval_end"])
            finally:
                server.shutdown()
                server.server_close()

    def test_current_equity_bar_rejects_a_stale_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_equity_current_bar", """
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1", "fact_as_of": "2026-09-01T01:45:30Z",
                    "data": {"finality": "intraday", "bars": [{
                        "symbol": "600000", "exchange": "SSE", "market": "CN-A", "freq": "1m",
                        "interval_start": "2026-09-01T09:45:00+08:00", "interval_end": "2026-09-01T09:46:00+08:00",
                        "observed_at": "2026-09-01T09:45:30+08:00", "last_trade_at": "2026-09-01T09:45:00+08:00",
                        "open": 9.1, "high": 9.3, "low": 9.0, "close": 9.2, "volume": 1200, "amount": 11040,
                        "freshness_ms": 300001, "is_final": False, "degraded": True,
                        "provider": "mootdx", "source_semantics": "native",
                    }]},
                }))
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                1, "cn_equity_current_bar", "2026-09-01T01:46:00Z", 2.0,
                {"symbols": ["600000"], "freq": "1m"}, finality="intraday",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_current_bar_stale", result.error_code)

    def test_market_snapshot_rejects_nontrading_or_stale_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_market_snapshot", """
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1", "fact_as_of": "2026-08-31T07:01:00Z",
                    "data": {"finality": "official_close", "is_trading_day": False, "trading_date": "2026-08-31", "source": "test",
                    "indices": [{"symbol": "000001", "name": "上证指数", "exchange": "SSE", "price": 1}],
                    "breadth": {"up": 1, "down": 1, "flat": 0, "limit_up": 0, "limit_down": 0}, "industries": [], "themes": []},
                }))
            """)
            result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                1, "cn_market_snapshot", "2026-09-01T07:01:00Z", 2.0, {}, finality="official_close",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_market_non_trading_day", result.error_code)

    def test_turnover_compare_rejects_an_inconsistent_difference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_market_turnover_compare", """
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1", "fact_as_of": "2026-09-01T07:00:00Z",
                    "data": {
                        "trading_date": "2026-09-01", "previous_trading_date": "2026-08-31",
                        "scope": "SSE+SZSE", "unit": "CNY", "current_amount": 100.0,
                        "previous_amount": 80.0, "change_amount": 19.0, "change_ratio": 0.25,
                        "source": "test", "source_urls": ["https://example.test/turnover"],
                        "finality": "official_close"
                    },
                }))
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                1, "cn_market_turnover_compare", "2026-09-01T07:00:00Z", 2.0, {},
                finality="official_close",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_market_turnover_calculation_invalid", result.error_code)

    def test_builtin_turnover_compare_uses_two_complete_sessions_with_one_scope(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                amount_rows = (
                    ("100000000000", "110000000000")
                    if "secid=1.000001" in self.path else
                    ("200000000000", "240000000000")
                )
                body = json.dumps({"data": {"klines": [
                    f"2026-08-31,1,1,1,1,1,{amount_rows[0]},1,1,1,1",
                    f"2026-09-01,1,1,1,1,1,{amount_rows[1]},1,1,1,1",
                ]}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-01T07:00:00Z", 4.0,
                    {"eastmoney_kline_url": f"http://127.0.0.1:{server.server_port}/kline"},
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("2026-09-01", result.data["trading_date"])
                self.assertEqual("2026-08-31", result.data["previous_trading_date"])
                self.assertEqual("SSE+SZSE", result.data["scope"])
                self.assertEqual(350_000_000_000.0, result.data["current_amount"])
                self.assertEqual(300_000_000_000.0, result.data["previous_amount"])
                self.assertEqual(50_000_000_000.0, result.data["change_amount"])
                self.assertAlmostEqual(1 / 6, result.data["change_ratio"])
                self.assertEqual("2026-09-01T07:00:00Z", result.fact_as_of)
                self.assertEqual(2, len(result.data["source_urls"]))
            finally:
                server.shutdown()
                server.server_close()

    def test_sector_snapshot_rejects_non_http_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_market_sector_snapshot", """
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1", "fact_as_of": "2026-09-01T07:00:00Z",
                    "data": {
                        "trading_date": "2026-09-01", "finality": "official_close", "source": "test",
                        "source_urls": ["file:///tmp/sector.json"],
                        "leaders": [{"board_id": "BK1", "name": "半导体", "kind": "industry",
                                     "change_percent": 3.2, "core": {"symbol": "600000", "name": "核心A",
                                     "amount": 1000000.0, "change_percent": 4.1}}],
                        "laggards": [{"board_id": "BK2", "name": "地产", "kind": "industry",
                                      "change_percent": -2.2, "core": {"symbol": "000001", "name": "核心B",
                                      "amount": 900000.0, "change_percent": -2.8}}]
                    },
                }))
            """)

            result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                1, "cn_market_sector_snapshot", "2026-09-01T07:20:00Z", 2.0, {},
                finality="official_close",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_market_source_urls_invalid", result.error_code)

    def test_builtin_sector_snapshot_returns_leaders_laggards_and_capacity_cores(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = parse_qs(urlsplit(self.path).query)
                if self.path.startswith("/boards"):
                    kind = "theme" if "t:3" in query.get("fs", [""])[0] else "industry"
                    values = {
                        "industry": [("BK100", "半导体", 3.2), ("BK300", "房地产", -2.1)],
                        "theme": [("BK200", "机器人", 4.5), ("BK400", "白酒", -3.0)],
                    }[kind]
                    body = json.dumps({"data": {"total": 2, "diff": [{
                        "f12": row[0], "f14": row[1], "f3": row[2], "f124": 1788246000,
                    } for row in values]}}).encode("utf-8")
                else:
                    board = query.get("fs", ["b:BK000"])[0].split(":", 1)[-1]
                    symbols = {"BK100": "600100", "BK200": "300200", "BK300": "600300", "BK400": "000400"}
                    body = json.dumps({"data": {"diff": [{
                        "f12": symbols[board], "f14": "容量核心" + board[-1],
                        "f3": 2.5 if board in {"BK100", "BK200"} else -2.5,
                        "f6": 12_000_000_000, "f124": 1788246000,
                    }]}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_sector_snapshot", "2026-09-01T07:20:00Z", 8.0,
                    {"eastmoney_board_url": base + "/boards", "eastmoney_constituent_url": base + "/cores"},
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(["半导体", "机器人"], [row["name"] for row in result.data["leaders"]])
                self.assertEqual(["房地产", "白酒"], [row["name"] for row in result.data["laggards"]])
                self.assertEqual(
                    ["600100", "300200", "600300", "000400"],
                    [row["core"]["symbol"] for row in [*result.data["leaders"], *result.data["laggards"]]],
                )
                self.assertEqual("2026-09-01T07:00:01Z", result.fact_as_of)
                self.assertGreaterEqual(len(result.data["source_urls"]), 6)
                self.assertEqual(2, result.data["distribution"]["industry"]["total"])
                self.assertEqual(2, result.data["distribution"]["theme"]["total"])
            finally:
                server.shutdown()
                server.server_close()

    def test_builtin_fund_flow_snapshot_sums_both_markets_for_the_frozen_close(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                secid = parse_qs(urlsplit(self.path).query).get("secid", [""])[0]
                main = -100.0 if secid == "1.000001" else -200.0
                payload = {"data": {"klines": [f"2026-09-04,{main},30,40,50,60"]}}
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"; ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/flow"
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_fund_flow_snapshot", "2026-09-04T07:00:00Z", 5.0,
                    {"eastmoney_history_url": endpoint}, finality="official_close",
                ))
                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(-300.0, result.data["combined"]["main_net_inflow"])
                self.assertEqual({"SSE", "SZSE"}, {row["exchange"] for row in result.data["markets"]})
            finally:
                server.shutdown(); server.server_close()

    def test_builtin_fund_flow_retries_a_transient_disconnect(self) -> None:
        calls: dict[str, int] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                secid = parse_qs(urlsplit(self.path).query).get("secid", [""])[0]
                calls[secid] = calls.get(secid, 0) + 1
                if calls[secid] <= 2:
                    self.close_connection = True
                    return
                body = json.dumps({
                    "data": {"klines": ["2026-09-04,-100,30,40,50,60"]},
                }).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"; ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/flow"
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_market_fund_flow_snapshot", "2026-09-04T07:00:00Z", 5.0,
                    {"eastmoney_history_url": endpoint}, finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual({"1.000001": 3, "0.399001": 3}, calls)
            finally:
                server.shutdown(); server.server_close()

    def test_builtin_market_event_snapshot_checks_each_source_and_freezes_the_window(self) -> None:
        requested_sources: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                query = parse_qs(urlsplit(self.path).query)
                source = query.get("source", [""])[0]
                requested_sources.append(source)
                articles = [{
                    "article_id": source + f":{index}", "published_at": "2026-09-04 15:30",
                    "title": f"A股收盘政策观察 {index}", "content": "市场风险与政策变化。" * 100,
                    "source_url": f"https://example.test/{source}/{index}",
                } for index in range(40)]
                articles.append({
                    "article_id": source + ":future", "published_at": "2026-09-05 12:30",
                    "title": "冻结时点之后", "content": "不得进入证据。",
                    "source_url": f"https://example.test/{source}/future",
                })
                payload = {
                    "source": source, "start_date": "2026-08-31", "end_date": "2026-09-05",
                    "groups": [{"source_key": source, "count": len(articles), "articles": articles}],
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"; ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_market_event_snapshot", "2026-09-05T02:00:00Z", 8.0, {
                        "base_url": f"http://127.0.0.1:{server.server_port}",
                        "start_at": "2026-08-31T07:00:00Z", "end_at": "2026-09-05T02:00:00Z",
                    }, finality="observed",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(
                    ["eastmoney_daily_topic_report", "cls_depth_article", "ths_important_news"],
                    requested_sources,
                )
                self.assertEqual(120, result.data["matched_count"])
                self.assertEqual(15, len(result.data["articles"]))
                self.assertTrue(all(len(row["content"]) <= 600 for row in result.data["articles"]))
                self.assertTrue(all("future" not in row["article_id"] for row in result.data["articles"]))
                self.assertEqual("2026-09-05T02:00:00Z", result.fact_as_of)
            finally:
                server.shutdown(); server.server_close()

    def test_builtin_announcement_snapshot_filters_and_normalizes_the_frozen_window(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                symbol = parse_qs(urlsplit(self.path).query).get("q", [""])[0]
                payload = {"公告": [
                    {"代码": symbol, "简称": "白云电器", "公告标题": "回购进展公告",
                     "公告内容": "截至本公告日，公司已完成本月回购进展披露。", "公告日期": "2026-09-02"},
                    {"代码": symbol, "简称": "白云电器", "公告标题": "旧公告",
                     "公告内容": "窗口外", "公告日期": "2026-08-01"},
                ]}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"; ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_equity_announcement_snapshot", "2026-09-05T02:00:00Z", 5.0,
                    {"symbols": ["603861"], "start_date": "2026-08-31", "end_date": "2026-09-05",
                     "base_url": f"http://127.0.0.1:{server.server_port}"}, finality="observed",
                ))
                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual(["603861"], result.data["checked_symbols"])
                self.assertEqual(["回购进展公告"], [row["title"] for row in result.data["announcements"]])
                self.assertEqual("2026-09-02", result.data["announcements"][0]["announcement_date"])
            finally:
                server.shutdown(); server.server_close()

    def test_close_market_capabilities_fall_back_after_primary_source_outage(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith(("/kline", "/boards", "/cores")):
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path.startswith("/turnover"):
                    payload = {
                        "contract": "markethub-cn-market-turnover-compare-v1",
                        "trading_date": "2026-09-01", "previous_trading_date": "2026-08-31",
                        "fact_as_of": "2026-09-01T15:00:00+08:00", "scope": "SSE+SZSE",
                        "unit": "CNY", "current_amount": 350_000_000_000.0,
                        "previous_amount": 300_000_000_000.0, "change_amount": 50_000_000_000.0,
                        "change_ratio": 1 / 6, "source": "markethub_test",
                        "scope_definition": "test_exchange_totals", "scope_note": "test CNY scope",
                        "current_markets": [
                            {"exchange": "SSE", "market_total_id": "test_sse", "amount": 150_000_000_000.0, "unit": "CNY"},
                            {"exchange": "SZSE", "market_total_id": "test_szse", "amount": 200_000_000_000.0, "unit": "CNY"},
                        ],
                        "previous_markets": [
                            {"exchange": "SSE", "market_total_id": "test_sse", "amount": 130_000_000_000.0, "unit": "CNY"},
                            {"exchange": "SZSE", "market_total_id": "test_szse", "amount": 170_000_000_000.0, "unit": "CNY"},
                        ],
                    }
                else:
                    payload = {
                        "contract": "markethub-cn-market-sector-snapshot-v1",
                        "trading_date": "2026-09-01", "fact_as_of": "2026-09-01T15:00:00+08:00",
                        "source": "markethub_test",
                        "leaders": [{"board_id": "BK1", "name": "半导体", "kind": "industry",
                                     "change_percent": 3.2, "core": {"symbol": "600000", "name": "核心A",
                                     "amount": 1_000_000.0, "change_percent": 4.1}}],
                        "laggards": [{"board_id": "BK2", "name": "房地产", "kind": "industry",
                                      "change_percent": -2.2, "core": {"symbol": "000001", "name": "核心B",
                                      "amount": 900_000.0, "change_percent": -2.8}}],
                    }
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                runner = ToolRunner(ToolCatalog(root))
                turnover = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-01T07:00:00Z", 5.0,
                    {"eastmoney_kline_url": base + "/kline", "sse_turnover_url": base + "/sse",
                     "szse_turnover_url": base + "/szse", "markethub_turnover_url": base + "/turnover"},
                    finality="official_close",
                ))
                sectors = runner.resolve_with_fallback(FactRequest(
                    1, "cn_market_sector_snapshot", "2026-09-01T07:20:00Z", 5.0,
                    {"eastmoney_board_url": base + "/boards", "eastmoney_constituent_url": base + "/cores",
                     "markethub_sector_url": base + "/sectors"}, finality="official_close",
                ))

                self.assertTrue(turnover.succeeded, turnover.error_code)
                self.assertEqual((
                    "eastmoney_history:tool_process_failed", "official_exchanges:tool_process_failed",
                    "eastmoney_spot_markethub:succeeded",
                ), turnover.attempts)
                self.assertTrue(sectors.succeeded, sectors.error_code)
                self.assertEqual(("eastmoney:tool_process_failed", "markethub:succeeded"), sectors.attempts)
            finally:
                server.shutdown()
                server.server_close()

    def test_turnover_fallback_rejects_partial_days_and_accepts_complete_snapshot_scope(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            previous_count = 1000

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/kline"):
                    self.send_response(503)
                    self.end_headers()
                    return
                query = parse_qs(urlsplit(self.path).query)
                if self.path.startswith("/tencent"):
                    records = []
                    for vendor, symbol, timestamp, amount in (
                        ("sh000001", "000001", "20260904161403", "500000000"),
                        ("sz399106", "399106", "20260904161421", "500000000"),
                    ):
                        fields = [""] * 88
                        fields[0:5] = ["1", vendor, symbol, "1", "1"]
                        fields[30] = timestamp
                        fields[35] = f"1/1/{amount}"
                        records.append(f'v_{vendor}="{"~".join(fields)}";')
                    body = "\n".join(records).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/spot"):
                    page = int(query.get("pn", ["1"])[0])
                    start = (page - 1) * 100
                    rows = [{
                        "f12": (f"{600000 + index:06d}" if index < 500 else f"{index - 499:06d}"),
                        "f6": 1_000_000.0, "f124": 1788505200,
                    } for index in range(start, min(start + 100, 1000))]
                    payload = {"data": {"total": 1000, "diff": rows}}
                else:
                    payload = [{
                        "code": (f"{600000 + index:06d}" if index < 500 else f"{index - 499:06d}"),
                        "trade_time": "2026-09-03",
                        "amount": 900_000.0,
                    } for index in range(self.previous_count)]
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-04T07:00:00Z", 8.0,
                    {"eastmoney_kline_url": base + "/kline", "eastmoney_spot_url": base + "/spot",
                     "sse_turnover_url": base + "/sse", "szse_turnover_url": base + "/szse",
                     "tencent_turnover_url": base + "/tencent?q=",
                     "markethub_snapshot_url": base + "/snapshot"}, finality="official_close",
                )
                Handler.previous_count = 100
                partial_runner = ToolRunner(ToolCatalog(root))
                partial = partial_runner.resolve_with_fallback(request)
                self.assertFalse(partial.succeeded)
                self.assertEqual("tool_process_failed", partial.error_code)
                self.assertEqual({
                    "eastmoney_history:tool_process_failed", "official_exchanges:tool_process_failed",
                    "eastmoney_spot_markethub:tool_process_failed", "tencent_spot_markethub:tool_process_failed",
                }, set(partial.attempts))
                self.assertIsNotNone(partial.diagnostic_artifact_ref)
                diagnostic = partial_runner.artifacts.read(partial.diagnostic_artifact_ref).decode("utf-8")
                self.assertIn("previous turnover snapshot is incomplete", diagnostic)

                Handler.previous_count = 1000
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(request)
                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual((
                    "eastmoney_history:tool_process_failed", "official_exchanges:tool_process_failed",
                    "eastmoney_spot_markethub:succeeded",
                ), result.attempts)
                self.assertEqual(1_000_000_000.0, result.data["current_amount"])
                self.assertEqual(900_000_000.0, result.data["previous_amount"])
                self.assertEqual({"current_security_count": 1000, "previous_security_count": 1000}, result.data["coverage"])
            finally:
                server.shutdown()
                server.server_close()

    def test_turnover_current_falls_back_from_eastmoney_to_tencent_exact_amounts(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            tencent_case = "ok"
            previous_count = 1000

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith(("/kline", "/spot", "/sse", "/szse")):
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path.startswith("/tencent"):
                    records = []
                    for vendor, name, symbol, price, previous, timestamp, volume, amount in (
                        ("sh000001", "上证指数", "000001", "3930.12", "3942.09", "20260904161403", "537286161", "938255187184"),
                        ("sz399106", "深证综指", "399106", "2492.96", "2512.87", "20260904161421", "678475372", "1092412648913"),
                    ):
                        if self.tencent_case == "wrong_date":
                            timestamp = "20260903" + timestamp[8:]
                        elif self.tencent_case == "open":
                            timestamp = timestamp[:8] + "145900"
                        exact_amount = "0" if self.tencent_case == "zero_amount" else amount
                        fields = [""] * 88
                        fields[0:5] = [
                            "1", name,
                            ("399001" if self.tencent_case == "wrong_identity" and vendor == "sz399106" else symbol),
                            price, previous,
                        ]
                        fields[30] = timestamp
                        fields[35] = (
                            f"{price}/{volume}" if self.tencent_case == "wrong_field"
                            else f"{price}/{volume}/{exact_amount}"
                        )
                        fields[36] = volume
                        fields[37] = str(round(int(amount) / 10_000))
                        records.append(f'v_{vendor}="{"~".join(fields)}";')
                    body = "\n".join(records).encode("gb18030")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=gb18030")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = json.dumps([{
                    "code": (f"{600000 + index:06d}" if index < 500 else f"{index - 499:06d}"),
                    "trade_time": "2026-09-03",
                    "amount": 900_000.0,
                } for index in range(self.previous_count)]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                request = FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-04T07:20:00Z", 8.0,
                    {"eastmoney_kline_url": base + "/kline", "eastmoney_spot_url": base + "/spot",
                     "sse_turnover_url": base + "/sse", "szse_turnover_url": base + "/szse",
                     "tencent_turnover_url": base + "/tencent?q=", "markethub_snapshot_url": base + "/snapshot"},
                    finality="official_close",
                )
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(request)

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual((
                    "eastmoney_history:tool_process_failed", "official_exchanges:tool_process_failed",
                    "eastmoney_spot_markethub:tool_process_failed", "tencent_spot_markethub:succeeded",
                ), result.attempts)
                self.assertEqual(2_030_667_836_097.0, result.data["current_amount"])
                self.assertEqual([
                    {"exchange": "SSE", "market_total_id": "sh000001",
                     "amount": 938_255_187_184, "unit": "CNY"},
                    {"exchange": "SZSE", "market_total_id": "sz399106",
                     "amount": 1_092_412_648_913, "unit": "CNY"},
                ], result.data["current_markets"])
                self.assertTrue(any("/tencent?q=sh000001,sz399106" in url for url in result.data["source_urls"]))

                for case, previous_count in (
                    ("wrong_date", 1000), ("open", 1000), ("wrong_field", 1000),
                    ("wrong_identity", 1000), ("zero_amount", 1000), ("ok", 100),
                ):
                    with self.subTest(case=case):
                        Handler.tencent_case = case
                        Handler.previous_count = previous_count
                        failed = ToolRunner(ToolCatalog(root)).resolve_with_fallback(request)
                        self.assertFalse(failed.succeeded)
                        self.assertEqual("tool_process_failed", failed.error_code)
                        self.assertIn("tencent_spot_markethub:tool_process_failed", failed.attempts)
            finally:
                server.shutdown()
                server.server_close()

    def test_official_exchange_turnover_uses_two_common_sessions_and_unit_conversion(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            skip_previous = False

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/kline"):
                    self.send_response(503)
                    self.end_headers()
                    return
                query = parse_qs(urlsplit(self.path).query)
                trading_date = query.get("SEARCH_DATE", query.get("txtQueryDate", [""]))[0]
                amounts = {
                    "2026-09-04": ("9395.56", "10,940.26"),
                    "2026-09-03": ("8206.03", "9,400.89"),
                    "2026-09-02": ("7000.00", "8,000.00"),
                }
                published = trading_date in amounts and not (self.skip_previous and trading_date == "2026-09-03")
                if self.path.startswith("/sse"):
                    rows = ([{"PRODUCT_CODE": "17", "TRADE_AMT": amounts[trading_date][0],
                              "TRADE_DATE": trading_date.replace("-", "")}] if published else [])
                    body = ("jsonpCallback(" + json.dumps({"result": rows}) + ")").encode("utf-8")
                else:
                    data = ([{"zbmc": "成交量（亿）", "gp": "1.00"},
                             {"zbmc": "成交金额（亿元）", "gp": amounts[trading_date][1]}]
                            if published else [])
                    body = json.dumps([{
                        "metadata": {"conditions": [{"name": "txtQueryDate", "defaultValue": trading_date}]},
                        "data": data, "error": None,
                    }], ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-04T07:20:00Z", 8.0,
                    {"eastmoney_kline_url": base + "/kline", "sse_turnover_url": base + "/sse",
                     "szse_turnover_url": base + "/szse"}, finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual((
                    "eastmoney_history:tool_process_failed", "official_exchanges:succeeded",
                ), result.attempts)
                self.assertEqual(2_033_582_000_000.0, result.data["current_amount"])
                self.assertEqual(1_760_692_000_000.0, result.data["previous_amount"])
                self.assertEqual(272_890_000_000.0, result.data["change_amount"])
                self.assertAlmostEqual(272_890 / 1_760_692, result.data["change_ratio"])
                self.assertEqual("exchange_published_stock_total", result.data["scope_definition"])
                self.assertEqual(4, len(result.data["source_evidence"]))
                self.assertEqual(4, len(result.data["source_urls"]))
                self.assertEqual("2026-09-04T07:00:00Z", result.fact_as_of)

                Handler.skip_previous = True
                prior_common = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_turnover_compare", "2026-09-04T07:20:00Z", 8.0,
                    {"eastmoney_kline_url": base + "/kline", "sse_turnover_url": base + "/sse",
                     "szse_turnover_url": base + "/szse"}, finality="official_close",
                ))
                self.assertTrue(prior_common.succeeded, prior_common.error_code)
                self.assertEqual("2026-09-02", prior_common.data["previous_trading_date"])
                self.assertEqual(1_500_000_000_000.0, prior_common.data["previous_amount"])
            finally:
                server.shutdown()
                server.server_close()

    def test_official_exchange_turnover_fails_closed_to_the_next_provider(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            case = "wrong_date"

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/kline"):
                    self.send_response(503)
                    self.end_headers()
                    return
                if self.path.startswith("/fallback"):
                    payload = {
                        "contract": "markethub-cn-market-turnover-compare-v1",
                        "trading_date": "2026-09-04", "previous_trading_date": "2026-09-03",
                        "fact_as_of": "2026-09-04T15:00:00+08:00", "scope": "SSE+SZSE",
                        "scope_definition": "test_exchange_totals", "scope_note": "test CNY scope",
                        "unit": "CNY", "current_amount": 200.0, "previous_amount": 100.0,
                        "change_amount": 100.0, "change_ratio": 1.0, "source": "fallback_test",
                        "current_markets": [
                            {"exchange": "SSE", "market_total_id": "test_sse", "amount": 90.0, "unit": "CNY"},
                            {"exchange": "SZSE", "market_total_id": "test_szse", "amount": 110.0, "unit": "CNY"},
                        ],
                        "previous_markets": [
                            {"exchange": "SSE", "market_total_id": "test_sse", "amount": 40.0, "unit": "CNY"},
                            {"exchange": "SZSE", "market_total_id": "test_szse", "amount": 60.0, "unit": "CNY"},
                        ],
                    }
                    body = json.dumps(payload).encode("utf-8")
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    trading_date = query.get("SEARCH_DATE", query.get("txtQueryDate", [""]))[0]
                    published = self.case != "not_published"
                    if self.path.startswith("/sse"):
                        echoed_date = "20260903" if self.case == "wrong_date" else trading_date.replace("-", "")
                        product_code = "01" if self.case == "wrong_product" else "17"
                        rows = ([{"PRODUCT_CODE": product_code, "TRADE_AMT": "1.00", "TRADE_DATE": echoed_date}]
                                if published else [])
                        body = ("jsonpCallback(" + json.dumps({"result": rows}) + ")").encode("utf-8")
                    elif self.case == "missing_exchange":
                        self.send_response(503)
                        self.end_headers()
                        return
                    else:
                        data = ([{"zbmc": "成交金额（亿元）", "gp": "1.00"}] if published else [])
                        body = json.dumps([{
                            "metadata": {"conditions": [{"name": "txtQueryDate", "defaultValue": trading_date}]},
                            "data": data, "error": ("upstream error" if self.case == "szse_error" else None),
                        }], ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                for case in (
                    "wrong_date", "missing_exchange", "not_published", "wrong_product", "szse_error",
                ):
                    with self.subTest(case=case):
                        Handler.case = case
                        root = Path(directory) / case / "tools"
                        ensure_builtin_tools(root)
                        result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                            1, "cn_market_turnover_compare", "2026-09-04T07:20:00Z", 8.0,
                            {"eastmoney_kline_url": base + "/kline", "sse_turnover_url": base + "/sse",
                             "szse_turnover_url": base + "/szse", "markethub_turnover_url": base + "/fallback"},
                            finality="official_close",
                        ))
                        self.assertTrue(result.succeeded, result.error_code)
                        self.assertEqual((
                            "eastmoney_history:tool_process_failed", "official_exchanges:tool_process_failed",
                            "eastmoney_spot_markethub:succeeded",
                        ), result.attempts)
            finally:
                server.shutdown()
                server.server_close()

    def test_market_breadth_rejects_a_snapshot_without_a_matching_trade_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            self.publish_tool(root, "cn_market_breadth", """
                import json
                print(json.dumps({
                    "contract": "ai-trading-tool-result/v1", "fact_as_of": "2026-08-31T07:01:00Z",
                    "data": {"is_trading_day": True, "trading_date": "2026-08-31", "source": "test",
                    "finality": "intraday", "breadth": {"up": 1, "down": 1, "flat": 0, "limit_up": 0, "limit_down": 0}},
                }))
            """)
            result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                1, "cn_market_breadth", "2026-09-01T07:01:00Z", 2.0, {}, finality="intraday",
            ))

            self.assertFalse(result.succeeded)
            self.assertEqual("tool_market_trading_date_mismatch", result.error_code)

    def test_official_close_breadth_uses_complete_markethub_snapshot_with_lineage(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = json.dumps({
                    "contract": "markethub-cn-a-share-market-breadth-v1",
                    "trade_date": "2026-09-01", "fact_as_of": "2026-09-01T15:00:00+08:00",
                    "status": "complete", "finality": "final",
                    "up": 1500, "down": 3900, "flat": 100, "unpriced": 0,
                    "suspended": 7, "universe_count": 5507,
                    "source": "markethub_local_canonical_daily_snapshot",
                    "lineage": {"dataset_version": "mhd-v1-test"},
                    "coverage": {
                        "eligible_count": 5507, "priced_count": 5500, "suspended_count": 7,
                        "missing_count": 0, "invalid_price_count": 0,
                        "accounted_count": 5507, "coverage_ratio": 1.0,
                    },
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve(FactRequest(
                    1, "cn_market_breadth", "2026-09-01T07:20:00Z", 4.0,
                    {"markethub_url": f"http://127.0.0.1:{server.server_port}/breadth"},
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("2026-09-01T07:00:00Z", result.fact_as_of)
                self.assertEqual(1500, result.data["breadth"]["up"])
                self.assertEqual(7, result.data["breadth"]["suspended"])
                self.assertEqual("mhd-v1-test", result.data["lineage"]["dataset_version"])
                self.assertEqual("official_close", result.data["finality"])
            finally:
                server.shutdown()
                server.server_close()

    def test_official_close_breadth_falls_back_to_full_market_public_snapshot(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/markethub"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"data": {"total": 3, "diff": [
                    {"f12": "600000", "f14": "浦发银行", "f2": 10.5, "f3": 1.2, "f124": 1788250200},
                    {"f12": "000001", "f14": "平安银行", "f2": 11.2, "f3": -2.0, "f124": 1788250200},
                    {"f12": "300001", "f14": "特锐德", "f2": 20.0, "f3": 0.0, "f124": 1788250200},
                ]}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "tools"
            ensure_builtin_tools(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                result = ToolRunner(ToolCatalog(root)).resolve_with_fallback(FactRequest(
                    1, "cn_market_breadth", "2026-09-01T07:20:00Z", 6.0,
                    {"markethub_url": f"http://127.0.0.1:{server.server_port}/markethub",
                     "breadth_url": f"http://127.0.0.1:{server.server_port}/eastmoney"},
                    finality="official_close",
                ))

                self.assertTrue(result.succeeded, result.error_code)
                self.assertEqual("2026-09-01T07:00:00Z", result.fact_as_of)
                self.assertEqual(("markethub:tool_process_failed", "eastmoney:succeeded"), result.attempts)
                self.assertEqual(1, result.data["breadth"]["up"])
                self.assertEqual(1, result.data["breadth"]["down"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
