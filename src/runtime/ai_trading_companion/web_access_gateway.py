"""Small, read-only client for the LAN Web Access Gateway MCP endpoint."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


class WebAccessGatewayError(RuntimeError):
    """A sanitized gateway failure; credentials are never included."""

    def __init__(self, message: str, *, category: str = "awg_error", attempts: int = 1) -> None:
        super().__init__(message)
        self.category = category
        self.attempts = attempts

    def __str__(self) -> str:
        return f"{self.category}: {super().__str__()}"


class WebAccessGatewayClient:
    def __init__(self, research: dict[str, Any]) -> None:
        config = dict(research.get("web_access_gateway") or {})
        self.url = str(config.get("mcp_url") or "").rstrip("/")
        self.token = str(config.get("token") or "")
        self.search_timeout = int(config.get("search_timeout_seconds") or 35)
        self.read_timeout = int(config.get("read_timeout_seconds") or 100)
        self.call_history: list[dict[str, Any]] = []

    def search(self, query: str, categories: str = "news") -> dict[str, Any]:
        value = self._call("web_search", {"query": query, "categories": categories}, self.search_timeout)
        results = value.get("results") if isinstance(value, dict) else []
        if not results and categories != "general":
            value = self._call("web_search", {"query": query, "categories": "general"}, self.search_timeout)
            results = value.get("results") if isinstance(value, dict) else []
        return {"trace_id": _text(value, "trace_id"), "results": [_item(item) for item in results if isinstance(item, dict)]}

    def read(self, url: str, render: str = "auto", not_after: str | None = None) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            raise WebAccessGatewayError("web_read requires an http(s) URL")
        value = self._call("web_read", {"url": url, "render": render, "output": "markdown"}, self.read_timeout)
        body = _text(value, "markdown")
        title = _text(value, "title")
        frozen_market = _frozen_public_market_row(url, body, not_after)
        if frozen_market is not None:
            return {"trace_id": _text(value, "trace_id"), "results": [{
                "url": _text(value, "url") or url,
                "title": frozen_market["title"],
                "excerpt_text": frozen_market["excerpt_text"],
                "fact_as_of": frozen_market["fact_as_of"],
                "published_at": None,
                "primary": False,
            }]}
        detected_time = _fact_as_of(title + "\n" + body + "\n" + url, not_after=not_after)
        return {"trace_id": _text(value, "trace_id"), "results": [{
            "url": _text(value, "url") or url, "title": title, "excerpt_text": body[:12000],
            "fact_as_of": detected_time, "published_at": detected_time, "primary": True,
        }]}

    def browser(self, session_id: str | None, actions: list[dict[str, Any]]) -> dict[str, Any]:
        allowed = {"navigate", "click", "wait", "scroll", "snapshot", "screenshot", "close"}
        if not isinstance(actions, list) or any(not isinstance(a, dict) or str(a.get("type") or a.get("action") or "") not in allowed for a in actions):
            raise WebAccessGatewayError("web_browser only accepts read-only navigation actions")
        normalized_actions = [
            {key: value for key, value in action.items() if value is not None}
            for action in actions
        ]
        value = self._call("web_browser", {"session_id": session_id, "actions": normalized_actions}, self.read_timeout)
        snapshot = _text(value, "snapshot") or _text(value, "markdown")
        url = _text(value, "url")
        return {"trace_id": _text(value, "trace_id"), "results": [{"url": url, "title": _text(value, "title"), "excerpt_text": snapshot[:12000], "fact_as_of": _fact_as_of(snapshot + "\n" + url), "primary": True}]}

    def _call(self, name: str, arguments: dict[str, Any], timeout: int) -> dict[str, Any]:
        if not self.url.startswith(("http://", "https://")):
            raise WebAccessGatewayError("Web Access Gateway MCP URL is not configured")
        if not self.token:
            raise WebAccessGatewayError("Web Access Gateway token is not configured")
        for attempt in range(1, 4):
            try:
                value = self._call_once(name, arguments, timeout)
                self.call_history.append({"tool": name, "status": "passed", "attempts": attempt})
                return value
            except WebAccessGatewayError:
                if attempt == 3:
                    self.call_history.append({"tool": name, "status": "failed", "attempts": 3})
                    raise WebAccessGatewayError(
                        "Web Access Gateway failed three consecutive read-only calls",
                        category="AWG_OUTAGE", attempts=3,
                    ) from None
        raise AssertionError("unreachable")

    def _call_once(self, name: str, arguments: dict[str, Any], timeout: int) -> dict[str, Any]:
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        request = Request(self.url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        try:
            with urlopen(request, timeout=max(1, timeout)) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raise WebAccessGatewayError(f"Web Access Gateway HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise WebAccessGatewayError("Web Access Gateway is unavailable") from exc
        response = _jsonrpc(raw)
        if response.get("error"):
            raise WebAccessGatewayError("Web Access Gateway rejected the read-only request")
        result = response.get("result") or {}
        content = result.get("content") if isinstance(result, dict) else None
        text = next((item.get("text") for item in content or [] if isinstance(item, dict) and item.get("type") == "text"), None)
        if not isinstance(text, str):
            raise WebAccessGatewayError("Web Access Gateway returned no text result")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise WebAccessGatewayError("Web Access Gateway returned malformed tool data") from exc
        if not isinstance(value, dict):
            raise WebAccessGatewayError("Web Access Gateway tool data is not an object")
        return value


def _jsonrpc(raw: str) -> dict[str, Any]:
    for line in reversed(raw.splitlines()):
        candidate = line.removeprefix("data:").strip()
        if candidate.startswith("{"):
            try:
                value = json.loads(candidate)
                if isinstance(value, dict): return value
            except json.JSONDecodeError: pass
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WebAccessGatewayError("Web Access Gateway returned malformed MCP") from exc
    return value if isinstance(value, dict) else {}


def _text(value: Any, key: str) -> str:
    return str(value.get(key) or "") if isinstance(value, dict) else ""


def _item(value: dict[str, Any]) -> dict[str, Any]:
    text = _text(value, "content")
    return {"url": _text(value, "url"), "title": _text(value, "title"), "excerpt_text": text[:2000], "fact_as_of": _fact_as_of(text + "\n" + _text(value, "url")), "primary": False}


def _fact_as_of(text: str, *, not_after: str | None = None) -> str | None:
    pattern = re.compile(
        r"(?<!\d)(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?"
        r"(?:[ T，,]{0,8}([01]?\d|2[0-3])(?:[:：时点]([0-5]?\d))?(?:分)?)?",
    )
    cutoff: datetime | None = None
    if not_after:
        try:
            cutoff = datetime.fromisoformat(str(not_after).replace("Z", "+00:00"))
            if cutoff.tzinfo is not None:
                cutoff = cutoff.astimezone(timezone.utc)
            else:
                cutoff = None
        except ValueError:
            cutoff = None
    candidates: list[datetime] = []
    for match in pattern.finditer(text):
        try:
            hour = int(match.group(4)) if match.group(4) is not None else 15
            minute = int(match.group(5) or 0) if match.group(4) is not None else 0
            local = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), hour, minute,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        except ValueError:
            continue
        utc = local.astimezone(timezone.utc)
        if cutoff is None or utc <= cutoff:
            candidates.append(utc)
    if not candidates:
        return None
    return max(candidates).isoformat().replace("+00:00", "Z")


def _frozen_public_market_row(url: str, body: str, not_after: str | None) -> dict[str, str] | None:
    """Reduce Tencent's mixed historical/current payload to one frozen daily row.

    The endpoint includes a current quote beside the requested historical row.
    Persisting that whole response would leak information after a historical
    replay cutoff, so only the requested completed daily bar crosses the
    acquisition boundary.
    """
    parsed = urlsplit(url)
    if parsed.netloc.casefold() != "web.ifzq.gtimg.cn" or parsed.path != "/appstock/app/fqkline/get":
        return None
    param = (parse_qs(parsed.query).get("param") or [""])[0].split(",")
    if len(param) < 5 or param[1] != "day":
        return None
    symbol = param[0]
    day_start = body.find('"day":')
    qt_match = re.search(r',\s*"qt"', body[day_start:]) if day_start >= 0 else None
    if day_start < 0 or qt_match is None:
        return None
    day_end = day_start + qt_match.start()
    row_pattern = re.compile(
        r'"(20\d{2}-\d{2}-\d{2})"\s*,\s*"([^"\\]+)"\s*,\s*"([^"\\]+)"\s*,\s*'
        r'"([^"\\]+)"\s*,\s*"([^"\\]+)"\s*,\s*"([^"\\]+)"'
    )
    series = [list(match.groups()) for match in row_pattern.finditer(body[day_start:day_end])]
    cutoff = _aware_utc(not_after)
    candidates: list[tuple[datetime, list[Any]]] = []
    for row in series:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            local_close = datetime.fromisoformat(str(row[0]) + "T15:00:00").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        except ValueError:
            continue
        utc_close = local_close.astimezone(timezone.utc)
        if cutoff is None or utc_close <= cutoff:
            candidates.append((utc_close, row))
    if not candidates:
        return None
    fact_time, row = max(candidates, key=lambda item: item[0])
    fields = {
        "source": "Tencent public historical daily kline",
        "symbol": symbol,
        "date": str(row[0]),
        "open": str(row[1]),
        "close": str(row[2]),
        "high": str(row[3]),
        "low": str(row[4]),
        "volume": str(row[5]),
    }
    return {
        "title": f"腾讯证券公开历史日线 {symbol} {row[0]}",
        "excerpt_text": json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "fact_as_of": fact_time.isoformat().replace("+00:00", "Z"),
    }


def _aware_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
