"""Deterministic qualification for current-information research outputs."""
from __future__ import annotations

import hashlib
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
            if key == "weekly_market_history":
                weekly_facts = self._weekly_market_history_facts(
                    [source_by_url[url] for url in urls if url in source_by_url],
                    [str(value) for value in requirement.get("required_entities") or [] if str(value)],
                )
                numeric_count = sum(len(values) for values in weekly_facts.values())
            elif key == "portfolio_market_state":
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
            if key == "weekly_market_history":
                absent = [
                    str(value) for value in requirement.get("required_entities") or []
                    if str(value) and str(value) not in weekly_facts
                ]
                if absent:
                    problems.append(f"blocking_requirement_missing_entities:{key}")
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
            if evidence.get("memory_receipt_required") is True:
                expected_hash = "sha256:" + hashlib.sha256(
                    str(item.get("excerpt_text") or "").encode("utf-8")
                ).hexdigest()
                if (
                    not str(item.get("memory_episode_id") or "")
                    or not self._time(item.get("known_at"), "source_known_at", problems)
                    or str(item.get("memory_content_hash") or "") != expected_hash
                ):
                    problems.append("source_memory_receipt_missing_or_invalid")
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
                # A query proves only that discovery was attempted.  A negative
                # conclusion must remain traceable to a result that identifies
                # the checked source/window (for example a normalized disclosure snapshot).
                problems.append(f"checked_no_change_untraceable:{key}"); missing.append(key)
                continue
            if not refs or len(bound) != len(refs):
                problems.append(f"blocking_requirement_untraceable:{key}"); missing.append(key); continue
            if not self._in_window(bound, requirement.get("window") or {}, problems):
                problems.append(f"blocking_requirement_stale:{key}"); missing.append(key); continue
            if row.get("status") == "checked_no_change" and not self._matching_negative_query(bound, requirement.get("negative_query_terms") or []):
                problems.append(f"checked_no_change_query_not_matched:{key}"); missing.append(key)
                continue
            if key == "indices_close":
                expected = {"000001", "399001", "399006"}
                complete = self._index_close_facts(bound)
                if complete != expected:
                    problems.append(f"blocking_requirement_missing_entities:{key}"); missing.append(key)
                continue
            if key == "market_breadth":
                breadth_facts = self._market_breadth_facts(bound)
                if len(breadth_facts) < int(requirement.get("minimum_numeric_facts") or 0):
                    problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key)
                continue
            if key == "market_fund_flow":
                flow_facts = self._market_fund_flow_facts(bound)
                if len(flow_facts) < int(requirement.get("minimum_numeric_facts") or 0):
                    problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key)
                elif not self._market_fund_flow_scope_valid(bound):
                    problems.append(f"blocking_requirement_fund_flow_scope_invalid:{key}"); missing.append(key)
                continue
            if key == "weekly_market_history":
                weekly_facts = self._weekly_market_history_facts(bound, required_entities)
                numeric_count = sum(len(values) for values in weekly_facts.values())
                if numeric_count < int(requirement.get("minimum_numeric_facts") or 0):
                    problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key); continue
                absent_entities = [entity for entity in required_entities if entity not in weekly_facts]
                if absent_entities:
                    problems.append(f"blocking_requirement_missing_entities:{key}"); missing.append(key)
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
            if key == "portfolio_current_bar":
                bar_facts = self._portfolio_current_bar_facts(bound, required_entities)
                numeric_count = sum(len(values) for values in bar_facts.values())
                if numeric_count < int(requirement.get("minimum_numeric_facts") or 0):
                    problems.append(f"blocking_requirement_lacks_numeric_facts:{key}"); missing.append(key); continue
                absent_entities = [entity for entity in required_entities if entity not in bar_facts]
                if absent_entities:
                    problems.append(f"blocking_requirement_missing_entities:{key}"); missing.append(key)
                continue
            if key == "themes_and_capacity_cores" and requirement.get("requires_distribution") is True:
                if not self._sector_distribution_complete(bound):
                    problems.append(f"blocking_requirement_lacks_distribution:{key}"); missing.append(key); continue
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
    def _weekly_market_history_facts(
        sources: list[dict[str, Any]], required_entities: list[str],
    ) -> dict[str, set[tuple[str, str]]]:
        """Return typed OHLCV facts from bounded multi-day index series."""
        required = set(required_entities)
        fields = {"open", "close", "high", "low", "volume"}
        complete: dict[str, set[tuple[str, str]]] = {}
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
                fact_date = datetime.fromisoformat(
                    str(source.get("fact_as_of") or "").replace("Z", "+00:00")
                ).date().isoformat()
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            symbol = str(payload.get("symbol") or "")
            series = payload.get("series")
            if (required and symbol not in required) or not isinstance(series, list) or len(series) < 2:
                continue
            dates: list[str] = []
            facts: set[tuple[str, str]] = set()
            valid = True
            for bar in series:
                if not isinstance(bar, dict):
                    valid = False
                    break
                try:
                    date = datetime.fromisoformat(str(bar.get("date") or "")).date().isoformat()
                except ValueError:
                    valid = False
                    break
                if any(not isinstance(bar.get(field), (int, float)) or isinstance(bar.get(field), bool) for field in fields):
                    valid = False
                    break
                dates.append(date)
                facts.update((date, field) for field in fields)
            if (
                not valid or dates != sorted(set(dates))
                or payload.get("start") != dates[0] or payload.get("end") != dates[-1]
                or fact_date != dates[-1]
            ):
                continue
            complete.setdefault(symbol, set()).update(facts)
        return complete

    @staticmethod
    def _portfolio_current_bar_facts(
        sources: list[dict[str, Any]], required_entities: list[str],
    ) -> dict[str, set[str]]:
        """Return complete deterministic OHLC facts for each required symbol."""
        required = set(required_entities)
        fields = {"open", "high", "low", "close"}
        complete: dict[str, set[str]] = {}
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            for bar in payload.get("bars") or []:
                if not isinstance(bar, dict):
                    continue
                symbol = str(bar.get("symbol") or "")
                valid = {
                    field for field in fields
                    if isinstance(bar.get(field), (int, float)) and not isinstance(bar.get(field), bool)
                }
                metadata_complete = all(bar.get(field) for field in (
                    "freq", "trade_time", "interval_start", "interval_end", "observed_at",
                    "market_status", "provider", "source_semantics",
                )) and isinstance(bar.get("is_final"), bool)
                if symbol in required and valid == fields and metadata_complete:
                    complete[symbol] = valid
        return complete

    @staticmethod
    def _index_close_facts(sources: list[dict[str, Any]]) -> set[str]:
        """Return canonical indices backed by complete typed official-close rows."""
        expected = {"000001", "399001", "399006"}
        fields = {"previous_close", "price", "change", "change_percent"}
        complete: set[str] = set()
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            if payload.get("finality") not in {"close", "official_close"}:
                continue
            for index in payload.get("indices") or []:
                if not isinstance(index, dict):
                    continue
                symbol = str(index.get("symbol") or "")
                valid = all(
                    isinstance(index.get(field), (int, float)) and not isinstance(index.get(field), bool)
                    for field in fields
                )
                if (
                    symbol in expected and valid and index.get("name")
                    and index.get("status") == "closed" and index.get("trading_date")
                    and str(index.get("quote_at") or "") == str(source.get("fact_as_of") or "")
                ):
                    complete.add(symbol)
        return complete

    @staticmethod
    def _market_breadth_facts(sources: list[dict[str, Any]]) -> set[str]:
        """Return required typed breadth fields from the canonical tool JSON."""
        fields = {"up", "down", "flat"}
        found: set[str] = set()
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            candidates = [payload.get("breadth")] if isinstance(payload, dict) else []
            while candidates:
                value = candidates.pop()
                if isinstance(value, dict):
                    found.update(field for field in fields if isinstance(value.get(field), int) and not isinstance(value.get(field), bool))
                    candidates.extend(item for item in value.values() if isinstance(item, dict))
        return found

    @staticmethod
    def _market_fund_flow_facts(sources: list[dict[str, Any]]) -> set[str]:
        required = {
            "main_net_inflow", "small_net_inflow", "medium_net_inflow",
            "large_net_inflow", "super_large_net_inflow",
        }
        found: set[str] = set()
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            combined = payload.get("combined") if isinstance(payload, dict) else None
            if isinstance(combined, dict):
                found.update(
                    field for field in required
                    if isinstance(combined.get(field), (int, float)) and not isinstance(combined.get(field), bool)
                )
            if isinstance(payload, dict) and payload.get("coverage_level") == "directional_sector":
                leaders = payload.get("sector_inflow_leaders")
                if isinstance(leaders, list):
                    found.update(
                        f"sector_net_inflow:{index}"
                        for index, row in enumerate(leaders)
                        if isinstance(row, dict)
                        and isinstance(row.get("net_inflow"), (int, float))
                        and not isinstance(row.get("net_inflow"), bool)
                    )
        return found

    @staticmethod
    def _market_fund_flow_scope_valid(sources: list[dict[str, Any]]) -> bool:
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get("combined"), dict):
                return True
            if payload.get("coverage_level") != "directional_sector":
                continue
            leaders = payload.get("sector_inflow_leaders")
            outflows = payload.get("sector_outflow_leaders")
            limitations = {str(value) for value in payload.get("limitations") or []}
            if (
                payload.get("currency") != "CNY" and payload.get("unit") != "CNY"
                or not isinstance(leaders, list) or len(leaders) < 3
                or not isinstance(outflows, list) or not outflows
                or not {"full_market_net_flow_unavailable", "order_size_breakdown_unavailable"}.issubset(limitations)
                or "combined" in payload or "markets" in payload
            ):
                continue
            try:
                valid_leaders = all(
                    isinstance(row, dict)
                    and str(row.get("name") or "").strip()
                    and row.get("direction") == "inflow"
                    and isinstance(row.get("rank"), int) and not isinstance(row.get("rank"), bool)
                    and float(row.get("net_inflow")) > 0
                    for row in leaders
                )
                valid_outflows = all(
                    isinstance(row, dict)
                    and str(row.get("name") or "").strip()
                    and row.get("direction") == "outflow"
                    and isinstance(row.get("rank"), int) and not isinstance(row.get("rank"), bool)
                    for row in outflows
                )
            except (TypeError, ValueError):
                continue
            if valid_leaders and valid_outflows:
                return True
        return False

    @staticmethod
    def _sector_distribution_complete(sources: list[dict[str, Any]]) -> bool:
        for source in sources:
            try:
                payload = json.loads(str(source.get("excerpt") or ""))
            except (TypeError, ValueError):
                continue
            distribution = payload.get("distribution") if isinstance(payload, dict) else None
            if not isinstance(distribution, dict):
                continue
            complete = True
            for kind in ("industry", "theme"):
                row = distribution.get(kind)
                if not isinstance(row, dict):
                    complete = False
                    break
                total = row.get("total")
                counts = (row.get("up"), row.get("down"), row.get("flat"))
                if (
                    not isinstance(total, int) or isinstance(total, bool) or total <= 0
                    or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counts)
                    or sum(counts) != total
                ):
                    complete = False
                    break
            if complete:
                return True
        return False

    @staticmethod
    def _matching_negative_query(sources: list[dict[str, Any]], terms: list[str]) -> bool:
        if not terms:
            return True
        return any(all(term.casefold() in str(item.get("tool_arguments", {}).get("query") or "").casefold() for term in terms) for item in sources)

    @staticmethod
    def _normalized(evidence: dict[str, Any], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
        materialized = []
        for source in evidence.get("sources") or []:
            item = sources.get(str(source.get("evidence_ref") or ""))
            if item:
                materialized.append({**item, "evidence_ref": source.get("evidence_ref"), "excerpt": source.get("excerpt"), "analysis": source.get("analysis")})
        return {**evidence, "sources": materialized}
