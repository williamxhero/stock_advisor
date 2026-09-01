from __future__ import annotations

import unittest

from ai_trading_companion.message_presentation import MessageQualificationError, present_message


class MessagePresentationTests(unittest.TestCase):
    def test_unicode_internal_fields_and_bare_report_labels_cannot_reach_speech(self):
        presented = present_message(
            """盘前研判
time_scope: next_trading_session
reference_at: 2026-09–01 09:00 Asia/Shanghai
Protocol: OpportunityDiscovery-v1.3
状态：unqualified

结论
现在还不能下结论。
市场基线
指数偏弱。
新增事件
昨夜有一条消息。""",
            as_of="2026-09-01T01:00:00Z",
            kind="ai_chat",
        )

        for leaked in (
            "time_scope", "next_trading_session", "reference_at", "Asia/Shanghai",
            "Protocol", "OpportunityDiscovery-v1.3", "unqualified", "盘前研判",
            "市场基线", "新增事件",
        ):
            self.assertNotIn(leaked, presented.markdown)
        self.assertNotRegex(presented.markdown, r"(?m)^结论\s*$")
        self.assertIn("下一个交易日", presented.markdown)
        self.assertIn("现有信息还不够，我先不下判断。", presented.markdown)
        self.assertEqual("passed", presented.metadata()["qualification"]["state"])

    def test_unknown_machine_field_fails_closed_instead_of_being_rewritten(self):
        with self.assertRaises(MessageQualificationError):
            present_message(
                "internal_flag: active\n我倾向于先等承接确认。",
                as_of="2026-09-01T01:00:00Z",
                kind="ai_chat",
            )

    def test_dates_are_spoken_relative_to_the_conversation(self):
        today = present_message(
            "数据截至2026-09-01 09:00。",
            as_of="2026-09-01T01:00:00Z",
            kind="ai_chat",
        )
        tomorrow = present_message(
            "我会在2026-09-02再看。",
            as_of="2026-09-01T01:00:00Z",
            kind="ai_chat",
        )
        monday = present_message(
            "我会在2026-09-07再看。",
            as_of="2026-09-04T01:00:00Z",
            kind="ai_chat",
        )

        self.assertEqual("数据截至今天早上九点。", today.markdown)
        self.assertEqual("我会在明天再看。", tomorrow.markdown)
        self.assertEqual("我会在周一再看。", monday.markdown)

    def test_dates_use_shanghai_context_across_utc_midnight_and_year_boundary(self):
        shanghai_today = present_message(
            "数据截至2027-01-01 09:00。",
            as_of="2026-12-31T16:30:00Z", kind="ai_chat",
        )
        historical = present_message(
            "我在2026-12-31记录过一次。",
            as_of="2027-01-01T01:00:00Z", kind="ai_chat",
        )

        self.assertIn("今天", shanghai_today.markdown)
        self.assertIn("昨天", historical.markdown)
        self.assertNotIn("2027-01-01", shanghai_today.markdown)

    def test_presentation_exposes_a_versioned_message_contract(self):
        presented = present_message(
            "我倾向于先等承接确认。",
            as_of="2026-09-01T01:00:00Z",
            kind="ai_chat",
        )

        self.assertEqual(2, presented.contract_version)
        self.assertRegex(presented.message()["message_id"], r"^[0-9a-f-]{36}$")
        self.assertTrue(presented.message()["sealed_at"].endswith("Z"))
        self.assertEqual(
            {
                "contract": "companion-published-message/v2",
                "message_id": presented.message()["message_id"],
                "sealed_at": presented.message()["sealed_at"],
                "kind": "ai_chat",
                "parts": [{"kind": "speech", "text": "我倾向于先等承接确认。"}],
                "text_projection": "我倾向于先等承接确认。",
            },
            presented.message(),
        )
        self.assertEqual(
            presented.message(),
            presented.metadata()["published_message"],
        )

    def test_own_message_is_released_as_natural_conversation(self):
        presented = present_message(
            "##盘前结论\ntime_scope: next_trading_session。 截至2026-08-31\n\n- 不预设反包\n- 不追高开",
            as_of="2026-08-31T01:00:00Z",
            kind="ai_chat",
        )

        self.assertNotIn("##", presented.markdown)
        self.assertNotIn("time_scope", presented.markdown)
        self.assertNotIn("next_trading_session", presented.markdown)
        self.assertNotIn("2026-08-31", presented.markdown)
        self.assertNotIn("\n- ", presented.markdown)
        self.assertIn("下一个交易日", presented.markdown)
        self.assertEqual("speech", presented.parts[0]["kind"])

    def test_explicit_format_request_keeps_the_requested_list(self):
        presented = present_message(
            "- 风险一是承接不足\n- 风险二是量能回落",
            as_of="2026-08-31T01:00:00Z",
            kind="ai_chat",
            allow_structured_format=True,
        )

        self.assertIn("- 风险一是承接不足", presented.markdown)

    def test_attributed_material_keeps_markdown_without_leaking_into_speech(self):
        presented = present_message(
            "我倾向于先等承接确认。 [[material:notice-1]]",
            as_of="2026-08-31T01:00:00Z",
            kind="ai_chat",
            material_registry={"notice-1": {
                "title": "公告原文", "url": "https://example.com/notice",
                "markdown": "> 公告原文：\n> - 事项仍在推进\n> - 结果存在不确定性\n> [来源](https://example.com/notice)",
            }},
        )

        self.assertEqual("speech", presented.parts[0]["kind"])
        self.assertEqual("material", presented.parts[1]["kind"])
        self.assertEqual("notice-1", presented.parts[1]["material_id"])
        self.assertEqual("https://example.com/notice", presented.parts[1]["source_url"])
        self.assertEqual("公告原文", presented.parts[1]["source_title"])
        self.assertIn("> - 事项仍在推进", presented.markdown)

    def test_long_material_becomes_a_link_instead_of_a_truncated_dump(self):
        presented = present_message(
            "我先说和判断有关的部分。 [[material:long-1]]",
            as_of="2026-09-01T01:00:00Z",
            kind="ai_chat",
            material_registry={"long-1": {
                "title": "公告原文", "url": "https://example.com/long",
                "markdown": "> 公告原文：\n> " + ("很长的原文。" * 300) + "\n> [查看原文](https://example.com/long)",
            }},
        )

        material = presented.parts[1]
        self.assertEqual("material", material["kind"])
        self.assertEqual("long-1", material["material_id"])
        self.assertEqual("https://example.com/long", material["source_url"])
        self.assertLess(len(material["markdown"]), 120)
        self.assertEqual("[查看公告原文](https://example.com/long)", material["markdown"])

    def test_unattributed_quote_does_not_get_material_format_privilege(self):
        presented = present_message(
            "我不认可这个说法。\n\n> - 这是没有来源的清单",
            as_of="2026-08-31T01:00:00Z", kind="ai_chat",
        )
        self.assertEqual(["speech"], [part["kind"] for part in presented.parts])
        self.assertNotIn("> -", presented.markdown)

    def test_unknown_material_id_and_forged_url_fail_closed(self):
        with self.assertRaisesRegex(MessageQualificationError, "unknown_material_id"):
            present_message(
                "我先看原文。 [[material:forged]]",
                as_of="2026-08-31T01:00:00Z", kind="ai_chat",
                material_registry={"real": {"title": "真实材料", "url": "https://example.com/real", "markdown": "> 真实原文"}},
            )
        with self.assertRaisesRegex(MessageQualificationError, "unregistered_material"):
            present_message(
                "我先看原文。\n\n> [伪造来源](https://evil.example/forged)",
                as_of="2026-08-31T01:00:00Z", kind="ai_chat",
            )

    def test_unattributed_code_block_is_spoken_as_companion_text(self):
        presented = present_message(
            "```\ntask_key: daily.execution.0945\n```",
            as_of="2026-08-31T01:00:00Z",
            kind="ai_chat",
        )

        self.assertNotIn("```", presented.markdown)
        self.assertNotIn("task_key", presented.markdown)
        self.assertNotIn("daily.execution.0945", presented.markdown)


if __name__ == "__main__":
    unittest.main()
