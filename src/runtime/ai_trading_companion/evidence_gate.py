"""Deterministic qualification for current-information research outputs."""
from __future__ import annotations

import json
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
        requirements: list[dict[str, Any]] | dict[str, Any],
        observations: list[dict[str, Any]],
        expected_as_of: str | None = None,
        *,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(requirements, dict) and int(requirements.get("version") or 0) >= 3:
            return _EvidenceGateV3().evaluate(evidence, requirements, observations, expected_as_of, attempt_id)
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
            if key == "portfolio_market_state":
                # Quote tools deliberately return typed JSON rather than prose
                # with currency suffixes. Count the four required numeric quote
                # facts across all held symbols; do not require one source to
                # carry the whole portfolio's 4*N fields.
                numeric_count = len(re.findall(
                    r'"(?:previous_close|price|change|change_percent)"\s*:\s*-?\d+(?:\.\d+)?', support,
                ))
            else:
                numeric_count = len(set(re.findall(
                    r"(?<![\d.])\d+(?:\.\d+)?\s*(?:%|％|万亿元|亿元|万亿|亿|万家|家|只)", support,
                )))
            if numeric_count < minimum_numeric:
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


class _EvidenceGateV3:
    """Pure validation of runtime-bound, attempt-scoped Evidence v3 references."""

    def evaluate(
        self, evidence: dict[str, Any], contract: dict[str, Any], observations: list[dict[str, Any]],
        expected_as_of: str | None, attempt_id: str | None,
    ) -> dict[str, Any]:
        problems: list[str] = []
        as_of = self._time(evidence.get("as_of"), "evidence_as_of", problems)
        expected = self._time(expected_as_of or contract.get("as_of"), "frozen_as_of", problems)
        if as_of and expected and as_of != expected:
            problems.append("evidence_as_of_does_not_match_frozen_packet")
        current = [item for item in observations if item.get("status") == "succeeded" and item.get("non_empty") and (not attempt_id or item.get("attempt_id") == attempt_id)]
        if not current:
            problems.append("no_current_information_tool_result")
        items = {
            str(entry.get("evidence_ref")): {**entry, "tool_arguments": observation.get("arguments") or {}}
            for observation in current for entry in observation.get("evidence_items") or []
            if entry.get("evidence_ref")
        }
        sources: dict[str, dict[str, Any]] = {}
        for source in evidence.get("sources") or []:
            ref = str(source.get("evidence_ref") or "")
            item = items.get(ref)
            if not item:
                problems.append("source_ref_not_in_current_attempt")
                continue
            excerpt = EvidenceGate._normalize_text(source.get("excerpt"))
            runtime_excerpt = EvidenceGate._normalize_text(item.get("excerpt_text"))
            if not excerpt or not runtime_excerpt or excerpt not in runtime_excerpt:
                problems.append("source_excerpt_not_in_runtime_evidence")
                continue
            for key in ("fact_as_of", "published_at", "acquired_at"):
                value = item.get(key)
                parsed = self._time(value, f"source_{key}", problems, required=(key != "published_at"))
                # Historical replays are necessarily acquired after their frozen
                # decision time.  Only the fact/publication time can introduce
                # look-ahead; acquisition remains required and timezone-aware.
                if key != "acquired_at" and parsed and as_of and parsed > as_of:
                    problems.append("source_from_future" if key == "fact_as_of" else f"source_{key}_in_future")
            sources[ref] = {**item, "excerpt": excerpt, "analysis": str(source.get("analysis") or "")}
        coverage = {str(row.get("requirement_key") or ""): row for row in evidence.get("coverage") or [] if isinstance(row, dict)}
        missing: list[str] = []
        for requirement in contract.get("requirements") or []:
            if not requirement.get("blocking", True):
                continue
            key = str(requirement.get("key") or "")
            row = coverage.get(key)
            allowed = set(requirement.get("allowed_coverage") or ["covered"])
            if not row or row.get("status") not in allowed:
                problems.append(f"blocking_requirement_missing:{key}"); missing.append(key); continue
            if requirement.get("evidence_class") == "internal_runtime":
                expected_status = "covered" if int(requirement.get("internal_record_count") or 0) > 0 else "checked_no_change"
                if row.get("status") != expected_status:
                    problems.append(f"internal_requirement_status_invalid:{key}"); missing.append(key)
                continue
            required_entities = [str(value) for value in requirement.get("required_entities") or [] if str(value)]
            if requirement.get("evidence_class") == "public_if_present" and not required_entities:
                if row.get("status") != "checked_no_change":
                    problems.append(f"empty_portfolio_requirement_status_invalid:{key}"); missing.append(key)
                continue
            refs = [str(ref) for ref in row.get("evidence_refs") or []]
            bound = [sources[ref] for ref in refs if ref in sources]
            if row.get("status") == "checked_no_change" and not refs:
                if not self._matching_negative_observation(
                    observations, key, requirement.get("negative_query_terms") or [], attempt_id,
                ):
                    problems.append(f"checked_no_change_query_not_matched:{key}"); missing.append(key)
                elif required_entities and not self._negative_queries_cover_entities(
                    observations, key, required_entities, requirement.get("negative_query_terms") or [], attempt_id,
                ):
                    problems.append(f"checked_no_change_query_missing_entities:{key}"); missing.append(key)
                continue
            if not refs or len(bound) != len(refs):
                problems.append(f"blocking_requirement_untraceable:{key}"); missing.append(key); continue
            if not self._in_window(bound, requirement.get("window") or {}, problems):
                problems.append(f"blocking_requirement_stale:{key}"); missing.append(key); continue
            if row.get("status") == "checked_no_change" and not self._matching_negative_query(bound, requirement.get("negative_query_terms") or []):
                problems.append(f"checked_no_change_query_not_matched:{key}"); missing.append(key)
                continue
            support = " ".join(EvidenceGate._normalize_text(item.get("excerpt")) for item in bound)
            term_groups = requirement.get("evidence_terms") or []
            if any(not any(str(term) in support for term in group) for group in term_groups):
                problems.append(f"blocking_requirement_semantically_unsupported:{key}"); missing.append(key); continue
            if key == "portfolio_market_state":
                quote_facts = self._portfolio_quote_facts(bound, required_entities)
                numeric_count = sum(len(values) for values in quote_facts.values())
                if numeric_count < int(requirement.get("minimum_numeric_facts") or 0):
                    problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key); continue
                absent_entities = [entity for entity in required_entities if entity not in quote_facts]
                if absent_entities:
                    problems.append(f"blocking_requirement_missing_entities:{key}"); missing.append(key)
                continue
            numeric_facts = set(re.findall(
                r"(?<![\d.])\d+(?:\.\d+)?\s*(?:%|％|万亿元|亿元|万亿|亿|万家|家|只|股|元)", support,
            ))
            if len(numeric_facts) < int(requirement.get("minimum_numeric_facts") or 0):
                problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key); continue
            entities = {
                name for name in re.findall(r"([\u4e00-\u9fffA-Za-z0-9]{2,12})(?:板块|概念|题材)", support)
                if name not in {"领涨", "领跌", "强势", "弱势", "市场", "行业", "多个", "相关"}
            }
            if len(entities) < int(requirement.get("minimum_named_entities") or 0):
                problems.append(f"blocking_requirement_lacks_named_entities:{key}"); missing.append(key); continue
            absent_entities = [entity for entity in required_entities if entity not in support]
            if absent_entities:
                problems.append(f"blocking_requirement_missing_entities:{key}"); missing.append(key)
        for event in evidence.get("high_impact_events") or []:
            if event.get("materiality") != "high":
                continue
            refs = [str(ref) for ref in event.get("evidence_refs") or []]
            bound = [sources[ref] for ref in refs if ref in sources]
            independent = {str(item.get("independence_group") or "") for item in bound if item.get("independence_group")}
            if not any(item.get("primary") for item in bound) and len(independent) < 2:
                problems.append("high_impact_fact_lacks_primary_or_independent_confirmation")
        return {
            "validator_version": 3, "passed": not problems,
            "problems": list(dict.fromkeys(problems)), "missing_requirements": list(dict.fromkeys(missing)),
            "attempted_backends": sorted({str(item.get("backend") or "") for item in current if item.get("backend")}),
            "successful_tool_results": len(current), "normalized_evidence": self._normalized(evidence, sources),
        }

    @staticmethod
    def _time(value: Any, label: str, problems: list[str], *, required: bool = True) -> datetime | None:
        if not value:
            if required: problems.append(f"{label}_missing")
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            problems.append(f"{label}_invalid"); return None
        if parsed.tzinfo is None:
            problems.append(f"{label}_missing_timezone"); return None
        return parsed.astimezone(timezone.utc)

    def _in_window(self, sources: list[dict[str, Any]], window: dict[str, Any], problems: list[str]) -> bool:
        start = self._time(window.get("start"), "contract_window_start", problems)
        end = self._time(window.get("end"), "contract_window_end", problems)
        if not start or not end:
            return False
        values = [self._time(item.get("fact_as_of"), "source_fact_as_of", problems) for item in sources]
        if any(value is None for value in values):
            return False
        if window.get("mode") == "exact":
            return any(value == start == end for value in values)
        return any(start < value <= end for value in values)

    @staticmethod
    def _portfolio_quote_facts(sources: list[dict[str, Any]], required_entities: list[str]) -> dict[str, set[str]]:
        """Return complete deterministic quote fields for each required symbol."""
        required = set(required_entities)
        fields = {"previous_close", "price", "change", "change_percent"}
        complete: dict[str, set[str]] = {}
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            for quote in payload.get("quotes") or []:
                if not isinstance(quote, dict):
                    continue
                symbol = str(quote.get("symbol") or "")
                valid = {
                    field for field in fields
                    if isinstance(quote.get(field), (int, float)) and not isinstance(quote.get(field), bool)
                }
                if symbol in required and valid == fields and quote.get("quote_at") and quote.get("trading_date") and quote.get("status"):
                    complete[symbol] = valid
        return complete

    @staticmethod
    def _matching_negative_query(sources: list[dict[str, Any]], terms: list[str]) -> bool:
        if not terms:
            return True
        return any(all(term.casefold() in str(item.get("tool_arguments", {}).get("query") or "").casefold() for term in terms) for item in sources)

    @staticmethod
    def _matching_negative_observation(
        observations: list[dict[str, Any]], requirement_key: str, terms: list[str], attempt_id: str | None,
    ) -> bool:
        if not terms:
            return False
        return any(
            item.get("operation") == "web_search"
            and item.get("status") == "succeeded"
            and (not attempt_id or item.get("attempt_id") == attempt_id)
            and str((item.get("arguments") or {}).get("requirement_key") or "") == requirement_key
            and all(
                str(term).casefold() in str((item.get("arguments") or {}).get("query") or "").casefold()
                for term in terms
            )
            for item in observations
        )

    @staticmethod
    def _negative_queries_cover_entities(
        observations: list[dict[str, Any]], requirement_key: str, entities: list[str], terms: list[str], attempt_id: str | None,
    ) -> bool:
        queries = [
            str((item.get("arguments") or {}).get("query") or "").casefold()
            for item in observations
            if item.get("operation") == "web_search"
            and item.get("status") == "succeeded"
            and (not attempt_id or item.get("attempt_id") == attempt_id)
            and str((item.get("arguments") or {}).get("requirement_key") or "") == requirement_key
            and all(str(term).casefold() in str((item.get("arguments") or {}).get("query") or "").casefold() for term in terms)
        ]
        return all(any(entity.casefold() in query for query in queries) for entity in entities)

    @staticmethod
    def _normalized(evidence: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
        materialized = []
        for source in evidence.get("sources") or []:
            item = sources.get(str(source.get("evidence_ref") or ""))
            if item:
                materialized.append({**item, "evidence_ref": source.get("evidence_ref"), "excerpt": source.get("excerpt"), "analysis": source.get("analysis")})
        return {**evidence, "sources": materialized}
