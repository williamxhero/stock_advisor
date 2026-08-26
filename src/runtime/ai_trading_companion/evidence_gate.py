"""Deterministic qualification for current-information research outputs."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class EvidenceInsufficient(RuntimeError):
    def __init__(self, verifier: dict[str, Any]) -> None:
        self.verifier = verifier
        self.category = "evidence_insufficient"
        self.request_id = None
        super().__init__("evidence_insufficient: " + ", ".join(verifier.get("problems") or ["unknown"]))


class EvidenceGate:
    """Reject untraceable, stale, future, or semantically incomplete evidence."""

    _accepted_coverage = {"covered", "checked_no_change"}

    def evaluate(
        self,
        evidence: dict[str, Any],
        requirements: list[dict[str, Any]],
        observations: list[dict[str, Any]],
        expected_as_of: str | None = None,
    ) -> dict[str, Any]:
        problems: list[str] = []
        successful = [item for item in observations if item.get("status") == "succeeded" and item.get("non_empty")]
        if not successful:
            problems.append("no_current_information_tool_result")

        observed_urls = {
            self._canonical_url(url)
            for item in successful
            for url in item.get("result_urls") or []
            if self._canonical_url(url)
        }
        observed_items = {
            (str(item.get("observation_id") or ""), str(result.get("result_item_hash") or ""), self._canonical_url(result.get("url"))): {
                **result, "tool_arguments": item.get("arguments") or {},
            }
            for item in successful
            for result in item.get("result_items") or []
        }
        source_by_url: dict[str, dict[str, Any]] = {}
        observed_source_by_url: dict[str, dict[str, Any]] = {}
        output_as_of = self._parse_time(evidence.get("as_of"))
        if output_as_of is None:
            problems.append("evidence_missing_valid_as_of")
        expected = self._parse_time(expected_as_of)
        if output_as_of and expected and output_as_of != expected:
            problems.append("evidence_as_of_does_not_match_frozen_packet")
        for source in evidence.get("sources") or []:
            url = self._canonical_url(source.get("url"))
            if not url or url not in observed_urls:
                problems.append("source_not_in_current_tool_trace")
                continue
            identity = (str(source.get("tool_observation_id") or ""), str(source.get("result_item_hash") or ""), url)
            if identity not in observed_items:
                problems.append("source_item_not_bound_to_tool_observation")
                continue
            observed_item = observed_items[identity]
            if self._normalize_text(source.get("title")) != self._normalize_text(observed_item.get("title")):
                problems.append("source_title_not_bound_to_tool_result")
            if str(source.get("source_family") or "") != str(observed_item.get("source_family") or ""):
                problems.append("source_family_not_bound_to_tool_result")
            if str(source.get("upstream_id") or "") != str(observed_item.get("upstream_id") or ""):
                problems.append("source_upstream_not_bound_to_tool_result")
            excerpt = self._normalize_text(source.get("excerpt"))
            evidence_text = self._normalize_text(observed_item.get("evidence_text"))
            if not excerpt or not evidence_text or excerpt not in evidence_text:
                problems.append("source_excerpt_not_in_tool_result")
                continue
            source_by_url[url] = source
            observed_source_by_url[url] = observed_item
            fact_as_of = self._parse_time(source.get("fact_as_of"))
            if fact_as_of is None:
                problems.append("source_missing_fact_as_of")
            elif output_as_of and fact_as_of > output_as_of:
                problems.append("source_from_future")
            elif not self._text_supports_date(excerpt, fact_as_of, observed_item.get("acquired_at")):
                problems.append("source_fact_time_not_supported_by_tool_result")
            elif self._text_contains_later_time(excerpt, fact_as_of):
                problems.append("source_fact_time_after_declared_as_of")
            published_at = self._parse_time(source.get("published_at"))
            if published_at and output_as_of and published_at > output_as_of:
                problems.append("source_published_in_future")
            observed_published = self._parse_time(observed_item.get("published_at"))
            if observed_published != published_at:
                problems.append("source_published_at_not_bound_to_tool_result")

        coverage = {
            str(item.get("requirement_key") or ""): item
            for item in evidence.get("coverage") or []
            if isinstance(item, dict)
        }
        missing: list[str] = []
        for requirement in requirements:
            if not requirement.get("blocking", True):
                continue
            key = str(requirement.get("key") or "")
            row = coverage.get(key)
            if not row or row.get("status") not in self._accepted_coverage:
                problems.append(f"blocking_requirement_missing:{key}")
                missing.append(key)
                continue
            if requirement.get("evidence_class") == "internal_frozen":
                expected_status = "covered" if int(requirement.get("internal_record_count") or 0) > 0 else "checked_no_change"
                if row.get("status") != expected_status:
                    problems.append(f"internal_requirement_status_invalid:{key}")
                    missing.append(key)
                continue
            urls = [self._canonical_url(url) for url in row.get("evidence_urls") or []]
            if not urls or any(not url or url not in source_by_url for url in urls):
                problems.append(f"blocking_requirement_untraceable:{key}")
                missing.append(key)
                continue
            if output_as_of and not any(
                (self._parse_time(source_by_url[url].get("fact_as_of")) or datetime.min.replace(tzinfo=timezone.utc)).date()
                == output_as_of.date()
                for url in urls
            ):
                problems.append(f"blocking_requirement_stale:{key}")
                missing.append(key)
                continue
            support = " ".join(
                self._normalize_text(source_by_url[url].get("excerpt")) for url in urls if url in source_by_url
            )
            term_groups = requirement.get("evidence_terms") or []
            if term_groups:
                if row.get("status") == "checked_no_change":
                    support = " ".join(
                        self._normalize_text(json_value)
                        for url in urls if url in observed_source_by_url
                        for json_value in (observed_source_by_url[url].get("tool_arguments") or {}).values()
                    )
                if any(not any(str(term) in support for term in group) for group in term_groups):
                    problems.append(f"blocking_requirement_semantically_unsupported:{key}")
                    missing.append(key)
                    continue
            minimum_numeric = int(requirement.get("minimum_numeric_facts") or 0)
            numeric_facts = set(re.findall(
                r"(?<![\d.])\d+(?:\.\d+)?\s*(?:%|％|万亿元|亿元|万亿|亿|万家|家|只)", support,
            ))
            if len(numeric_facts) < minimum_numeric:
                problems.append(f"blocking_requirement_lacks_numeric_facts:{key}")
                missing.append(key)
                continue
            minimum_entities = int(requirement.get("minimum_named_entities") or 0)
            entities = {
                name for name in re.findall(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:板块|概念|题材)", support)
                if name not in {"领涨", "领跌", "强势", "弱势", "市场", "行业", "多个", "相关"}
            }
            if len(entities) < minimum_entities:
                problems.append(f"blocking_requirement_lacks_named_entities:{key}")
                missing.append(key)

        for event in evidence.get("high_impact_events") or []:
            if event.get("materiality") != "high":
                continue
            urls = {self._canonical_url(url) for url in event.get("evidence_urls") or []}
            sources = [source_by_url[url] for url in urls if url in source_by_url]
            upstreams = {self._host(item.get("url")) for item in sources if self._host(item.get("url"))}
            if not any(self._trusted_primary(item.get("url")) for item in sources) and len(upstreams) < 2:
                problems.append("high_impact_fact_lacks_primary_or_independent_confirmation")

        backends = sorted({str(item.get("backend") or "") for item in observations if item.get("backend")})
        return {
            "validator_version": 2,
            "passed": not problems,
            "problems": list(dict.fromkeys(problems)),
            "missing_requirements": list(dict.fromkeys(missing)),
            "attempted_backends": backends,
            "successful_tool_results": len(successful),
            "observed_urls": sorted(observed_urls),
        }

    @staticmethod
    def _parse_time(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    @staticmethod
    def _canonical_url(value: Any) -> str:
        try:
            parsed = urlsplit(str(value or "").strip())
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return parsed._replace(fragment="").geturl().rstrip("/")

    @staticmethod
    def _host(value: Any) -> str:
        return urlsplit(str(value or "")).netloc.lower()

    @classmethod
    def _trusted_primary(cls, value: Any) -> bool:
        host = cls._host(value).split(":", 1)[0]
        return host.endswith(".gov.cn") or host in {
            "gov.cn", "www.gov.cn", "www.csrc.gov.cn", "www.sse.com.cn", "www.szse.cn",
            "www.bse.cn", "www.cninfo.com.cn", "www.pbc.gov.cn", "www.stats.gov.cn",
        }

    @staticmethod
    def _normalize_text(value: Any) -> str:
        return " ".join(str(value or "").split())

    @classmethod
    def _text_supports_date(cls, text: str, fact_as_of: datetime, acquired_at: Any) -> bool:
        month, day, year = fact_as_of.month, fact_as_of.day, fact_as_of.year
        compact = cls._normalize_text(text)
        explicit = (
            f"{year}-{month:02d}-{day:02d}", f"{year}/{month:02d}/{day:02d}",
            f"{year}年{month}月{day}日", f"{month}月{day}日", f"{month:02d}-{day:02d}",
        )
        if any(value in compact for value in explicit):
            return True
        acquired = cls._parse_time(acquired_at)
        return bool(
            acquired and acquired.date() == fact_as_of.date()
            and any(word in compact for word in ("今日", "今天", "当日"))
            and re.search(r"(?<!\d)\d{1,2}(?:[:：]\d{2}|[时点](?:\d{1,2}分?)?)", compact)
        )

    @staticmethod
    def _text_contains_later_time(text: str, fact_as_of: datetime) -> bool:
        matches = re.findall(
            r"(?<!\d)([01]?\d|2[0-3])(?:[:：]|时|点)([0-5]?\d)?(?:分)?", text,
        )
        if not matches:
            return False
        local = fact_as_of.astimezone(ZoneInfo("Asia/Shanghai"))
        return any((int(hour), int(minute or 0)) > (local.hour, local.minute) for hour, minute in matches)
