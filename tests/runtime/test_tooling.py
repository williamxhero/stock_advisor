from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
