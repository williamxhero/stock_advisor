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
    "cn_equity_identity": "cn_equity_identity",
    "cn_equity_quote_batch": "cn_equity_quote_batch",
    "cn_market_index_batch": "cn_market_index_batch",
    "cn_market_snapshot": "cn_market_snapshot",
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
import os
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen


sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


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


def browser_executable() -> str | None:
    configured = os.environ.get("AI_TRADING_COMPANION_BROWSER_EXECUTABLE", "").strip()
    candidates = [configured, shutil.which("chrome"), shutil.which("chrome.exe")]
    for root in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if root:
            candidates.extend((
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            ))
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def capture_dynamic(url: str) -> str | None:
    if os.environ.get("AI_TRADING_COMPANION_DISABLE_DYNAMIC_BROWSER") == "1":
        return None
    executable = browser_executable()
    if not executable:
        return None
    with tempfile.TemporaryDirectory(prefix="ai-trading-browser-") as profile:
        try:
            completed = subprocess.run([
                executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-extensions", "--user-data-dir=" + profile,
                "--virtual-time-budget=1200", "--dump-dom", url,
            ], capture_output=True, timeout=12, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def result(data: dict[str, object], *, fact_as_of: str | None = None) -> None:
    print(json.dumps({
        "contract": "ai-trading-tool-result/v1",
        "fact_as_of": fact_as_of or dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "data": data,
    }, ensure_ascii=False, separators=(",", ":")))


def identity(symbol: object) -> dict[str, str]:
    code = str(symbol or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        fail(64, "A-share symbols must be six digits")
    if code.startswith(("6", "9")):
        exchange, vendor_prefix = "SSE", "sh"
    elif code.startswith(("0", "2", "3")):
        exchange, vendor_prefix = "SZSE", "sz"
    elif code.startswith(("4", "8")):
        exchange, vendor_prefix = "BSE", "bj"
    else:
        fail(64, "unsupported A-share symbol")
    return {"symbol": code, "exchange": exchange, "market": "CN-A", "vendor_symbol": vendor_prefix + code}


def china_timestamp(compact: str) -> tuple[str, str]:
    if not re.fullmatch(r"20\d{12}", compact):
        fail(75, "quote timestamp is invalid")
    moment = dt.datetime.strptime(compact, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return moment.isoformat(), moment.date().isoformat()


def quote_payload(body: str, symbols: list[dict[str, str]], finality: str) -> tuple[dict[str, object], str]:
    records = {match.group(1): match.group(2).split("~") for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', body)}
    quotes: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for item in symbols:
        fields = records.get(item["vendor_symbol"])
        if not fields or len(fields) < 5 or fields[2] != item["symbol"]:
            fail(75, "quote response has missing or mismatched symbol")
        timestamp = next((field for field in fields if re.fullmatch(r"20\d{12}", field)), "")
        quote_at, trading_date = china_timestamp(timestamp)
        moment = dt.datetime.fromisoformat(quote_at)
        latest = max(latest, moment) if latest else moment
        try:
            price = float(fields[3])
            previous_close = float(fields[4])
        except ValueError:
            fail(75, "quote price is invalid")
        if price <= 0 or previous_close < 0 or not fields[1].strip():
            fail(75, "quote identity or price is invalid")
        close_ready = moment.time() >= dt.time(15, 0)
        if finality in {"close", "official_close"} and not close_ready:
            fail(75, "quote does not meet close finality")
        quotes.append({
            "symbol": item["symbol"], "name": fields[1].strip(), "exchange": item["exchange"], "market": item["market"],
            "price": price, "previous_close": previous_close, "quote_at": quote_at, "trading_date": trading_date,
            "status": "closed" if close_ready else "trading", "source": "tencent_quote",
        })
    if latest is None:
        fail(64, "at least one symbol is required")
    return {"quotes": quotes, "finality": finality, "source": "tencent_quote"}, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def index_identity(symbol: object) -> dict[str, str]:
    code = str(symbol or "").strip()
    known = {
        "000001": ("SSE", "sh000001"), "000300": ("SSE", "sh000300"), "000905": ("SSE", "sh000905"),
        "399001": ("SZSE", "sz399001"), "399006": ("SZSE", "sz399006"),
    }
    if code not in known:
        fail(64, "unsupported market index")
    exchange, vendor_symbol = known[code]
    return {"symbol": code, "exchange": exchange, "vendor_symbol": vendor_symbol}


def index_payload(body: str, symbols: list[dict[str, str]], finality: str) -> tuple[dict[str, object], str]:
    records = {match.group(1): match.group(2).split("~") for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', body)}
    indices: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for item in symbols:
        fields = records.get(item["vendor_symbol"])
        if not fields or len(fields) < 5 or fields[2] != item["symbol"]:
            fail(75, "index response has missing or mismatched symbol")
        timestamp = next((field for field in fields if re.fullmatch(r"20\d{12}", field)), "")
        quote_at, trading_date = china_timestamp(timestamp)
        moment = dt.datetime.fromisoformat(quote_at)
        latest = max(latest, moment) if latest else moment
        try:
            price = float(fields[3])
            previous_close = float(fields[4])
        except ValueError:
            fail(75, "index price is invalid")
        if price <= 0 or not fields[1].strip():
            fail(75, "index identity or price is invalid")
        close_ready = moment.time() >= dt.time(15, 0)
        if finality in {"close", "official_close"} and not close_ready:
            fail(75, "index does not meet close finality")
        indices.append({
            "symbol": item["symbol"], "name": fields[1].strip(), "exchange": item["exchange"], "price": price,
            "previous_close": previous_close, "quote_at": quote_at, "trading_date": trading_date,
            "status": "closed" if close_ready else "trading", "source": "tencent_quote",
        })
    if latest is None:
        fail(64, "at least one market index is required")
    return {"indices": indices, "finality": finality, "source": "tencent_quote"}, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def market_snapshot_payload(body: str, finality: str) -> tuple[dict[str, object], str]:
    try:
        snapshot = json.loads(body)
    except json.JSONDecodeError:
        fail(75, "market snapshot response is not JSON")
    if not isinstance(snapshot, dict):
        fail(75, "market snapshot response is not an object")
    fact_as_of = str(snapshot.get("fact_as_of") or "")
    try:
        moment = dt.datetime.fromisoformat(fact_as_of.replace("Z", "+00:00"))
    except ValueError:
        fail(75, "market snapshot fact time is invalid")
    data = {key: snapshot.get(key) for key in ("is_trading_day", "trading_date", "source", "indices", "breadth", "industries", "themes")}
    data["finality"] = finality
    return data, moment.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
    if mode == "cn_equity_identity":
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail(64, "symbols must be a non-empty array")
        result({"identities": [identity(symbol) for symbol in symbols], "source": "a_share_code_rules"})
        return
    if mode == "cn_equity_quote_batch":
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail(64, "symbols must be a non-empty array")
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported quote finality")
        normalized = [identity(symbol) for symbol in symbols]
        quote_url = safe_url(inputs.get("quote_url") or "https://qt.gtimg.cn/q=")
        separator = "" if quote_url.endswith(("=", ",")) else "&q="
        _url, body = fetch(quote_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
        payload, fact_as_of = quote_payload(body, normalized, finality)
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_market_index_batch":
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail(64, "symbols must be a non-empty array")
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported index finality")
        normalized = [index_identity(symbol) for symbol in symbols]
        index_url = safe_url(inputs.get("index_url") or "https://qt.gtimg.cn/q=")
        separator = "" if index_url.endswith(("=", ",")) else "&q="
        _url, body = fetch(index_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
        payload, fact_as_of = index_payload(body, normalized, finality)
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_market_snapshot":
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported market finality")
        _url, body = fetch(safe_url(inputs.get("url")))
        payload, fact_as_of = market_snapshot_payload(body, finality)
        result(payload, fact_as_of=fact_as_of)
        return
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
    url = safe_url(inputs.get("url"))
    if mode == "browser_capture":
        dynamic = capture_dynamic(url)
        if dynamic is not None:
            result({"url": url, "capture_mode": "dynamic", "text": strip_html(dynamic)})
            return
    url, body = fetch(url)
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
