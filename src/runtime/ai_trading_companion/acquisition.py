"""Runtime-owned conversion from tool output to opaque Evidence v3 references."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .secret_guard import find_secrets


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
        secret_rejected_items = 0
        for row in rows:
            url = str(row.get("url") or "")
            if not url:
                continue
            self._sequence += 1
            ref = f"ev_{self.attempt_id.replace('-', '')}_{self._sequence}"
            host = urlsplit(url).netloc.lower()
            excerpt = str(
                row.get("excerpt_text") or row.get("snippet") or row.get("text") or row.get("title") or ""
            )[:8000]
            if find_secrets(excerpt):
                secret_rejected_items += 1
                continue
            canonical_url = self._canonical_url(url)
            original_source = self._canonical_origin(row.get("original_source") or row.get("original_url"))
            content_fingerprint = "sha256:" + hashlib.sha256(
                " ".join(excerpt.split()).encode("utf-8")
            ).hexdigest()
            independence_group = str(row.get("independence_group") or "").strip()
            if original_source:
                independence_group = "origin:" + original_source.casefold()
            elif not independence_group:
                independence_group = "publisher:" + str(row.get("original_publisher") or host).strip().casefold()
            citation_chain = [
                self._canonical_origin(value) for value in row.get("citation_chain") or []
                if self._canonical_origin(value)
            ]
            item = {
                "evidence_ref": ref, "url": url, "title": str(row.get("title") or result.get("title") or ""),
                "canonical_url": canonical_url, "source_identity": host,
                "author": str(row.get("author") or ""),
                "publisher": str(row.get("publisher") or row.get("source") or host),
                "original_source": original_source, "citation_chain": citation_chain,
                "content_fingerprint": str(row.get("content_fingerprint") or content_fingerprint),
                "independence_group": independence_group,
                "primary": bool(row.get("primary")) or self._trusted_primary(host),
                "source_tier": str(row.get("source_tier") or (
                    "primary_document" if bool(row.get("primary")) or self._trusted_primary(host) else "secondary"
                )),
                "factual_status": str(row.get("factual_status") or "unknown"),
                "market_propagation": str(row.get("market_propagation") or "unknown"),
                "claims": [dict(value) for value in row.get("claims") or [] if isinstance(value, dict)],
                "excerpt_text": excerpt, "fact_as_of": row.get("fact_as_of") or row.get("published_at"),
                "published_at": row.get("published_at"), "acquired_at": acquired_at,
            }
            evidence_items.append(item)
            model_rows.append({"evidence_ref": ref, "excerpt": excerpt})
        observation = {
            "attempt_id": self.attempt_id, "observation_id": f"obs_{self.attempt_id.replace('-', '')}_{self._observation_sequence}",
            "tool": name, "backend": str(result.get("backend") or name), "operation": name,
            "ok": True, "status": "succeeded", "non_empty": bool(evidence_items) if rows else non_empty,
            "secret_rejected_items": secret_rejected_items, "arguments": arguments,
            "acquired_at": acquired_at, "evidence_items": evidence_items,
            "content_sha256": result.get("content_sha256") or self._hash(result), "result_sha256": self._hash(result),
            "prompt_injection_detected": bool(result.get("prompt_injection_detected")),
            "prompt_injection_blocked": bool(result.get("prompt_injection_blocked")),
            "prompt_injection_succeeded": bool(result.get("prompt_injection_succeeded")),
        }
        model_result = {"backend": str(result.get("backend") or name), "results": model_rows}
        if not model_rows and result.get("text"):
            model_result["text"] = str(result.get("text"))[:8000]
        return observation, model_result

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_url(value: Any) -> str:
        try:
            parsed = urlsplit(str(value or "").strip())
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        tracking = {"fbclid", "gclid", "spm"}
        query = [
            (key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in tracking
        ]
        host = parsed.hostname.casefold()
        if parsed.port and not (
            parsed.scheme == "http" and parsed.port == 80
            or parsed.scheme == "https" and parsed.port == 443
        ):
            host += f":{parsed.port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((parsed.scheme.casefold(), host, path, urlencode(sorted(query)), "")).rstrip("/")

    @classmethod
    def _canonical_origin(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return cls._canonical_url(text) or " ".join(text.split())

    @staticmethod
    def _trusted_primary(host: str) -> bool:
        host = host.split(":", 1)[0]
        return host.endswith(".gov.cn") or host in {"gov.cn", "www.sse.com.cn", "www.szse.cn", "www.cninfo.com.cn"}
