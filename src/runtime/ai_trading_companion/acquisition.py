"""Runtime-owned conversion from tool output to opaque Evidence v3 references."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


class AcquisitionBoundary:
    def __init__(self, attempt_id: str) -> None:
        self.attempt_id = attempt_id
        self._sequence = 0
        self._observation_sequence = 0

    def observe(self, name: str, arguments: dict[str, Any], result: dict[str, Any], non_empty: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        self._observation_sequence += 1
        acquired_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [row for row in result.get("results") or [] if isinstance(row, dict)]
        if not rows and result.get("url"):
            rows = [result]
        evidence_items: list[dict[str, Any]] = []
        model_rows: list[dict[str, Any]] = []
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            self._sequence += 1
            ref = f"ev_{self.attempt_id.replace('-', '')}_{self._sequence}"
            host = urlsplit(url).netloc.lower()
            excerpt = str(row.get("snippet") or row.get("text") or row.get("title") or "")[:8000]
            item = {
                "evidence_ref": ref, "url": url, "title": str(row.get("title") or result.get("title") or ""),
                "source_identity": host, "independence_group": str(row.get("independence_group") or host),
                "primary": bool(row.get("primary")) or self._trusted_primary(host),
                "excerpt_text": excerpt, "fact_as_of": row.get("fact_as_of") or row.get("published_at") or acquired_at,
                "published_at": row.get("published_at"), "acquired_at": acquired_at,
            }
            evidence_items.append(item)
            model_rows.append({"evidence_ref": ref, "excerpt": excerpt})
        observation = {
            "attempt_id": self.attempt_id, "observation_id": f"obs_{self.attempt_id.replace('-', '')}_{self._observation_sequence}",
            "tool": name, "backend": str(result.get("backend") or name), "operation": name,
            "ok": True, "status": "succeeded", "non_empty": non_empty, "arguments": arguments,
            "acquired_at": acquired_at, "evidence_items": evidence_items,
            "content_sha256": result.get("content_sha256") or self._hash(result), "result_sha256": self._hash(result),
        }
        model_result = {"backend": str(result.get("backend") or name), "results": model_rows}
        if not model_rows and result.get("text"):
            model_result["text"] = str(result.get("text"))[:8000]
        return observation, model_result

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _trusted_primary(host: str) -> bool:
        host = host.split(":", 1)[0]
        return host.endswith(".gov.cn") or host in {"gov.cn", "www.sse.com.cn", "www.szse.cn", "www.cninfo.com.cn"}
