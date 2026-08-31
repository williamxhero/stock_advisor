"""Install the first read-only, short-lived CLI data tools into the mutable tools root."""
from __future__ import annotations

import json
import sys
from pathlib import Path


_VERSION = "1.0.0"
_CAPABILITIES = {
    "generic_http_json": "http_json",
    "generic_web_read": "web_read",
    "generic_browser_capture": "browser_capture",
    "generic_web_search": "web_search",
    "cninfo_search": "cninfo_search",
    "article_range": "article_range",
}


def ensure_builtin_tools(root: Path) -> None:
    """Materialize immutable stdlib-only CLI packages without a resident service."""
    root = Path(root)
    for capability, mode in _CAPABILITIES.items():
        version_root = root / capability / "versions" / _VERSION
        manifest = version_root / "manifest.json"
        if not manifest.exists():
            version_root.mkdir(parents=True, exist_ok=True)
            (version_root / "tool.py").write_text(_CLI, encoding="utf-8")
            manifest.write_text(json.dumps({
                "contract": "ai-trading-tool-manifest/v1", "capability": capability,
                "version": _VERSION, "state": "promoted",
                "command": [sys.executable, "tool.py", mode],
            }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        current = root / capability / "current.json"
        current.parent.mkdir(parents=True, exist_ok=True)
        if not current.exists():
            current.write_text(json.dumps({
                "contract": "ai-trading-tool-current/v1", "version": _VERSION,
            }, sort_keys=True), encoding="utf-8")


_CLI = r'''from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen


def fail(code: int, message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def safe_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    lowered = url.lower()
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        fail(64, "only public http(s) URLs are supported")
    if parsed.username or parsed.password or any(word in lowered for word in ("/login", "/signin", "/auth", "password=", "token=", "cookie=", "apikey=", "api_key=")):
        fail(64, "access-controlled URLs are not supported")
    return url


def fetch(url: str) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": "AITradingCompanion-ReadOnly/1"})
    try:
        with urlopen(request, timeout=15) as response:
            status = getattr(response, "status", 200)
            if status in {401, 402, 403}:
                fail(64, "access-controlled response")
            if status >= 400:
                fail(75, f"upstream HTTP {status}")
            raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                fail(75, "response too large")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.geturl(), raw.decode(charset, errors="replace")
    except SystemExit:
        raise
    except Exception as exc:
        fail(75, f"network read failed: {type(exc).__name__}")


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return " ".join(html.unescape(value).split())[:200_000]


def result(data: dict[str, object]) -> None:
    print(json.dumps({
        "contract": "ai-trading-tool-result/v1",
        "fact_as_of": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": data,
    }, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    if len(sys.argv) != 2:
        fail(64, "tool mode is required")
    try:
        request = json.load(sys.stdin)
    except Exception:
        fail(64, "one JSON request is required")
    inputs = request.get("inputs") if isinstance(request, dict) else None
    if not isinstance(inputs, dict):
        fail(64, "inputs must be an object")
    mode = sys.argv[1]
    if mode in {"cninfo_search", "article_range"}:
        base = safe_url(inputs.get("base_url") or "http://yosef-server:8815").rstrip("/")
        if mode == "cninfo_search":
            query = str(inputs.get("q") or "").strip()
            if not query:
                fail(64, "q is required")
            url, body = fetch(base + "/api/cninfo/search?q=" + quote_plus(query))
        else:
            source = str(inputs.get("source") or "all")
            start_date = str(inputs.get("start_date") or "")
            end_date = str(inputs.get("end_date") or "")
            if not start_date or not end_date:
                fail(64, "start_date and end_date are required")
            if source not in {"all", "cninfo_disclosure", "eastmoney_stock_report", "eastmoney_broker_report", "eastmoney_daily_topic_report", "cls_depth_article", "ths_important_news"}:
                fail(64, "unsupported article source")
            url, body = fetch(base + "/api/articles/range?source=" + quote_plus(source) + "&start_date=" + quote_plus(start_date) + "&end_date=" + quote_plus(end_date))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            fail(75, "article service response is not JSON")
        if not isinstance(payload, dict):
            fail(75, "article service response is not an object")
        result({"url": url, **payload})
        return
    if mode == "web_search":
        query = str(inputs.get("query") or "").strip()
        if not query:
            fail(64, "query is required")
        url, page = fetch("https://html.duckduckgo.com/html/?q=" + quote_plus(query))
        links = re.findall(r'(?is)class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page)
        result({"url": url, "query": query, "results": [{"url": html.unescape(link), "title": strip_html(title)} for link, title in links[:10]]})
        return
    url, body = fetch(safe_url(inputs.get("url")))
    if mode == "http_json":
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            fail(75, "response is not JSON")
        result({"url": url, "json": parsed})
    elif mode == "web_read":
        result({"url": url, "text": strip_html(body)})
    elif mode == "browser_capture":
        result({"url": url, "capture_mode": "static", "text": strip_html(body)})
    else:
        fail(64, "unsupported tool mode")


if __name__ == "__main__":
    main()
'''
