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

    def request(self, capability: str = "cn_equity_identity") -> FactRequest:
        return FactRequest(
            contract_version=1,
            capability=capability,
            required_at="2026-09-01T01:30:00Z",
            deadline_seconds=2.0,
            inputs={"symbols": ["600000"]},
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


if __name__ == "__main__":
    unittest.main()
