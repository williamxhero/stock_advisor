"""Gate for a future OpenAI-compatible embedding adapter.

It intentionally does not make a network request: cloud embedding is enabled
only after a retrieval evaluation proves semantic misses are material.  Keeping
this gate concrete prevents an accidental configuration flip from exporting
historical text without a full secret scan.
"""
from __future__ import annotations

from typing import Any

from .config import RuntimeSettings
from .secret_guard import find_secrets


class CloudEmbeddingGate:
    def __init__(self, store: Any, settings: RuntimeSettings) -> None:
        self.store=store; self.settings=settings

    def readiness(self, *, semantic_miss_cases: int, measured_material_gain: bool) -> dict[str, Any]:
        if not self.settings.cloud_embedding_enabled:
            return {"enabled":False,"ready":False,"reason":"本机 embedding 配置未启用"}
        if semantic_miss_cases < 50 or not measured_material_gain:
            return {"enabled":True,"ready":False,"reason":"尚未达到语义漏召回与材料性提升证据门"}
        findings=[]
        with self.store.connection() as c:
            for row in c.execute("SELECT artifact_id,body_markdown FROM narrative_artifact"):
                if find_secrets(row["body_markdown"]): findings.append(f"artifact:{row['artifact_id']}")
            for row in c.execute("SELECT evidence_id,body_text FROM evidence_ledger_entry"):
                if find_secrets(row["body_text"]): findings.append(f"evidence:{row['evidence_id']}")
        if findings:
            return {"enabled":True,"ready":False,"reason":"历史库存在疑似秘密，已阻止云端 embedding","blocked_records":findings}
        return {"enabled":True,"ready":True,"reason":"证据门与 Secret Guard 均通过"}
