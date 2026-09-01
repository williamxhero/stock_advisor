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

from ai_trading_companion.builtin_tools import ensure_builtin_tools
from ai_trading_companion.tooling import FactRequest, ToolCatalog, ToolRunner


class ToolRunnerTests(unittest.TestCase):
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
                capture_result = runner.resolve(FactRequest(1, "generic_browser_capture", "2026-09-01T01:30:00Z", 2.0, {"url": f"{base}/page"}))
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
                    {"symbols": ["600000", "000001", "830001"], "quote_url": f"http://127.0.0.1:{server.server_port}/quotes?q="},
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
                    {"symbols": ["000001", "399001"], "index_url": f"{base}/index?q="}, finality="official_close",
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


if __name__ == "__main__":
    unittest.main()
