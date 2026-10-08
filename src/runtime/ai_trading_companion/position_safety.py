"""Runtime-owned position truth and read-only advice qualification.

Provenance is bound by the calling Runtime seam, never by provider output.
A qualified suggestion is not permission to mutate facts or execute a trade.
"""
from __future__ import annotations

import copy
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .mandate_spec import canonical_json, sha256
from .trading_calendar import TradingCalendarUnavailable, XshgTradingCalendar

CONTRACT = "PositionSafetySpec/v1"
VERSION = 1
RESULT_CONTRACT = "PositionSafetyResult/v1"
REPLAY_CONTRACT = "PositionSafetyReplay/v1"
CANONICAL_RISK = {
    "version": 1, "leverage_allowed": False, "single_stock_max": .20,
    "same_theme_max": .40, "planned_loss_max": .01,
    "drawdown_review_threshold": .15, "freshness_trading_days": 1,
}
_PERMISSIONS = {"write_permissions": []}
_QUANTRESEARCH = {"access": "read_only", "write_permissions": []}
_SOURCES = {"runtime": "runtime_database", "broker": "broker_position_snapshot",
            "verified_user": "verified_user_statement"}
_STAGES = {"m0_compose", "m1_judgment", "m2", "chat", "reflection"}
_FORBIDDEN = {
    "write_positions", "mutate_positions", "position_write", "portfolio.write",
    "submit_trade", "trade.submit", "place_order", "order.place", "orders",
    "transactions", "write_permissions", "execute_trade", "broker_order",
}
# Free-form published advice has no mechanically checkable post-trade exposure
# or loss bound. It cannot stand in for the structured sizing proposal below.
_EXACT_ADVICE = re.compile(
    r"(?:买入|卖出|加仓|减仓|持有|配置|买|卖).{0,24}?(?:\d[\d,.]*|[一二三四五六七八九十百千]+)\s*(?:股|手|万元|元)|"
    r"(?:仓位|配置|敞口).{0,16}?(?:\d+(?:\.\d+)?\s*[%％成]|[一二三四五六七八九十]+成)|"
    r"(?:buy|sell|hold|allocate).{0,30}?\d[\d,.]*\s*(?:shares|lots|%|percent)|"
    r"(?:\d[\d,.]*|[一二三四五六七八九十百千]+)\s*(?:股|手|万元|元|[%％成]).{0,24}?(?:买入|卖出|加仓|减仓|持有|配置|买|卖)|"
    r"\d[\d,.]*\s*(?:shares|lots|%|percent).{0,30}?\b(?:buy|sell|hold|allocate)\b", re.I,
)


def advice_clauses(text: str) -> list[str]:
    """Bound action/negation checks to a proposition, not adjacent market facts."""
    return re.split(r"[。；;，\n!?！？]|(?<!\d),(?!\d)|但是|但|然而|而是|并且|而且|然后|"
                    r"也(?=不)|\b(?:but|however|instead|whereas|and|then)\b", text, flags=re.I)


def negated_advice(clause: str) -> bool:
    negative = re.search(r"不(?:认可|建议|应|能|要|使用|因|代表|等于|加仓|买入|卖出)|禁止|拒绝|避免|不得|不能|不可|"
                         r"\b(?:no|not|avoid(?:ing)?)\b", clause, re.I)
    action = re.search(r"买入|卖出|加仓|减仓|持有|配置|杠杆|融资|"
                       r"\b(?:buy(?:ing)?|sell(?:ing)?|hold|allocate|margin|leverage)\b", clause, re.I)
    # A later 'do not chase' cannot negate an earlier affirmative recommendation.
    return bool(negative and (action is None or negative.start() <= action.start()))


def _time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("position safety requires a valid fact clock") from exc
    if parsed.tzinfo is None:
        raise ValueError("position safety requires timezone-aware fact clocks")
    return parsed


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("position safety requires finite numeric facts")
    if value < 0 or positive and value == 0:
        raise ValueError("position safety requires nonnegative facts and positive assets/prices")
    return float(value)


def assert_operation(actor: str, operation: str) -> None:
    """No actor in this product can place orders; only Runtime records facts."""
    if operation in {"place_order", "submit_trade", "execute_trade"}:
        raise ValueError("real trading is user-decided and unavailable to this product")
    if operation not in {"read_positions", "advise", "record_verified_fact"}:
        raise ValueError("unsupported position operation")
    if operation == "record_verified_fact" and actor != "runtime":
        raise ValueError("only Runtime may record verified position facts")
    if actor not in {"runtime", "llm", "quantresearch"}:
        raise ValueError("untrusted position actor")


def assert_no_execution(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _FORBIDDEN and child not in (None, [], {}):
                raise ValueError("LLM position write/order attempt: " + normalized)
            if normalized in {"action", "action_type", "operation"} and str(child).casefold() in _FORBIDDEN:
                raise ValueError("LLM position write/order attempt")
            assert_no_execution(child)
    elif isinstance(value, list):
        for child in value:
            assert_no_execution(child)
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except ValueError:
            return
        assert_no_execution(decoded)


def latest_trading_day(as_of: str, calendar: Any | None = None) -> str | None:
    calendar = calendar or XshgTradingCalendar()
    day = _time(as_of).astimezone(ZoneInfo("Asia/Shanghai")).date()
    try:
        for _ in range(40):
            if calendar.is_trading_day(day):
                return day.isoformat()
            day -= timedelta(days=1)
    except (TradingCalendarUnavailable, ValueError, IndexError):
        return None
    return None


def build_input(
    snapshot: dict[str, Any], *, stage: str, as_of: str, source_ref: str,
    source: str = "runtime", verified: bool = True,
    latest_session: str | None = None, calendar: Any | None = None,
) -> dict[str, Any]:
    truth = {
        "positions": [{key: row.get(key) for key in ("code", "shares", "last_price", "updated_at")}
                      for row in snapshot.get("positions") or []],
        "total_assets": snapshot.get("total_assets"),
        "holdings_as_of": snapshot.get("holdings_as_of"),
        "assets_as_of": snapshot.get("assets_as_of"),
        "risk_state": copy.deepcopy(snapshot.get("risk_state") or {}),
    }
    value = {
        "contract": CONTRACT, "version": VERSION, "stage": stage, "as_of": as_of,
        "truth": truth, "latest_trading_day": latest_session if latest_session is not None else latest_trading_day(as_of, calendar),
        "risk_policy": copy.deepcopy(CANONICAL_RISK),
        "permissions": copy.deepcopy(_PERMISSIONS), "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {"source": source, "kind": _SOURCES.get(source), "verified": verified,
                       "source_ref": source_ref, "snapshot_sha256": sha256(truth)},
    }
    value["sha256"] = sha256(value)
    return validate_input(value)


def validate_input(value: dict[str, Any]) -> dict[str, Any]:
    fields = {"contract", "version", "stage", "as_of", "truth", "latest_trading_day", "risk_policy",
              "permissions", "quantresearch", "provenance", "sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("PositionSafetySpec input fields are not exact")
    if value["contract"] != CONTRACT or type(value["version"]) is not int or value["version"] != VERSION or value["stage"] not in _STAGES:
        raise ValueError("unsupported PositionSafetySpec identity")
    cutoff = _time(value["as_of"])
    provenance = value["provenance"]
    if (not isinstance(provenance, dict) or set(provenance) != {"source", "kind", "verified", "source_ref", "snapshot_sha256"}
            or provenance.get("source") not in _SOURCES or provenance.get("kind") != _SOURCES[provenance["source"]]
            or provenance.get("verified") is not True or not isinstance(provenance.get("source_ref"), str)
            or not provenance["source_ref"].strip() or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("snapshot_sha256")))
            or provenance["snapshot_sha256"] != sha256(value.get("truth"))):
        raise ValueError("untrusted position truth provenance")
    if value["risk_policy"] != CANONICAL_RISK or value["permissions"] != _PERMISSIONS or value["quantresearch"] != _QUANTRESEARCH:
        raise ValueError("position safety canonical risk/read_only boundary mismatch")
    truth = value["truth"]
    if not isinstance(truth, dict) or set(truth) != {"positions", "total_assets", "holdings_as_of", "assets_as_of", "risk_state"}:
        raise ValueError("position truth fields are not exact")
    if truth["total_assets"] is not None:
        _number(truth["total_assets"], positive=True)
    if not isinstance(truth["positions"], list):
        raise ValueError("positions must be a list")
    seen = set()
    for row in truth["positions"]:
        if not isinstance(row, dict) or set(row) != {"code", "shares", "last_price", "updated_at"}:
            raise ValueError("position fields are not exact")
        if not re.fullmatch(r"\d{6}", str(row["code"])) or row["code"] in seen or type(row["shares"]) is not int or row["shares"] < 0:
            raise ValueError("conflicting or invalid position facts")
        seen.add(row["code"])
        if row["last_price"] is not None:
            _number(row["last_price"], positive=True)
        if row["updated_at"] is not None:
            _time(row["updated_at"])
    for key in ("holdings_as_of", "assets_as_of"):
        if truth[key] is not None:
            _time(truth[key])
    state = truth["risk_state"]
    if not isinstance(state, dict) or set(state) - {"theme_by_code", "peak_assets", "synchronized", "review_completed"}:
        raise ValueError("invalid runtime risk state")
    if "peak_assets" in state:
        _number(state["peak_assets"], positive=True)
    for key in ("synchronized", "review_completed"):
        if key in state and type(state[key]) is not bool:
            raise ValueError("invalid runtime risk state flag")
    if "theme_by_code" in state and (not isinstance(state["theme_by_code"], dict) or any(
        not re.fullmatch(r"\d{6}", str(code)) or not isinstance(theme, str) or not theme.strip()
        for code, theme in state["theme_by_code"].items()
    )):
        raise ValueError("invalid runtime theme facts")
    day = value["latest_trading_day"]
    if day is not None and (date.fromisoformat(day) > cutoff.astimezone(ZoneInfo("Asia/Shanghai")).date()):
        raise ValueError("future trading session")
    if value["sha256"] != sha256({k: v for k, v in value.items() if k != "sha256"}):
        raise ValueError("position safety input digest mismatch")
    return value


def freshness_problems(value: dict[str, Any]) -> list[str]:
    validate_input(value)
    truth, day, cutoff = value["truth"], value["latest_trading_day"], _time(value["as_of"])
    problems = []
    if day is None:
        problems.append("trading_calendar_unavailable")
    if truth["total_assets"] is None:
        problems.append("total_assets_unknown")
    for field in ("holdings_as_of", "assets_as_of"):
        stamp = truth[field]
        if stamp is None or day is None or _time(stamp) > cutoff or _time(stamp).astimezone(ZoneInfo("Asia/Shanghai")).date() != date.fromisoformat(day):
            problems.append("stale_or_unknown_" + field)
    if any(row["updated_at"] is not None and _time(row["updated_at"]) > cutoff for row in truth["positions"]):
        problems.append("future_position_fact")
    return problems


def drawdown_blocked(truth: dict[str, Any]) -> bool:
    state = truth["risk_state"]
    return bool(state.get("synchronized") is True and state.get("peak_assets") and truth["total_assets"]
                and truth["total_assets"] <= state["peak_assets"] * (1 - CANONICAL_RISK["drawdown_review_threshold"])
                and state.get("review_completed") is not True)


def sizing_problems(value: dict[str, Any], proposal: dict[str, Any]) -> list[str]:
    """Compute exposures from Runtime facts, not model-supplied risk ratios."""
    problems = freshness_problems(value)
    if not isinstance(proposal, dict) or set(proposal) != {"code", "target_shares", "stop_price", "leverage"}:
        return [*problems, "structured_sizing_required"]
    if proposal["leverage"] is not False:
        problems.append("leverage_not_approved")
    shares = proposal["target_shares"]
    if type(shares) is not int or shares < 0:
        return [*problems, "invalid_target_shares"]
    code = proposal["code"]
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
        return [*problems, "invalid_instrument_code"]
    truth, state = value["truth"], value["truth"]["risk_state"]
    rows = {row["code"]: row for row in truth["positions"]}
    row = rows.get(code)
    themes = state.get("theme_by_code") or {}
    if row is None or row["last_price"] is None or not truth["total_assets"]:
        return [*problems, "sizing_valuation_unknown"]
    if any(item["last_price"] is None or item["code"] not in themes for item in rows.values()):
        return [*problems, "theme_exposure_unknown"]
    assets, price = truth["total_assets"], row["last_price"]
    target = shares * price
    others = [item for code, item in rows.items() if code != proposal["code"]]
    theme_value = target + sum(item["shares"] * item["last_price"] for item in others if themes[item["code"]] == themes[row["code"]])
    if target > assets * CANONICAL_RISK["single_stock_max"]:
        problems.append("single_stock_limit")
    if theme_value > assets * CANONICAL_RISK["same_theme_max"]:
        problems.append("same_theme_limit")
    if target + sum(item["shares"] * item["last_price"] for item in others) > assets:
        problems.append("leverage_not_approved")
    try:
        stop = _number(proposal["stop_price"])
        if stop >= price or shares * (price - stop) > assets * CANONICAL_RISK["planned_loss_max"]:
            problems.append("planned_loss_limit")
    except ValueError:
        problems.append("planned_loss_unknown")
    if shares > row["shares"]:
        if state.get("synchronized") is not True or not state.get("peak_assets"):
            problems.append("portfolio_drawdown_unknown")
        elif drawdown_blocked(truth):
            problems.append("drawdown_requires_review")
    if value["stage"] == "m0_compose":
        problems.append("m0_cannot_advise")
    return list(dict.fromkeys(problems))


def build_output(value: dict[str, Any], output: dict[str, Any], *, sizing: dict[str, Any] | None = None) -> dict[str, Any]:
    validate_input(value)
    if not isinstance(output, dict):
        raise ValueError("position safety output must be an object")
    problems: list[str] = []
    try:
        assert_no_execution(output)
    except ValueError as exc:
        problems.append(str(exc))
    if sizing is not None:
        problems.extend(sizing_problems(value, sizing))
    else:
        # Inspect the candidate's own prose, never source packets/user quotes.
        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, str) and any(_EXACT_ADVICE.search(clause) and not negated_advice(clause)
                                               for clause in advice_clauses(item)):
                problems.extend([*freshness_problems(value), "structured_sizing_required"])
        walk(output)
    def adds_risk(item: Any) -> bool:
        if isinstance(item, dict):
            return item.get("current_action") == "allow_add_risk" or item.get("action") == "allow_add_risk" or any(adds_risk(child) for child in item.values())
        return isinstance(item, list) and any(adds_risk(child) for child in item)
    if adds_risk(output) and drawdown_blocked(value["truth"]):
        problems.append("drawdown_requires_review")
    receipt = {
        "contract": RESULT_CONTRACT, "version": VERSION, "spec_contract": CONTRACT,
        "state": "refused" if problems else "qualified" if sizing is not None else "directional_only",
        "input": copy.deepcopy(value), "source_output": copy.deepcopy(output), "sizing": copy.deepcopy(sizing),
        "problems": list(dict.fromkeys(problems)), "permissions": copy.deepcopy(_PERMISSIONS),
        "quantresearch": copy.deepcopy(_QUANTRESEARCH),
        "provenance": {"source": "runtime", "input_sha256": value["sha256"], "output_sha256": sha256(output)},
    }
    receipt["sha256"] = sha256(receipt)
    return receipt


def validate_output(receipt: dict[str, Any]) -> dict[str, Any]:
    fields = {"contract", "version", "spec_contract", "state", "input", "source_output", "sizing", "problems",
              "permissions", "quantresearch", "provenance", "sha256"}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ValueError("PositionSafetyResult fields are not exact")
    expected = build_output(receipt["input"], receipt["source_output"], sizing=receipt["sizing"])
    if receipt != expected or type(receipt["version"]) is not int:
        raise ValueError("position safety receipt qualification/digest mismatch")
    return receipt


def frozen_replay(value: dict[str, Any], output: dict[str, Any], *, sizing: dict[str, Any] | None = None,
                  expected_output_sha256: str | None = None) -> dict[str, Any]:
    if expected_output_sha256 is not None and sha256(output) != expected_output_sha256:
        raise ValueError("position safety replay output digest mismatch")
    receipt = build_output(copy.deepcopy(value), copy.deepcopy(output), sizing=copy.deepcopy(sizing))
    return {"contract": REPLAY_CONTRACT, "source_input": copy.deepcopy(value),
            "source_output": copy.deepcopy(output), "source_output_sha256": sha256(output), "receipt": receipt,
            "qualification": {"valid": receipt["state"] != "refused", "state": receipt["state"], "read_only": True}}


def publication_receipt(packet: dict[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    value = packet.get("position_safety")
    if value is None:
        return None
    validate_input(value)
    if value["stage"] != packet.get("stage") or value["as_of"] != packet.get("as_of"):
        raise ValueError("position safety packet identity mismatch")
    return build_output(value, output)


def install_qualification() -> dict[str, Any]:
    at = "2026-09-21T01:45:00Z"
    snapshot = {"positions": [{"code": "603179", "shares": 100, "last_price": 10, "updated_at": at}],
                "total_assets": 100000, "holdings_as_of": at, "assets_as_of": at,
                "risk_state": {"theme_by_code": {"603179": "auto"}, "peak_assets": 100000, "synchronized": True}}
    value = build_input(snapshot, stage="m1_judgment", as_of=at, source_ref="install-runtime", latest_session="2026-09-21")
    sizing = {"code": "603179", "target_shares": 1000, "stop_price": 9, "leverage": False}
    output = {"text": "仅在条件确认后考虑扩大敞口；真实交易由用户决定。"}
    first, second = frozen_replay(value, output, sizing=sizing), frozen_replay(value, output, sizing=sizing)
    stale = copy.deepcopy(snapshot)
    stale["assets_as_of"] = "2026-09-17T01:45:00Z"
    stale_value = build_input(stale, stage="m1_judgment", as_of=at, source_ref="install-stale", latest_session="2026-09-21")
    checks = {"frozen_replay": first == second, "precise_qualified": first["qualification"]["valid"],
              "stale_assets_refused": build_output(stale_value, output, sizing=sizing)["state"] == "refused",
              "llm_order_refused": build_output(value, {"place_order": {"shares": 100}})["state"] == "refused",
              "quantresearch_read_only": value["quantresearch"] == _QUANTRESEARCH}
    for actor in ("llm", "quantresearch"):
        try:
            assert_operation(actor, "record_verified_fact")
        except ValueError:
            checks[actor + "_write_refused"] = True
        else:
            checks[actor + "_write_refused"] = False
    return {"contract": "PositionSafetyInstallQualification/v1", "qualified": all(checks.values()),
            "checks": checks, "replay_sha256": sha256(first), "evaluation_vector": {
                "delivery_speed": {"status": "not_measured", "reason": "deterministic offline qualification"},
                "qualification_probability": {"status": "not_measured", "reason": "no provider sampling"},
                "research_quality": {"status": "not_measured", "reason": "no research invocation"},
                "judgment_outcome": {"status": "not_measured", "reason": "no realized trade outcome"},
                "safety_reliability": {"status": "pass" if all(checks.values()) else "fail", "measurements": checks},
            }}


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
