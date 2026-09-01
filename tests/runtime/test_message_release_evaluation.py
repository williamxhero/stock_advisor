from __future__ import annotations

import unittest
import json
from pathlib import Path

from ai_trading_companion.message_presentation import present_message


class MessageReleaseEvaluationTests(unittest.TestCase):
    def test_every_registered_message_kind_passes_the_eight_release_dimensions(self):
        registry = json.loads((Path(__file__).parents[2] / "resources/contracts/companion-message-publication-registry-v2.json").read_text(encoding="utf-8"))
        for kind in registry["published_kinds"]:
            message = present_message(
                "我现在还不能把不确定性说成结论，接下来先看核心承接。",
                as_of="2026-09-01T01:00:00Z", kind=kind,
            )
            visible = message.markdown
            dimensions = {
                "naturalness": "_" not in visible,
                "clarity": bool(visible),
                "professional_fidelity": "承接" in visible,
                "uncertainty": "不能" in visible,
                "independence": "先看" in visible,
                "persona_continuity": "我" in visible,
                "format_restraint": not any(token in visible for token in ("##", "\n- ", " | ")),
                "material_attribution": all(part["kind"] == "speech" for part in message.parts),
            }
            self.assertTrue(all(dimensions.values()), (kind, dimensions))
    def test_frozen_real_world_counterexamples_pass_each_release_dimension(self):
        cases = (
            "盘前研判\ntime_scope: next_trading_session\nreference_at: 2026-09–01 09:00 Asia/Shanghai\nProtocol: OpportunityDiscovery-v1.3\n状态：unqualified\n结论\n现有证据还不够。",
            "## 市场基线\n- 指数偏弱\n- 核心承接不足\n\n## 新增事件\n暂时没有改变判断的新消息。",
            "方向 | 盘前状态 | 当前处理\n成长与科技 | 分化偏弱 | 不预设反包，不追高开",
        )
        forbidden = (
            "time_scope", "next_trading_session", "reference_at", "Asia/Shanghai",
            "Protocol", "OpportunityDiscovery-v1.3", "unqualified", "##", "\n- ", " | ",
        )
        results = []
        for raw in cases:
            message = present_message(raw, as_of="2026-09-01T01:00:00Z", kind="ai_chat")
            visible = message.message()["text_projection"]
            results.append({
                "naturalness": not any(token in visible for token in forbidden),
                "clarity": bool(visible.strip()),
                "professional_fidelity": any(token in visible for token in ("证据", "指数", "承接", "成长与科技")),
                "uncertainty": "证据还不够" not in raw or "先不下判断" in visible,
                "independence": "不预设反包" not in raw or "不预设反包" in visible,
                "persona_continuity": not any(token in visible for token in ("M0", "M1", "M2", "策略分析师", "秘书")),
                "format_restraint": not any(token in visible for token in ("##", "\n- ", " | ")),
                "material_attribution": all(part["kind"] == "speech" for part in message.parts),
            })

        for result in results:
            self.assertTrue(all(result.values()), result)

    def test_sourced_material_remains_attributed_and_separate_from_speech(self):
        message = present_message(
            "我先说判断：这条消息还不足以改变原来的谨慎。 [[material:notice]]",
            as_of="2026-09-01T01:00:00Z", kind="ai_chat",
            material_registry={"notice": {
                "title": "公告原文", "url": "https://example.com/notice",
                "markdown": "> 公告原文：\n> - 项目仍在推进\n> [来源](https://example.com/notice)",
            }},
        )

        self.assertEqual(["speech", "material"], [part["kind"] for part in message.parts])
        self.assertEqual("https://example.com/notice", message.parts[1]["source_url"])


if __name__ == "__main__":
    unittest.main()
