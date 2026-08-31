from __future__ import annotations

import json
from typing import Any, Callable, Protocol
from urllib.parse import urlencode
from urllib.request import urlopen


class SourceUnavailable(RuntimeError):
    pass


class SourceAdapter(Protocol):
    def hydrate(self, reference: dict[str, str]) -> dict[str, str]: ...
    def health(self) -> dict[str, str]: ...


def _fetch_json(url: str, timeout: float = 20.0) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read())


class MarketHubSourceAdapter:
    def __init__(
        self, base_url: str = "http://yosef-server:8803",
        fetch_json: Callable[[str], dict[str, Any]] = _fetch_json,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetch_json = fetch_json

    def hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        if reference.get("record_type") != "stock_quote_1d":
            raise SourceUnavailable("unsupported MarketHub record_type")
        _require(reference, "date", "code")
        health = self.fetch_json(self.base_url + "/api/health")
        query = urlencode(
            {
                "code": reference["code"], "freq": "1d",
                "trade_date": reference["date"], "data_version": health["data_version"],
            }
        )
        result = self.fetch_json(self.base_url + "/api/stocks/quotes?" + query)
        items = [
            item for item in result.get("items") or []
            if str(item.get("code")) == reference["code"]
            and str(item.get("trade_time", ""))[:10] == reference["date"]
        ]
        if not items:
            raise SourceUnavailable("MarketHub reference returned no matching record")
        return {
            "title": f"{reference['code']} {reference['date']} 日线",
            "body": json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "occurred_at": reference["date"] + "T00:00:00Z",
        }

    def health(self) -> dict[str, str]:
        try:
            value = self.fetch_json(self.base_url + "/api/health")
            return {"state": "ready" if value.get("status") == "ok" else "degraded", "version": str(value.get("version", ""))}
        except Exception as error:
            return {"state": "unavailable", "detail": str(error)}


class ArticleArchiveSourceAdapter:
    def __init__(
        self, base_url: str = "http://yosef-server:8815",
        fetch_json: Callable[[str], dict[str, Any]] = _fetch_json,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetch_json = fetch_json

    def hydrate(self, reference: dict[str, str]) -> dict[str, str]:
        _require(reference, "record_type", "date", "code", "event_id")
        query = urlencode(
            {
                "source": reference["record_type"], "start_date": reference["date"],
                "end_date": reference["date"], "limit_per_source": 1000,
            }
        )
        result = self.fetch_json(self.base_url + "/api/articles/range?" + query)
        for group in result.get("groups") or []:
            for article in group.get("articles") or []:
                if str(article.get("article_id")) != reference["event_id"]:
                    continue
                searchable = str(article.get("category", "")) + " " + str(article.get("content", ""))
                if reference["code"] not in searchable:
                    raise SourceUnavailable("8815 event does not match stock code")
                return {
                    "title": str(article.get("title") or reference["event_id"]),
                    "body": str(article.get("content") or ""),
                    "occurred_at": _iso(str(article.get("published_at") or reference["date"])),
                }
        raise SourceUnavailable("8815 reference returned no matching event")

    def health(self) -> dict[str, str]:
        try:
            self.fetch_json(self.base_url + "/api/articles/range?source=all&start_date=1970-01-01&end_date=1970-01-01")
            return {"state": "ready"}
        except Exception as error:
            return {"state": "unavailable", "detail": str(error)}


def _require(reference: dict[str, str], *names: str) -> None:
    missing = [name for name in names if not reference.get(name)]
    if missing:
        raise SourceUnavailable("source reference missing: " + ", ".join(missing))


def _iso(value: str) -> str:
    normalized = value.strip().replace(" ", "T")
    if len(normalized) == 10:
        normalized += "T00:00:00"
    return normalized + ("Z" if not normalized.endswith("Z") else "")

