from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_trading_companion.exchange import LocalExchange


class CompanionExchangeTests(unittest.TestCase):
    def test_receive_accepts_utf8_bom_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory))
            exchange.ensure()
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "command-with-bom",
                "cycle_id": "cycle-1",
                "type": "begin_voice_capture",
            }
            pending = Path(directory) / "to-runtime" / "pending" / "command-with-bom.json"
            pending.write_text(json.dumps(command), encoding="utf-8-sig")

            received = exchange.receive("to-runtime")

            self.assertEqual(command, received[0][1])
            self.assertFalse(any((Path(directory) / "to-runtime" / "dead-letter").iterdir()))

    def test_reject_preserves_utf8_bom_command_and_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            exchange = LocalExchange(Path(directory))
            exchange.ensure()
            command = {
                "contract": "companion-user-command/v1",
                "command_id": "rejected-command-with-bom",
                "cycle_id": "missing-cycle",
                "type": "begin_voice_capture",
            }
            pending = Path(directory) / "to-runtime" / "pending" / "rejected-command-with-bom.json"
            pending.write_text(json.dumps(command), encoding="utf-8-sig")
            claimed, received = exchange.receive("to-runtime")[0]

            exchange.reject("to-runtime", claimed, "cycle not found")

            dead_letter = Path(directory) / "to-runtime" / "dead-letter" / pending.name
            payload = json.loads(dead_letter.read_text(encoding="utf-8"))
            self.assertEqual("cycle not found", payload["error"])
            self.assertEqual(received, payload["received"])


if __name__ == "__main__":
    unittest.main()
