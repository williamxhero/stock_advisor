from __future__ import annotations

from ai_trading_companion.router import CognitiveRouter
from ai_trading_companion.stage_expression import normalize_stage_output


def _semantic() -> dict:
    return {
        "summary": "收盘判断中性偏谨慎；当前五只持仓为新大陆、中宠股份、力星股份、紫金矿业和白云电器。",
        "direction": "neutral",
        "qualified": True,
        "horizon": "未来1至2个交易日",
        "current_action": "observe",
        "key_evidence": [
            "截至9月4日15:00，腾讯收盘行情显示三大指数均收跌，上涨2225家、下跌2794家。",
            "来源为上交所和深交所日度概况：成交额20335.82亿元，前一交易日17606.92亿元，增加2728.90亿元。",
            "截至收盘，东方财富板块行情显示畜禽饲料领涨、玻纤制造领跌，核心标的同步分化。",
        ],
        "transition_conditions": [
            {
                "outcome": "upgrade",
                "price": "三大指数重新站上本日收盘位",
                "breadth": "上涨家数持续超过下跌家数",
                "persistence": "至少连续2个交易日收盘确认",
            },
            {
                "outcome": "downgrade",
                "price": "主要指数继续跌破本日收盘位",
                "breadth": "下跌家数继续显著多于上涨家数",
                "persistence": "至少连续2个交易日或单日加速",
            },
        ],
        "position_focus": [
            {"symbol": "603861", "priority": 1, "reason": "当前市值暴露最大且相对偏弱", "action": "observe"},
            {"symbol": "300421", "priority": 2, "reason": "收盘表现显著弱于指数", "action": "reduce_risk"},
        ],
        "risks": ["负广度若延续，指数跌幅可能低估个股风险。"],
        "unknowns": ["未取得直接论坛传播数据，市场情绪以收盘广度和涨跌停候选作为替代证据。"],
    }


def _packet() -> dict:
    return {
        "task_key": "daily.review.1520",
        "task_profile": {
            "evidence_family": "completed_close",
            "analysis": {
                "goal": "覆盖成交额比较、板块题材、论坛情绪、当前账户全部证券持仓，并标注每项来源和资料时点。",
            },
        },
        "business_context": {
            "private_context_before_h0": {
                "positions": [
                    {"code": "000997", "name": "新大陆", "shares": 100},
                    {"code": "002891", "name": "中宠股份", "shares": 100},
                    {"code": "300421", "name": "力星股份", "shares": 200},
                    {"code": "601899", "name": "紫金矿业", "shares": 100},
                    {"code": "603861", "name": "白云电器", "shares": 700},
                    {"code": "000070", "name": "特发信息", "shares": 0},
                ],
            },
        },
    }


def test_v4_expression_keeps_key_evidence_when_summary_is_present() -> None:
    normalized = normalize_stage_output("m1_judgment", {"result_version": 4, "semantic": _semantic()})

    assert "20335.82亿元" in normalized.text
    assert "畜禽饲料领涨" in normalized.text
    assert normalized.text.index("收盘判断中性偏谨慎") < normalized.text.index("20335.82亿元")


def test_completed_close_verifier_enforces_requested_visible_coverage() -> None:
    accepted = CognitiveRouter().verify(
        "m1_judgment", _packet(), {"result_version": 4, "semantic": _semantic()},
    )
    incomplete = _semantic()
    incomplete["summary"] = "收盘判断中性偏谨慎。"
    incomplete["key_evidence"] = ["成交额显著放大，但市场广度偏弱。"]
    incomplete["unknowns"] = []
    rejected = CognitiveRouter().verify(
        "m1_judgment", _packet(), {"result_version": 4, "semantic": incomplete},
    )

    assert accepted["passed"], accepted["problems"]
    assert not rejected["passed"]
    assert "close_review_lacks_numeric_turnover_comparison" in rejected["problems"]
    assert "close_review_lacks_theme_leaders_and_laggards" in rejected["problems"]
    assert "close_review_lacks_forum_or_sentiment_substitute" in rejected["problems"]
    assert "close_review_lacks_requested_source_attribution" in rejected["problems"]
    assert "close_review_lacks_requested_fact_timing" in rejected["problems"]
    assert "close_review_omits_active_position:601899" in rejected["problems"]
