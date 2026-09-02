from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from ai_trading_companion.config import DEFAULT_PROVIDER, DEFAULT_RESEARCH
from ai_trading_companion.provider_client import ProviderClient, ProviderError


class ProviderClientTests(unittest.TestCase):
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
