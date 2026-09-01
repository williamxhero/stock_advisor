from __future__ import annotations

from pathlib import Path

import pytest

from trading_memory_hub import MemoryHub, SourceIntegrityError


class ControlledSource:
    def __init__(self, body: str) -> None:
        self.body = body

    def hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        return {"title": "不可变公告", "body": self.body, "occurred_at": reference["date"] + "T00:00:00Z"}

    def health(self) -> dict[str, str]:
        return {"state": "ready"}


@pytest.mark.parametrize(
    "reference",
    [
        {"source_system": "markethub", "record_type": "stock_quote_1d", "date": "2026-08-28", "code": "600000"},
        {"source_system": "8815", "record_type": "cninfo_disclosure", "date": "2026-08-28", "code": "600000", "event_id": "notice-1"},
    ],
)
def test_immutable_source_is_searchable_without_copying_body_into_ledger(
    tmp_path: Path, reference: dict[str, str]
) -> None:
    source = ControlledSource("标题没有实体，但正文提到机器人订单显著增长。")
    hub = MemoryHub(tmp_path / "ledger.sqlite3", source_adapters={reference["source_system"]: source})
    receipt = hub.append(
        {
            "memory_space_id": "partner-main",
            "source_system": reference["source_system"],
            "source_event_id": "remember-" + reference["source_system"],
            "content_hash": "auto",
            "episode_type": "external_evidence",
            "source_reference": reference,
            "occurred_at": "2026-08-28T00:00:00Z",
            "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:00Z",
            "authority": "immutable_public_source",
            "protocol_version": "memoryhub/v1",
        }
    )
    snapshot = hub.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")

    cards = hub.search(snapshot.snapshot_id, "机器人")

    assert [item["episode_id"] for item in cards] == [receipt.episode_id]
    assert hub.expand(snapshot.snapshot_id, receipt.episode_id)["body"].endswith("订单显著增长。")


def test_expand_fails_closed_when_immutable_source_hash_changes(tmp_path: Path) -> None:
    source = ControlledSource("原始公告正文")
    hub = MemoryHub(tmp_path / "ledger.sqlite3", source_adapters={"8815": source})
    receipt = hub.append(
        {
            "memory_space_id": "partner-main", "source_system": "8815",
            "source_event_id": "notice-1", "content_hash": "auto",
            "episode_type": "external_evidence",
            "source_reference": {"source_system": "8815", "record_type": "cninfo_disclosure", "date": "2026-08-28", "code": "600000", "event_id": "notice-1"},
            "occurred_at": "2026-08-28T00:00:00Z", "known_at": "2026-08-31T01:00:00Z",
            "submitted_at": "2026-08-31T01:00:00Z", "authority": "immutable_public_source",
            "protocol_version": "memoryhub/v1",
        }
    )
    snapshot = hub.begin_snapshot("partner-main", as_of="2026-08-31T02:00:00Z", stage="chat")
    source.body = "来源异常改写后的正文"

    with pytest.raises(SourceIntegrityError):
        hub.expand(snapshot.snapshot_id, receipt.episode_id)
