"""Opt-in acceptance: ordinary-chat retrieval against the deployed MemoryHub."""
from __future__ import annotations

import os
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from ai_trading_companion.__main__ import consume, flush, run_chat
from ai_trading_companion.broker_client import BrokerResponse
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.exchange import LocalExchange
from ai_trading_companion.memory_port import HttpMemoryAdapter
from ai_trading_companion.portfolio import PortfolioService
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.memory_evidence import MemoryEvidenceRegistrar
from ai_trading_companion.web_access_gateway import WebAccessGatewayClient
from ai_trading_companion.config import load_settings
from ai_trading_companion.paths import RuntimePaths


@unittest.skipUnless(
    os.environ.get("AI_TRADING_COMPANION_RUN_MEMORYHUB_INTEGRATION") == "1",
    "set AI_TRADING_COMPANION_RUN_MEMORYHUB_INTEGRATION=1 for the deployed-service acceptance",
)
class RealMemoryHubChatIntegrationTests(unittest.TestCase):
    def test_cross_source_receipts_are_visible_only_after_a_new_snapshot(self) -> None:
        memory = HttpMemoryAdapter(os.environ.get("MEMORYHUB_URL", "http://yosef-server:8820"))
        space = f"stock-advisor-issue-61-{uuid.uuid4()}"
        now = "2026-09-01T12:00:00Z"
        try:
            market = memory.append({
                "memory_space_id": space, "source_system": "markethub", "source_event_id": "market-600000-2026-08-28",
                "content_hash": "auto", "episode_type": "external_evidence",
                "source_reference": {"source_system": "markethub", "record_type": "stock_quote_1d", "date": "2026-08-28", "code": "600000"},
                "occurred_at": "2026-08-28T00:00:00Z", "known_at": now, "submitted_at": now,
                "authority": "immutable_source_reference", "protocol_version": "memoryhub/v1",
            })
            archive = memory.append({
                "memory_space_id": space, "source_system": "8815", "source_event_id": "cninfo-1225539198",
                "content_hash": "auto", "episode_type": "external_evidence",
                "source_reference": {"source_system": "8815", "record_type": "cninfo_disclosure", "date": "2026-09-01", "code": "600337", "event_id": "1225539198"},
                "occurred_at": "2026-09-01T00:00:00Z", "known_at": now, "submitted_at": now,
                "authority": "immutable_source_reference", "protocol_version": "memoryhub/v1",
            })
            snapshot = memory.begin_snapshot({"memory_space_id": space, "as_of": now, "stage": "chat", "cycle_id": "issue-61"})
            self.assertTrue(memory.expand(snapshot["snapshot_id"], market["episode_id"])["body"])
            self.assertTrue(memory.expand(snapshot["snapshot_id"], archive["episode_id"])["body"])
            gateway = WebAccessGatewayClient(load_settings(RuntimePaths.discover().home).research)
            row = next(item for item in gateway.search("A股 市场风险", "news")["results"] if item.get("url") and item.get("excerpt_text"))
            web = MemoryEvidenceRegistrar(memory, clock=lambda: now).register_web_snapshot(
                memory_space_id=space, source_event_id="wag-fixed-issue-61", url=row["url"], title=row.get("title", ""),
                body=row["excerpt_text"], occurred_at=row.get("published_at") or row.get("fact_as_of") or now,
            )
            self.assertTrue(web.episode_id)
            self.assertEqual(web.episode_id, web.context["memory_episode_id"])
        finally:
            exported = memory.export_space(space)
            prepared = memory.prepare_clear(space, exported["export_sha256"])
            memory.clear_space(space, prepared["confirmation_token"])
            self.assertEqual([], memory.timeline(space))

    def test_real_memoryhub_chat_projects_only_the_final_reply_to_exchange(self) -> None:
        memory = HttpMemoryAdapter(os.environ.get("MEMORYHUB_URL", "http://yosef-server:8820"))
        space = f"stock-advisor-issue-60-{uuid.uuid4()}"
        timestamp = "2026-09-01T00:00:00Z"
        try:
            health = memory.health()
            self.assertEqual("memoryhub/v1", health["protocol_version"])
            self.assertEqual("ready", health["ledger"]["state"])
            memory.append({
                "memory_space_id": space, "source_system": "stock-advisor-acceptance",
                "source_event_id": "fixed-semiconductor-lesson-v1", "content_hash": "auto",
                "episode_type": "note", "body": "Fixed semiconductor drawdown lesson for Issue 60 acceptance.",
                "occurred_at": timestamp, "known_at": timestamp, "submitted_at": timestamp,
                "authority": "acceptance_fixture", "protocol_version": "memoryhub/v1",
            })

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CompanionStore(root / "runtime.sqlite3")
                engine = CompanionEngine(store, memory=memory, memory_space_id=space)
                exchange = LocalExchange(root / "exchange")
                portfolio = PortfolioService(root, store)
                conversation = store.ensure_daily_conversation("2026-09-01")
                message = store.stage_message(
                    conversation["cycle_id"], "Recall semiconductor risk lessons.", "conversation", message_id="chat",
                )
                batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
                store.append_artifact(
                    conversation["cycle_id"], "chat_human", "human", message["body_text"], conversation["as_of"], {"batch_id": batch_id},
                )
                broker = Mock()
                broker.invoke.side_effect = [
                    _response({"operation": "search", "query": "semiconductor", "episode_id": None}),
                    _response({"operation": "expand", "query": None, "episode_id": None}),
                    _response({"operation": "complete", "query": None, "episode_id": None}),
                    _response({
                        "answer": {"points": ["The semiconductor drawdown lesson is still relevant."], "material_ids": []},
                        "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": [],
                    }),
                ]
                # The expansion id is only available after the real snapshot search.
                def invoke(request: object) -> BrokerResponse:
                    if len(broker.invoke.mock_calls) == 1:
                        return _response({"operation": "search", "query": "semiconductor", "episode_id": None})
                    if len(broker.invoke.mock_calls) == 2:
                        state = request.packet["research_state"]
                        return _response({"operation": "expand", "query": None, "episode_id": state["known_episode_ids"][0]})
                    if len(broker.invoke.mock_calls) == 3:
                        return _response({"operation": "complete", "query": None, "episode_id": None})
                    return _response({"answer": {"points": ["The semiconductor drawdown lesson is still relevant."], "material_ids": []}, "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": []})
                broker.invoke.side_effect = invoke

                with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=broker):
                    result = run_chat(engine, store, portfolio, conversation["cycle_id"], batch_id, True)
                flush(store, exchange)
                events = [value for _path, value in exchange.receive("to-client")]

            self.assertEqual(["The semiconductor drawdown lesson is still relevant."], result["answer"]["points"])
            self.assertTrue(any(event["type"] == "chat.ready" for event in events))
            self.assertNotIn("search", json.dumps(events, ensure_ascii=False).lower())
        finally:
            exported = memory.export_space(space)
            prepared = memory.prepare_clear(space, exported["export_sha256"])
            cleared = memory.clear_space(space, prepared["confirmation_token"])
            self.assertEqual("cleared", cleared["state"])
            self.assertEqual([], memory.timeline(space))

    def test_real_runtime_exchange_control_reuses_snapshot_and_hides_h0(self) -> None:
        """#64: deployed service plus Runtime/Exchange terminate→continue acceptance."""
        memory = HttpMemoryAdapter(os.environ.get("MEMORYHUB_URL", "http://yosef-server:8820"))
        space = f"stock-advisor-issue-64-{uuid.uuid4()}"
        other_space = f"stock-advisor-issue-64-isolated-{uuid.uuid4()}"
        timestamp = "2026-09-01T12:00:00Z"
        try:
            prior = memory.append({
                "memory_space_id": space, "source_system": "stock-advisor-acceptance", "source_event_id": "prior",
                "content_hash": "auto", "episode_type": "note", "body": "prior qualified market evidence",
                "occurred_at": timestamp, "known_at": timestamp, "submitted_at": timestamp,
                "authority": "acceptance_fixture", "protocol_version": "memoryhub/v1",
                "metadata": {"cycle_id": "formal-cycle", "stage": "m0_research", "actor": "ai"},
            })
            h0 = memory.append({
                "memory_space_id": space, "source_system": "stock-advisor-acceptance", "source_event_id": "h0",
                "content_hash": "auto", "episode_type": "user_message", "body": "current cycle H0 must stay isolated",
                "occurred_at": timestamp, "known_at": timestamp, "submitted_at": timestamp,
                "authority": "acceptance_fixture", "protocol_version": "memoryhub/v1",
                "metadata": {"cycle_id": "formal-cycle", "stage": "h0", "actor": "human"},
            })
            m0 = memory.begin_snapshot({"memory_space_id": space, "as_of": timestamp, "stage": "m0_research", "cycle_id": "formal-cycle"})
            m1 = memory.begin_snapshot({"memory_space_id": space, "as_of": timestamp, "stage": "m1_research", "cycle_id": "formal-cycle"})
            self.assertIn(prior["episode_id"], {item["episode_id"] for item in memory.search(m0["snapshot_id"], "qualified")})
            self.assertNotIn(h0["episode_id"], {item["episode_id"] for item in memory.search(m1["snapshot_id"], "H0")})
            isolated = memory.begin_snapshot({"memory_space_id": other_space, "as_of": timestamp, "stage": "chat", "cycle_id": "other"})
            self.assertEqual([], memory.search(isolated["snapshot_id"], "qualified"))
            with self.assertRaisesRegex(Exception, "MemoryHub rejected"):
                memory.expand(m1["snapshot_id"], "does-not-exist")

            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = CompanionStore(root / "runtime.sqlite3")
                engine = CompanionEngine(store, memory=memory, memory_space_id=space)
                exchange = LocalExchange(root / "exchange")
                portfolio = PortfolioService(root, store)
                conversation = store.ensure_daily_conversation("2026-09-01")
                message = store.stage_message(conversation["cycle_id"], "Recall qualified market evidence.", "conversation", message_id="chat")
                batch_id, _ = store.commit_staged_messages(conversation["cycle_id"], "conversation")
                source = store.append_artifact(conversation["cycle_id"], "chat_human", "human", message["body_text"], conversation["as_of"], {"batch_id": batch_id})
                first_broker = Mock()
                first_broker.invoke.return_value = _response({"operation": "search", "query": "qualified", "episode_id": None})
                stopped = False

                def stop_from_exchange() -> bool:
                    nonlocal stopped
                    if first_broker.invoke.call_count and not stopped:
                        stopped = True
                        exchange.send("to-runtime", "stop", {
                            "contract": "companion-user-command/v1", "command_id": "stop",
                            "cycle_id": conversation["cycle_id"], "type": "terminate_chat_research",
                        })
                        consume(engine, store, exchange, portfolio, True)
                    return store.chat_research_terminated(conversation["cycle_id"])

                with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=first_broker):
                    stopped_result = run_chat(engine, store, portfolio, conversation["cycle_id"], batch_id, True, cancelled=stop_from_exchange)
                self.assertEqual("terminated", stopped_result["state"])
                self.assertEqual(1, first_broker.invoke.call_count)
                checkpoint = store.resumed_chat_research_checkpoint(conversation["cycle_id"], source["artifact_id"])
                self.assertIsNone(checkpoint)

                resumed_broker = Mock()
                resumed_broker.invoke.side_effect = [
                    _response({"operation": "complete", "query": None, "episode_id": None}),
                    _response({"answer": {"points": ["Qualified evidence is reused after the pause."], "material_ids": []}, "needs_fresh_search": False, "public_search_request": None, "propositions": [], "actions": []}),
                ]
                exchange.send("to-runtime", "continue", {
                    "contract": "companion-user-command/v1", "command_id": "continue",
                    "cycle_id": conversation["cycle_id"], "type": "continue_chat_research",
                })
                with patch("ai_trading_companion.__main__.ProviderBrokerClient", return_value=resumed_broker):
                    resumed = consume(engine, store, exchange, portfolio, True)
                self.assertEqual("resumed", resumed[0]["state"])
                self.assertEqual(["chat_research", "chat"], [call.args[0].stage for call in resumed_broker.invoke.call_args_list])
                events = [value for _path, value in exchange.receive("to-client")]
                self.assertTrue(any(event["type"] == "chat.research.terminated" for event in events))
                self.assertTrue(any(event["type"] == "chat.research.continued" for event in events))
                self.assertTrue(any(event["type"] == "chat.ready" for event in events))
        finally:
            for candidate in (space, other_space):
                exported = memory.export_space(candidate)
                prepared = memory.prepare_clear(candidate, exported["export_sha256"])
                memory.clear_space(candidate, prepared["confirmation_token"])
                self.assertEqual([], memory.timeline(candidate))


def _response(result: dict[str, object]) -> BrokerResponse:
    return BrokerResponse(
        output_text=json.dumps(result), result=result, actual_model="acceptance", provider="controlled",
        intellect="smart", fulfilled_intellect="smart", request_id="acceptance",
    )


if __name__ == "__main__":
    unittest.main()
