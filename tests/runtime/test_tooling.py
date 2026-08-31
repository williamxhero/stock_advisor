from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import threading
import unittest
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
                self.assertEqual("static", capture_result.data["capture_mode"])
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


if __name__ == "__main__":
    unittest.main()
