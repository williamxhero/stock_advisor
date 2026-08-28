from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ai_trading_companion.config import DEFAULT_PROVIDER, DEFAULT_RESEARCH
from ai_trading_companion.provider_client import ProviderClient, ProviderError, _local_codex_version


class ProviderClientTests(unittest.TestCase):
    def test_local_codex_version_decodes_utf8_without_windows_codepage_dependency(self) -> None:
        executable = mock.Mock()
        executable.stat.return_value.st_mtime = 1
        with mock.patch("ai_trading_companion.provider_client.Path.is_dir", return_value=True), mock.patch(
            "ai_trading_companion.provider_client.Path.glob", return_value=[executable],
        ), mock.patch(
            "ai_trading_companion.provider_client.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="codex-cli 1.2.3\n"),
        ) as run:
            self.assertEqual("1.2.3", _local_codex_version())
        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])
        self.assertEqual("replace", run.call_args.kwargs["errors"])

    def test_standard_payload_never_sends_native_tools(self) -> None:
        with TemporaryDirectory() as temporary:
            client = ProviderClient(DEFAULT_PROVIDER, DEFAULT_RESEARCH, Path(temporary))
            payload = client._payload("hello", "test-model", "medium")
            self.assertNotIn("tools", payload)
            self.assertNotIn("tool_choice", payload)

    def test_research_mode_is_rejected_before_any_provider_request(self) -> None:
        with TemporaryDirectory() as temporary:
            client = ProviderClient(DEFAULT_PROVIDER, DEFAULT_RESEARCH, Path(temporary))
            with mock.patch.object(client, "_request") as request:
                with self.assertRaisesRegex(ProviderError, "retired"):
                    client.run("research", None, slot="fast", effort="medium", search=True, timeout=1)
            request.assert_not_called()
