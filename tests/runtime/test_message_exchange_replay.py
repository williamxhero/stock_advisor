from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from ai_trading_companion.exchange import LocalExchange


def test_runtime_exchange_replays_the_frozen_v2_message_without_rebuilding_text() -> None:
    fixture = json.loads((Path(__file__).parents[1] / "fixtures/message-v2-runtime-exchange-desktop.json").read_text(encoding="utf-8"))
    with TemporaryDirectory() as temporary:
        exchange = LocalExchange(Path(temporary) / "exchange")
        exchange.send("to-client", fixture["event_id"], fixture)
        claimed, replayed = exchange.receive("to-client")[0]
        message = replayed["payload"]["ai_messages"][0]["message"]
        assert message["message_id"] == "message-release-1"
        assert message["text_projection"] == "我先看核心承接，不急着追。"
        assert replayed["payload"]["ai_messages"][0]["text"] == "raw internal fallback"
        exchange.acknowledge("to-client", claimed)
