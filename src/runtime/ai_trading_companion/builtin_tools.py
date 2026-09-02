"""Install the first read-only, short-lived CLI data tools into the mutable tools root."""
from __future__ import annotations

import json
import sys
from pathlib import Path


_VERSION = "1.1.1"
_PREVIOUS_BUILTIN_VERSIONS = {"1.1.0"}
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
    "cn_market_breadth": "cn_market_breadth",
}
_ADAPTERS = {
    "cn_equity_quote_batch": {"tencent": "cn_equity_quote_tencent", "sina": "cn_equity_quote_sina"},
    "cn_market_index_batch": {"tencent": "cn_market_index_tencent", "sina": "cn_market_index_sina"},
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
        _promote_builtin_current(current)
    for capability, adapters in _ADAPTERS.items():
        for adapter, mode in adapters.items():
            version_root = root / capability / "adapters" / adapter / "versions" / _VERSION
            manifest = version_root / "manifest.json"
            if not manifest.exists():
                version_root.mkdir(parents=True, exist_ok=True)
                (version_root / "tool.py").write_text(_CLI, encoding="utf-8")
                manifest.write_text(json.dumps({
                    "contract": "ai-trading-tool-manifest/v1", "capability": capability,
                    "version": _VERSION, "state": "promoted",
                    "command": [sys.executable, "tool.py", mode],
                }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        routing = root / capability / "routing.json"
        if _routing_is_managed_builtin(routing, set(adapters)):
            routing.write_text(json.dumps({
                "contract": "ai-trading-tool-routing/v1",
                "candidates": [{"adapter": adapter, "version": _VERSION} for adapter in adapters],
            }, sort_keys=True), encoding="utf-8")


def _promote_builtin_current(current: Path) -> None:
    try:
        selected = json.loads(current.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        selected = None
    if current.exists() and not (
        isinstance(selected, dict)
        and selected.get("contract") == "ai-trading-tool-current/v1"
        and selected.get("version") in _PREVIOUS_BUILTIN_VERSIONS
    ):
        return
    current.write_text(json.dumps({
        "contract": "ai-trading-tool-current/v1", "version": _VERSION,
    }, sort_keys=True), encoding="utf-8")


def _routing_is_managed_builtin(routing: Path, adapters: set[str]) -> bool:
    try:
        selected = json.loads(routing.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except json.JSONDecodeError:
        return False
    candidates = selected.get("candidates") if isinstance(selected, dict) else None
    return bool(
        selected.get("contract") == "ai-trading-tool-routing/v1"
        and isinstance(candidates, list)
        and {str(row.get("adapter") or "") for row in candidates if isinstance(row, dict)} == adapters
        and all(row.get("version") in _PREVIOUS_BUILTIN_VERSIONS for row in candidates if isinstance(row, dict))
    )


_CLI = r'''from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
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
            process = subprocess.Popen([
                executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-extensions", "--user-data-dir=" + profile,
                "--virtual-time-budget=1200", "--dump-dom", url,
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, _stderr = process.communicate(timeout=12)
        except OSError:
            return None
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.kill()
            process.communicate()
            return None
    if process.returncode != 0 or not stdout:
        return None
    return stdout.decode("utf-8", errors="replace")


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


def sina_payload(body: str, symbols: list[dict[str, str]], finality: str, *, kind: str) -> tuple[dict[str, object], str]:
    records = {match.group(1): match.group(2).split(",") for match in re.finditer(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";', body)}
    rows: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for item in symbols:
        fields = records.get(item["vendor_symbol"])
        if not fields or len(fields) < 32 or not fields[0].strip():
            fail(75, "sina quote response has missing or mismatched symbol")
        try:
            price, previous_close = float(fields[3]), float(fields[2])
            moment = dt.datetime.strptime(fields[30] + " " + fields[31], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=dt.timezone(dt.timedelta(hours=8)))
        except (ValueError, IndexError):
            fail(75, "sina quote timestamp or price is invalid")
        if price <= 0 or previous_close < 0:
            fail(75, "sina quote price is invalid")
        close_ready = moment.time() >= dt.time(15, 0)
        if finality in {"close", "official_close"} and not close_ready:
            fail(75, "sina quote does not meet close finality")
        latest = max(latest, moment) if latest else moment
        row = {
            "symbol": item["symbol"], "name": fields[0].strip(), "exchange": item["exchange"],
            "price": price, "previous_close": previous_close,
            "quote_at": moment.isoformat(), "trading_date": moment.date().isoformat(),
            "status": "closed" if close_ready else "trading", "source": "sina_quote",
        }
        if kind == "equity":
            row["market"] = "CN-A"
        rows.append(row)
    if latest is None:
        fail(64, "at least one symbol is required")
    key = "quotes" if kind == "equity" else "indices"
    return {key: rows, "finality": finality, "source": "sina_quote"}, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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


def breadth_page_url(endpoint: str, page: int) -> str:
    separator = "&" if "?" in endpoint else "?"
    return endpoint + separator + (
        "pn=" + str(page) + "&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        "&fltt=2&invt=2&fid=f3&fs=m%3A0%2Bt%3A6%2Cm%3A0%2Bt%3A80"
        "&fields=f12%2Cf14%2Cf2%2Cf3%2Cf124"
    )


def market_breadth_payload(endpoint: str, finality: str) -> tuple[dict[str, object], str]:
    first_url, first_body = fetch(breadth_page_url(endpoint, 1))
    try:
        first = json.loads(first_body)
        first_data = first["data"]
        total = int(first_data["total"])
        rows = list(first_data["diff"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail(75, "market breadth response is invalid")
    if total <= 0 or not all(isinstance(row, dict) for row in rows):
        fail(75, "market breadth response is empty")
    pages = min(64, (total + 99) // 100)
    if pages > 1:
        def read_page(page: int) -> list[object]:
            _page_url, page_body = fetch(breadth_page_url(endpoint, page))
            try:
                value = json.loads(page_body)["data"]["diff"]
            except (KeyError, TypeError, json.JSONDecodeError):
                fail(75, "market breadth page is invalid")
            return value if isinstance(value, list) else []
        with ThreadPoolExecutor(max_workers=min(8, pages - 1)) as pool:
            for page_rows in pool.map(read_page, range(2, pages + 1)):
                rows.extend(row for row in page_rows if isinstance(row, dict))
    if len(rows) < total:
        fail(75, "market breadth response is incomplete")
    moments: list[dt.datetime] = []
    up = down = flat = limit_up = limit_down = unpriced = 0
    for row in rows[:total]:
        try:
            moment = dt.datetime.fromtimestamp(int(row["f124"]), tz=dt.timezone.utc)
        except (KeyError, TypeError, ValueError, OSError):
            fail(75, "market breadth quote is invalid")
        moments.append(moment)
        try:
            change = float(row["f3"])
        except (KeyError, TypeError, ValueError):
            unpriced += 1
            continue
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        limit_up += int(change >= 9.9)
        limit_down += int(change <= -9.9)
    observed = max(moments)
    local = observed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    if finality in {"close", "official_close"} and local.time() < dt.time(15, 0):
        fail(75, "market breadth does not meet close finality")
    data = {
        "is_trading_day": True, "trading_date": local.date().isoformat(), "source": "eastmoney_breadth",
        "source_urls": [first_url],
        "breadth": {
            "up": up, "down": down, "flat": flat, "limit_up": limit_up, "limit_down": limit_down,
            "universe_count": total, "unpriced": unpriced,
            "limit_count_basis": "percent_change_threshold_candidates",
        },
        "finality": finality,
    }
    fact_as_of = observed.isoformat().replace("+00:00", "Z")
    data["source_evidence"] = [{"url": first_url, "fact_as_of": fact_as_of, "data": {
        "trading_date": data["trading_date"], "breadth": data["breadth"], "finality": finality,
    }}]
    return data, fact_as_of


def default_market_snapshot(inputs: dict[str, object], finality: str) -> tuple[dict[str, object], str]:
    normalized = [index_identity(symbol) for symbol in ("000001", "399001", "399006")]
    index_url = safe_url(inputs.get("index_url") or "https://qt.gtimg.cn/q=")
    separator = "" if index_url.endswith(("=", ",")) else "&q="
    index_source_url, index_body = fetch(index_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
    index_data, index_fact_as_of = index_payload(index_body, normalized, finality)
    breadth_endpoint = safe_url(inputs.get("breadth_url") or "https://push2delay.eastmoney.com/api/qt/clist/get")
    breadth_data, breadth_fact_as_of = market_breadth_payload(breadth_endpoint, finality)
    index_moment = dt.datetime.fromisoformat(index_fact_as_of.replace("Z", "+00:00"))
    breadth_moment = dt.datetime.fromisoformat(breadth_fact_as_of.replace("Z", "+00:00"))
    fact_as_of = min(index_moment, breadth_moment).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "is_trading_day": True, "trading_date": breadth_data["trading_date"],
        "source": "tencent_quote+eastmoney_breadth", "indices": index_data["indices"],
        "source_urls": [index_source_url, *breadth_data["source_urls"]],
        "source_evidence": [
            {"url": index_source_url, "fact_as_of": index_fact_as_of, "data": {"indices": index_data["indices"], "finality": finality}},
            *breadth_data["source_evidence"],
        ],
        "breadth": breadth_data["breadth"], "industries": [], "themes": [], "finality": finality,
    }, fact_as_of


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
    if mode in {"cn_equity_quote_batch", "cn_equity_quote_tencent", "cn_equity_quote_sina"}:
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail(64, "symbols must be a non-empty array")
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported quote finality")
        normalized = [identity(symbol) for symbol in symbols]
        if mode == "cn_equity_quote_sina":
            quote_url = safe_url(inputs.get("sina_quote_url") or "https://hq.sinajs.cn/list=")
            separator = "" if quote_url.endswith(("=", ",")) else "&list="
            _url, body = fetch(quote_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
            payload, fact_as_of = sina_payload(body, normalized, finality, kind="equity")
            result(payload, fact_as_of=fact_as_of)
            return
        quote_url = safe_url(inputs.get("tencent_quote_url") or inputs.get("quote_url") or "https://qt.gtimg.cn/q=")
        separator = "" if quote_url.endswith(("=", ",")) else "&q="
        _url, body = fetch(quote_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
        payload, fact_as_of = quote_payload(body, normalized, finality)
        result(payload, fact_as_of=fact_as_of)
        return
    if mode in {"cn_market_index_batch", "cn_market_index_tencent", "cn_market_index_sina"}:
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols:
            fail(64, "symbols must be a non-empty array")
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported index finality")
        normalized = [index_identity(symbol) for symbol in symbols]
        if mode == "cn_market_index_sina":
            index_url = safe_url(inputs.get("sina_index_url") or "https://hq.sinajs.cn/list=")
            separator = "" if index_url.endswith(("=", ",")) else "&list="
            _url, body = fetch(index_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
            payload, fact_as_of = sina_payload(body, normalized, finality, kind="index")
            result(payload, fact_as_of=fact_as_of)
            return
        index_url = safe_url(inputs.get("tencent_index_url") or inputs.get("index_url") or "https://qt.gtimg.cn/q=")
        separator = "" if index_url.endswith(("=", ",")) else "&q="
        _url, body = fetch(index_url + separator + ",".join(item["vendor_symbol"] for item in normalized))
        payload, fact_as_of = index_payload(body, normalized, finality)
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_market_breadth":
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported market finality")
        payload, fact_as_of = market_breadth_payload(
            safe_url(inputs.get("breadth_url") or "https://push2delay.eastmoney.com/api/qt/clist/get"), finality,
        )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_market_snapshot":
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported market finality")
        if inputs.get("url"):
            _url, body = fetch(safe_url(inputs.get("url")))
            payload, fact_as_of = market_snapshot_payload(body, finality)
        else:
            payload, fact_as_of = default_market_snapshot(inputs, finality)
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
        base = safe_url(inputs.get("base_url") or "http://yosef-server:8801").rstrip("/")
        url, body = fetch(base + "/search?q=" + quote_plus(query) + "&format=json")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            fail(75, "search service response is not JSON")
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            fail(75, "search service results are invalid")
        results = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            results.append({
                "url": safe_url(row.get("url")),
                "title": strip_html(str(row.get("title") or "")),
                "snippet": strip_html(str(row.get("content") or "")),
            })
            if len(results) >= 10:
                break
        result({"url": url, "query": query, "results": results})
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
