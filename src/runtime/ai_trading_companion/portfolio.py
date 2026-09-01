from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from .store import now


STATE_TERMS = re.compile(r"买入|卖出|成交|加仓|减仓|清仓|持仓|成本|仓位|总资产|股票资产")
FUTURE_TERMS = re.compile(r"计划|准备|打算|考虑|建议|如果|若|明天|等到|满足.*后")


def is_portfolio_statement(text: str) -> bool:
    return bool(STATE_TERMS.search(text))


def is_future_action_statement(text: str) -> bool:
    action = re.search(r"买入|卖出|加仓|减仓|清仓", text)
    return bool(action and FUTURE_TERMS.search(text[:action.start()]))


class PortfolioService:
    """Owns factual portfolio events. Model output is treated only as an untrusted proposal."""

    def __init__(self, store_or_legacy_root: Any, store: Any | None = None) -> None:
        # A second argument is tolerated for upgrade compatibility; the first
        # value is never inspected and cannot become a factual input.
        self.store = store if store is not None else store_or_legacy_root
        self.store.initialize()

    def snapshot(self) -> dict[str, Any]:
        with self.store.connection() as connection:
            positions = [dict(row) for row in connection.execute(
                "SELECT * FROM portfolio_position WHERE shares>0 ORDER BY market_value DESC, code"
            )]
            transactions = [dict(row) for row in connection.execute(
                "SELECT * FROM portfolio_transaction ORDER BY created_at DESC LIMIT 20"
            )]
            pending = [dict(row) for row in connection.execute(
                "SELECT proposal_id,source_text,missing_fields_json,created_at FROM portfolio_change_proposal "
                "WHERE state='needs_input' ORDER BY created_at DESC"
            )]
            meta = {row[0]: row[1] for row in connection.execute("SELECT key,value FROM portfolio_meta")}
        total_assets = float(meta["total_assets"]) if meta.get("total_assets") else None
        for position in positions:
            position["weight"] = (float(position["market_value"]) / total_assets) if total_assets and position["market_value"] is not None else None
        return {
            "positions": positions,
            "transactions": transactions,
            "pending_proposals": [
                {**row, "missing_fields": json.loads(row.pop("missing_fields_json"))}
                for row in pending
            ],
            "total_assets": total_assets,
            "updated_at": max((item["updated_at"] for item in positions), default=meta.get("baseline_imported")),
        }

    def emit_snapshot(self) -> None:
        self.store.queue_portfolio_event("portfolio.snapshot.ready", self.snapshot())

    def record_job(self, source_artifact_id: str, cycle_id: str, text: str) -> str:
        job_id = str(uuid.uuid4())
        with self.store.connection() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO portfolio_interpretation_job VALUES(?,?,?,?, 'queued',?,NULL,NULL)",
                (job_id, source_artifact_id, cycle_id, text, now()),
            )
        if inserted.rowcount:
            self.store.queue_portfolio_event("portfolio.interpretation.started", {
                "source_artifact_id": source_artifact_id, "source_cycle_id": cycle_id,
            })
        return job_id

    def complete_job(self, source_artifact_id: str, extraction: dict[str, Any]) -> dict[str, Any]:
        with self.store.connection() as connection:
            job = connection.execute(
                "SELECT * FROM portfolio_interpretation_job WHERE source_artifact_id=?", (source_artifact_id,)
            ).fetchone()
        if not job:
            raise ValueError("unknown portfolio interpretation job")
        if job["state"] != "queued":
            return {"state": job["state"], "source_artifact_id": source_artifact_id}
        result = self.apply_extraction(
            job["source_text"], extraction, job["source_cycle_id"], source_artifact_id
        )
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE portfolio_interpretation_job SET state=?,completed_at=?,error=? WHERE source_artifact_id=?",
                (result["state"], now(), result.get("error"), source_artifact_id),
            )
        return result

    def fail_job(self, source_artifact_id: str, error: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE portfolio_interpretation_job SET state='failed',completed_at=?,error=? WHERE source_artifact_id=?",
                (now(), error[-2000:], source_artifact_id),
            )
        self.store.queue_portfolio_event("portfolio.change.rejected", {
            "source_artifact_id": source_artifact_id,
            "reason": "持仓识别失败；原判断已保存，持仓未修改。",
        })

    def apply_extraction(
        self, source_text: str, extraction: dict[str, Any], cycle_id: str | None,
        source_artifact_id: str | None,
    ) -> dict[str, Any]:
        proposals = extraction.get("changes") or []
        if is_future_action_statement(source_text) or extraction.get("statement_type") not in {"executed", "current_state"}:
            return self._record_non_action(source_text, extraction, cycle_id, source_artifact_id)
        if not proposals:
            return self._record_needs_input(source_text, extraction, cycle_id, source_artifact_id, ["成交信息"])

        resolved = []
        missing: set[str] = set()
        instruments = self._instrument_map()
        current = {row["code"]: row for row in self.snapshot()["positions"]}
        for change in proposals:
            raw_action = change.get("action")
            action = raw_action
            evidence = change.get("evidence") or {}
            if any(str(value) not in source_text for value in evidence.values() if value not in {None, ""}):
                missing.add("可追溯的原文证据")
            if raw_action == "asset_correction":
                try:
                    total_assets = float(change.get("total_assets")) if change.get("total_assets") is not None else None
                except (TypeError, ValueError):
                    total_assets = None
                if not total_assets or total_assets <= 0:
                    missing.add("总资产金额")
                if not evidence.get("total_assets") or not evidence.get("action"):
                    missing.add("总资产的原文证据")
                resolved.append({"action": "asset_correction", "total_assets": total_assets, "occurred_at": change.get("occurred_at") or now()})
                continue
            code = str(change.get("code") or "").strip()
            name = str(change.get("name") or "").strip()
            candidates = instruments.get(code, []) if code else instruments.get(name, [])
            if not candidates and re.fullmatch(r"\d{6}", code) and name and evidence.get("instrument"):
                candidates = [(code, name)]
            if len(candidates) != 1:
                missing.add("唯一股票代码或名称")
                continue
            code, canonical_name = candidates[0]
            shares = change.get("shares")
            if action == "sell_all":
                shares = current.get(code, {}).get("shares")
                action = "sell"
            try:
                shares = int(shares) if shares is not None else None
            except (TypeError, ValueError):
                shares = None
            try:
                price = float(change.get("price")) if change.get("price") is not None else None
            except (TypeError, ValueError):
                price = None
            if raw_action == "position_correction":
                try:
                    average_cost = float(change.get("average_cost")) if change.get("average_cost") is not None else None
                except (TypeError, ValueError):
                    average_cost = None
                    missing.add("有效成本价")
                if shares is None or shares < 0:
                    missing.add("当前持仓股数")
                if not evidence.get("instrument") or not evidence.get("shares") or not evidence.get("action"):
                    missing.add("持仓修正的原文证据")
                resolved.append({
                    "action": "position_correction", "code": code, "name": canonical_name,
                    "shares": shares, "price": price, "average_cost": average_cost,
                    "occurred_at": change.get("occurred_at") or now(),
                })
                continue
            if action not in {"buy", "sell"}:
                missing.add("买入或卖出方向")
            if not shares or shares <= 0:
                missing.add("成交股数")
            if not price or price <= 0:
                missing.add("成交价格")
            if action == "sell" and shares and shares > current.get(code, {}).get("shares", 0):
                missing.add("不超过当前持仓的卖出股数")
            if not evidence.get("instrument") or not evidence.get("action") or not evidence.get("price"):
                missing.add("可追溯的原文证据")
            if shares and raw_action != "sell_all" and not evidence.get("shares"):
                missing.add("股数的原文证据")
            resolved.append({
                "action": action, "code": code, "name": canonical_name,
                "shares": shares, "price": price,
                "occurred_at": change.get("occurred_at") or now(),
            })
        if missing:
            return self._record_needs_input(
                source_text, extraction, cycle_id, source_artifact_id, sorted(missing)
            )
        return self._commit_transactions(source_text, resolved, cycle_id, source_artifact_id)

    def replace_complete_snapshot(
        self, source_text: str, changes: list[dict[str, Any]], cycle_id: str | None,
        source_artifact_id: str | None,
    ) -> dict[str, Any]:
        """Atomically replace positions only when the user explicitly says the scope is complete."""
        completeness_markers = ("完整账户", "完整持仓", "全部持仓", "全量持仓", "这是全部", "以下为全部")
        if not any(marker in source_text for marker in completeness_markers):
            return self._record_needs_input(
                source_text, {"statement_type": "current_state", "changes": changes},
                cycle_id, source_artifact_id, ["明确的完整账户或全部持仓范围"],
            )
        current = {row["code"]: row for row in self.snapshot()["positions"]}
        resolved: list[dict[str, Any]] = []
        seen: set[str] = set()
        missing: set[str] = set()
        for change in changes:
            if change.get("action") == "asset_correction":
                try:
                    total_assets = float(change.get("total_assets"))
                except (TypeError, ValueError):
                    total_assets = 0
                if total_assets <= 0:
                    missing.add("总资产金额")
                else:
                    resolved.append({"action": "asset_correction", "total_assets": total_assets, "occurred_at": change.get("occurred_at") or now()})
                continue
            code = str(change.get("code") or "").strip()
            name = str(change.get("name") or "").strip()
            try:
                shares = int(change.get("shares"))
            except (TypeError, ValueError):
                shares = -1
            try:
                average_cost = float(change.get("average_cost")) if change.get("average_cost") is not None else None
                price = float(change.get("price")) if change.get("price") is not None else None
            except (TypeError, ValueError):
                average_cost, price = None, None
            evidence = change.get("evidence") or {}
            if not re.fullmatch(r"\d{6}", code) or not name or shares < 0:
                missing.add("每项持仓的代码、名称和非负股数")
                continue
            if code in seen:
                missing.add("不重复的股票代码")
                continue
            if not evidence.get("instrument") or not evidence.get("shares") or any(
                str(value) not in source_text for value in (evidence.get("instrument"), evidence.get("shares")) if value
            ):
                missing.add("每项持仓的原文证据")
            if shares > 0 and average_cost is None and price is None and code not in current:
                missing.add("新持仓的成本价或参考价")
            seen.add(code)
            old = current.get(code) or {}
            resolved.append({
                "action": "position_correction", "code": code, "name": name, "shares": shares,
                "price": price or old.get("last_price") or average_cost,
                "average_cost": average_cost if average_cost is not None else old.get("average_cost"),
                "occurred_at": change.get("occurred_at") or now(),
            })
        if missing:
            return self._record_needs_input(
                source_text, {"statement_type": "current_state", "changes": changes},
                cycle_id, source_artifact_id, sorted(missing),
            )
        for code, old in current.items():
            if code in seen:
                continue
            resolved.append({
                "action": "position_correction", "code": code, "name": old["name"], "shares": 0,
                "price": old.get("last_price") or old.get("average_cost") or 0,
                "average_cost": old.get("average_cost"), "occurred_at": now(),
            })
        if not resolved:
            return self._record_needs_input(
                source_text, {"statement_type": "current_state", "changes": changes},
                cycle_id, source_artifact_id, ["完整快照内容"],
            )
        result = self._commit_transactions(source_text, resolved, cycle_id, source_artifact_id)
        result["complete_snapshot"] = True
        return result

    def _instrument_map(self) -> dict[str, list[tuple[str, str]]]:
        result: dict[str, list[tuple[str, str]]] = {}
        for row in self.snapshot()["positions"]:
            pair = (row["code"], row["name"])
            for key in pair:
                result.setdefault(key, [])
                if pair not in result[key]:
                    result[key].append(pair)
        return result

    def _record_non_action(self, text: str, extraction: dict[str, Any], cycle_id: str | None, artifact_id: str | None) -> dict[str, Any]:
        proposal_id = self._insert_proposal(text, extraction, cycle_id, artifact_id, "rejected", [])
        payload = {"proposal_id": proposal_id, "source_artifact_id": artifact_id, "reason": "未作为真实成交处理"}
        self.store.queue_portfolio_event("portfolio.change.rejected", payload)
        return {"state": "rejected", **payload}

    def _record_needs_input(self, text: str, extraction: dict[str, Any], cycle_id: str | None, artifact_id: str | None, missing: list[str]) -> dict[str, Any]:
        proposal_id = self._insert_proposal(text, extraction, cycle_id, artifact_id, "needs_input", missing)
        payload = {"proposal_id": proposal_id, "source_artifact_id": artifact_id, "missing_fields": missing}
        self.store.queue_portfolio_event("portfolio.change.needs_input", payload)
        return {"state": "needs_input", **payload}

    def _insert_proposal(self, text: str, extraction: dict[str, Any], cycle_id: str | None, artifact_id: str | None, state: str, missing: list[str]) -> str:
        proposal_id = str(uuid.uuid4())
        with self.store.connection() as connection:
            connection.execute(
                "INSERT INTO portfolio_change_proposal VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                (proposal_id, artifact_id, cycle_id, text, json.dumps(extraction, ensure_ascii=False),
                 state, json.dumps(missing, ensure_ascii=False), now()),
            )
        return proposal_id

    def _commit_transactions(self, text: str, changes: list[dict[str, Any]], cycle_id: str | None, artifact_id: str | None) -> dict[str, Any]:
        group_seed = artifact_id or f"{cycle_id or ''}:{hashlib.sha256(text.encode()).hexdigest()}"
        action_group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"portfolio-action:{group_seed}"))
        transaction_ids: list[str] = []
        summaries: list[str] = []
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            positions = {row["code"]: dict(row) for row in connection.execute("SELECT * FROM portfolio_position")}
            for index, change in enumerate(changes):
                idempotency_key = f"{artifact_id or hashlib.sha256(text.encode()).hexdigest()}:{index}"
                existing = connection.execute(
                    "SELECT transaction_id FROM portfolio_transaction WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if existing:
                    transaction_ids.append(existing[0])
                    continue
                if change["action"] == "asset_correction":
                    transaction_id = str(uuid.uuid4())
                    prior_assets_row = connection.execute("SELECT value FROM portfolio_meta WHERE key='total_assets'").fetchone()
                    prior_assets = float(prior_assets_row[0]) if prior_assets_row else None
                    connection.execute(
                        "INSERT INTO portfolio_transaction(transaction_id,source_artifact_id,source_cycle_id,source_text,action,code,name,shares,price,position_before,position_after,occurred_at,created_at,reversal_of,reverted_by,idempotency_key) VALUES(?,?,?,?,?,'','总资产',0,?,?,?,?,?,NULL,NULL,?)",
                        (transaction_id, artifact_id, cycle_id, text, "asset_correction", change["total_assets"], int(prior_assets) if prior_assets is not None else None, int(change["total_assets"]), change["occurred_at"], now(), idempotency_key),
                    )
                    connection.execute(
                        "UPDATE portfolio_transaction SET action_group_id=?,before_json=?,after_json=? WHERE transaction_id=?",
                        (action_group_id, json.dumps({"total_assets": prior_assets}, ensure_ascii=False),
                         json.dumps({"total_assets": change["total_assets"]}, ensure_ascii=False), transaction_id),
                    )
                    connection.execute("INSERT INTO portfolio_meta(key,value) VALUES('total_assets',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(change["total_assets"]),))
                    transaction_ids.append(transaction_id)
                    summaries.append(f"总资产修正为 {change['total_assets']:,.2f}元")
                    continue
                old = positions.get(change["code"], {"shares": 0, "average_cost": None, "last_price": None, "revision": 0})
                old_shares = int(old["shares"])
                if change["action"] == "position_correction":
                    new_shares = int(change["shares"])
                    new_cost = float(change["average_cost"]) if change.get("average_cost") is not None else old.get("average_cost")
                    effective_price = change.get("price") or old.get("last_price") or new_cost
                    if effective_price is None:
                        raise ValueError("new position correction requires price or average cost")
                    change["price"] = float(effective_price)
                    signed = new_shares - old_shares
                elif change["action"] == "buy":
                    new_shares = old_shares + change["shares"]
                    old_cost = float(old["average_cost"] or 0)
                    new_cost = ((old_shares * old_cost) + (change["shares"] * change["price"])) / new_shares
                    signed = change["shares"]
                else:
                    new_shares = old_shares - change["shares"]
                    new_cost = old["average_cost"] if new_shares else None
                    signed = -change["shares"]
                transaction_id = str(uuid.uuid4())
                connection.execute(
                    "INSERT INTO portfolio_transaction(transaction_id,source_artifact_id,source_cycle_id,source_text,action,code,name,shares,price,position_before,position_after,occurred_at,created_at,reversal_of,reverted_by,idempotency_key) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?)",
                    (transaction_id, artifact_id, cycle_id, text, change["action"], change["code"],
                     change["name"], change["shares"], change["price"], old_shares, new_shares,
                     change["occurred_at"], now(), idempotency_key),
                )
                market_value = new_shares * change["price"]
                pnl = new_shares * (change["price"] - new_cost) if new_cost is not None else None
                connection.execute(
                    "INSERT INTO portfolio_position(code,name,shares,average_cost,last_price,price_as_of,market_value,unrealized_pnl,weight,updated_at,revision) "
                    "VALUES(?,?,?,?,?,?,?,?,NULL,?,1) ON CONFLICT(code) DO UPDATE SET name=excluded.name,shares=excluded.shares,"
                    "average_cost=excluded.average_cost,last_price=excluded.last_price,price_as_of=excluded.price_as_of,"
                    "market_value=excluded.market_value,unrealized_pnl=excluded.unrealized_pnl,updated_at=excluded.updated_at,revision=portfolio_position.revision+1",
                    (change["code"], change["name"], new_shares, new_cost, change["price"],
                     change["occurred_at"], market_value, pnl, change["occurred_at"]),
                )
                after_position = {
                    "code": change["code"], "name": change["name"], "shares": new_shares,
                    "average_cost": new_cost, "last_price": change["price"], "price_as_of": change["occurred_at"],
                    "market_value": market_value, "unrealized_pnl": pnl,
                }
                connection.execute(
                    "UPDATE portfolio_transaction SET action_group_id=?,before_json=?,after_json=? WHERE transaction_id=?",
                    (action_group_id, json.dumps({"position": old if old_shares or change["code"] in positions else None}, ensure_ascii=False, sort_keys=True),
                     json.dumps({"position": after_position}, ensure_ascii=False, sort_keys=True), transaction_id),
                )
                positions[change["code"]] = {**old, "shares": new_shares, "average_cost": new_cost}
                transaction_ids.append(transaction_id)
                if change["action"] == "position_correction":
                    summaries.append(f"{change['name']} 当前持仓修正为 {new_shares}股")
                else:
                    summaries.append(f"{change['name']} {'+' if signed > 0 else ''}{signed}股 @ {change['price']:g}")
        payload = {
            "source_artifact_id": artifact_id,
            "transaction_ids": transaction_ids,
            "summary": "；".join(summaries) or "持仓已更新",
            "projection_pending": False,
            "snapshot": self.snapshot(),
        }
        self.store.queue_portfolio_event("portfolio.change.applied", payload)
        return {"state": "applied", **payload}

    def reconcile(self) -> list[str]:
        """Retire projection intents left by pre-cutover builds without touching files."""
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE portfolio_render_intent SET state='retired',completed_at=?,"
                "error='file projections retired after MemoryHub cutover' WHERE state='pending'",
                (now(),),
            )
        return []

    def revert_latest(self) -> dict[str, Any]:
        with self.store.connection() as connection:
            grouped = connection.execute(
                """SELECT action_group_id FROM portfolio_transaction
                   WHERE reversal_of IS NULL AND reverted_by IS NULL AND before_json IS NOT NULL
                     AND action_group_id IS NOT NULL AND action!='reversal'
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        if grouped:
            return self._revert_action_group(str(grouped["action_group_id"]))
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM portfolio_transaction WHERE action IN ('buy','sell') AND reversal_of IS NULL AND reverted_by IS NULL ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise ValueError("没有可撤销的持仓更新")
        original = dict(row)
        inverse = "sell" if original["action"] == "buy" else "buy"
        result = self._commit_transactions(
            f"撤销 {original['transaction_id']}",
            [{"action": inverse, "code": original["code"], "name": original["name"],
              "shares": original["shares"], "price": original["price"], "occurred_at": now()}],
            original["source_cycle_id"], None,
        )
        reversal_id = result["transaction_ids"][-1]
        with self.store.connection() as connection:
            connection.execute("UPDATE portfolio_transaction SET reverted_by=? WHERE transaction_id=?", (reversal_id, original["transaction_id"]))
            connection.execute("UPDATE portfolio_transaction SET reversal_of=? WHERE transaction_id=?", (original["transaction_id"], reversal_id))
        payload = {"transaction_id": original["transaction_id"], "reversal_transaction_id": reversal_id, "snapshot": self.snapshot()}
        self.store.queue_portfolio_event("portfolio.change.reverted", payload)
        return payload

    def _revert_action_group(self, action_group_id: str) -> dict[str, Any]:
        reversal_group_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"portfolio-reversal:{action_group_id}"))
        reversal_ids: list[str] = []
        with self.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            originals = [dict(row) for row in connection.execute(
                """SELECT * FROM portfolio_transaction
                   WHERE action_group_id=? AND reversal_of IS NULL AND reverted_by IS NULL
                   ORDER BY created_at DESC,transaction_id DESC""",
                (action_group_id,),
            )]
            if not originals:
                raise ValueError("没有可撤销的持仓更新")
            for original in originals:
                before = json.loads(original["before_json"])
                if "total_assets" in before:
                    if before["total_assets"] is None:
                        connection.execute("DELETE FROM portfolio_meta WHERE key='total_assets'")
                    else:
                        connection.execute(
                            "INSERT INTO portfolio_meta(key,value) VALUES('total_assets',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (str(before["total_assets"]),),
                        )
                else:
                    position = before.get("position")
                    if position is None:
                        connection.execute("DELETE FROM portfolio_position WHERE code=?", (original["code"],))
                    else:
                        connection.execute(
                            """INSERT INTO portfolio_position(
                                 code,name,shares,average_cost,last_price,price_as_of,market_value,unrealized_pnl,weight,updated_at,revision)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?)
                               ON CONFLICT(code) DO UPDATE SET name=excluded.name,shares=excluded.shares,
                                 average_cost=excluded.average_cost,last_price=excluded.last_price,
                                 price_as_of=excluded.price_as_of,market_value=excluded.market_value,
                                 unrealized_pnl=excluded.unrealized_pnl,weight=excluded.weight,
                                 updated_at=excluded.updated_at,revision=portfolio_position.revision+1""",
                            (
                                position["code"], position["name"], int(position["shares"]), position.get("average_cost"),
                                position.get("last_price"), position.get("price_as_of"), position.get("market_value"),
                                position.get("unrealized_pnl"), position.get("weight"), now(), int(position.get("revision") or 1),
                            ),
                        )
                reversal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"reversal:{original['transaction_id']}"))
                connection.execute(
                    """INSERT OR IGNORE INTO portfolio_transaction(
                         transaction_id,source_artifact_id,source_cycle_id,source_text,action,code,name,shares,price,
                         position_before,position_after,occurred_at,created_at,reversal_of,reverted_by,idempotency_key,
                         action_group_id,before_json,after_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)""",
                    (
                        reversal_id, None, original["source_cycle_id"], f"撤销动作组 {action_group_id}", "reversal",
                        original["code"], original["name"], int(original["shares"]), float(original["price"]),
                        original["position_after"], original["position_before"], now(), now(), original["transaction_id"],
                        f"revert:{original['transaction_id']}", reversal_group_id, original["after_json"], original["before_json"],
                    ),
                )
                connection.execute("UPDATE portfolio_transaction SET reverted_by=? WHERE transaction_id=?", (reversal_id, original["transaction_id"]))
                reversal_ids.append(reversal_id)
        payload = {
            "transaction_id": originals[-1]["transaction_id"],
            "transaction_ids": [item["transaction_id"] for item in originals],
            "reversal_transaction_id": reversal_ids[-1], "reversal_transaction_ids": reversal_ids,
            "projection_pending": False, "snapshot": self.snapshot(),
        }
        self.store.queue_portfolio_event("portfolio.change.reverted", payload)
        return payload

    def cancel_pending(self, proposal_id: str) -> None:
        with self.store.connection() as connection:
            connection.execute(
                "UPDATE portfolio_change_proposal SET state='cancelled',resolved_at=? WHERE proposal_id=? AND state='needs_input'",
                (now(), proposal_id),
            )
        self.emit_snapshot()

def explicit_fixture_extraction(text: str) -> dict[str, Any]:
    """Offline fixture parser used only when the runtime is deliberately not executing Codex."""
    statement_type = "planned" if FUTURE_TERMS.search(text) else "executed"
    action_match = re.search(r"(买入|加仓|卖出|减仓|清仓|全部卖出)", text)
    instrument_match = re.search(r"(\d{6}|[\u4e00-\u9fff]{2,8}(?:股份|电器|电源|光电|钼业|办公)?)", text)
    shares_match = re.search(r"(\d+)\s*股", text)
    price_match = re.search(r"(?:价格|价|@|以)\s*([0-9]+(?:\.[0-9]+)?)\s*元?", text, re.IGNORECASE)
    if not price_match:
        price_match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*元", text)
    changes = []
    if action_match:
        raw_action = action_match.group(1)
        action = "sell_all" if raw_action in {"清仓", "全部卖出"} else "buy" if raw_action in {"买入", "加仓"} else "sell"
        instrument = instrument_match.group(1) if instrument_match else None
        changes.append({
            "action": action,
            "code": instrument if instrument and instrument.isdigit() else None,
            "name": instrument if instrument and not instrument.isdigit() else None,
            "shares": int(shares_match.group(1)) if shares_match else None,
            "price": float(price_match.group(1)) if price_match else None,
            "occurred_at": None,
            "evidence": {
                "instrument": instrument, "action": raw_action,
                "shares": shares_match.group(0) if shares_match else None,
                "price": price_match.group(0) if price_match else None,
            },
        })
    return {"statement_type": statement_type, "changes": changes}
