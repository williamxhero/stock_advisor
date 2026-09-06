"""Install the first read-only, short-lived CLI data tools into the mutable tools root."""
from __future__ import annotations

import json
import sys
from pathlib import Path


_VERSION = "1.1.15"
_PREVIOUS_BUILTIN_VERSIONS = {"1.1.0", "1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6", "1.1.7", "1.1.8", "1.1.9", "1.1.10", "1.1.11", "1.1.12", "1.1.13", "1.1.14"}
_CAPABILITIES = {
    "generic_http_json": "http_json",
    "generic_web_read": "web_read",
    "generic_browser_capture": "browser_capture",
    "generic_web_search": "web_search",
    "cninfo_search": "cninfo_search",
    "article_range": "article_range",
    "cn_equity_identity": "cn_equity_identity",
    "cn_equity_quote_batch": "cn_equity_quote_batch",
    "cn_equity_current_bar": "cn_equity_current_bar",
    "cn_market_index_batch": "cn_market_index_batch",
    "cn_market_snapshot": "cn_market_snapshot",
    "cn_market_breadth": "cn_market_breadth",
    "cn_market_turnover_compare": "cn_market_turnover_compare",
    "cn_market_sector_snapshot": "cn_market_sector_snapshot",
    "cn_market_fund_flow_snapshot": "cn_market_fund_flow_snapshot_eastmoney_history",
    "cn_market_event_snapshot": "cn_market_event_snapshot",
    "cn_equity_announcement_snapshot": "cn_equity_announcement_snapshot",
}
_ADAPTERS = {
    "cn_equity_quote_batch": {"tencent": "cn_equity_quote_tencent", "sina": "cn_equity_quote_sina"},
    "cn_equity_current_bar": {"markethub": "cn_equity_current_bar", "tencent": "cn_equity_current_bar_tencent"},
    "cn_market_index_batch": {"tencent": "cn_market_index_tencent", "sina": "cn_market_index_sina"},
    "cn_market_breadth": {"markethub": "cn_market_breadth_markethub", "eastmoney": "cn_market_breadth_eastmoney"},
    "cn_market_turnover_compare": {
        "eastmoney_history": "cn_market_turnover_compare_eastmoney_history",
        "official_exchanges": "cn_market_turnover_compare_official_exchanges",
        "eastmoney_spot_markethub": "cn_market_turnover_compare_eastmoney_spot_markethub",
        "tencent_spot_markethub": "cn_market_turnover_compare_tencent_spot_markethub",
    },
    "cn_market_sector_snapshot": {
        "eastmoney": "cn_market_sector_snapshot_eastmoney",
        "markethub": "cn_market_sector_snapshot_markethub",
    },
    "cn_market_fund_flow_snapshot": {
        "eastmoney_history": "cn_market_fund_flow_snapshot_eastmoney_history",
        "eastmoney_history_alt": "cn_market_fund_flow_snapshot_eastmoney_history_alt",
        "article_digest": "cn_market_fund_flow_snapshot_article_digest",
    },
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
                manifest_payload = {
                    "contract": "ai-trading-tool-manifest/v1", "capability": capability,
                    "version": _VERSION, "state": "promoted",
                    "command": [sys.executable, "tool.py", mode],
                }
                if capability == "cn_market_turnover_compare" and adapter == "official_exchanges":
                    manifest_payload["egress"] = {
                        "allowed_domains": ["query.sse.com.cn", "www.szse.cn"],
                    }
                manifest.write_text(json.dumps(
                    manifest_payload, ensure_ascii=False, sort_keys=True,
                ), encoding="utf-8")
        routing = root / capability / "routing.json"
        legacy_adapter_sets = (
            ({"eastmoney", "markethub"},)
            if capability == "cn_market_turnover_compare"
            else (
                {"eastmoney_history", "eastmoney_history_alt"},
                {"eastmoney_history", "eastmoney_live"},
            )
            if capability == "cn_market_fund_flow_snapshot"
            else ()
        )
        if _routing_is_managed_builtin(routing, set(adapters), legacy_adapter_sets):
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


def _routing_is_managed_builtin(
    routing: Path, adapters: set[str], legacy_adapter_sets: tuple[set[str], ...] = (),
) -> bool:
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
        and {str(row.get("adapter") or "") for row in candidates if isinstance(row, dict)}
        in (adapters, *legacy_adapter_sets)
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
from decimal import Decimal, InvalidOperation
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen


sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def fail(code: int, message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def clean_text(value: object) -> str:
    return str(value or "").encode("utf-8", errors="replace").decode("utf-8")


def safe_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    lowered = url.lower()
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        fail(64, "only public http(s) URLs are supported")
    if parsed.username or parsed.password or any(word in lowered for word in ("/login", "/signin", "/auth", "password=", "token=", "cookie=", "apikey=", "api_key=")):
        fail(64, "access-controlled URLs are not supported")
    return url


def fetch(
    url: str, *, referer: str | None = None, curl_fallback: bool = False,
) -> tuple[str, str]:
    headers = {"User-Agent": (
        "Mozilla/5.0"
        if referer else "AITradingCompanion-ReadOnly/1"
    )}
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for _attempt in range(2):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=12) as response:
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
            last_error = exc
    curl = (shutil.which("curl") or shutil.which("curl.exe")) if curl_fallback else None
    if curl:
        command = [
            curl, "--silent", "--show-error", "--location", "--max-time", "12",
            "--max-filesize", "1000000", "--user-agent", headers["User-Agent"],
        ]
        if referer:
            command.extend(("--referer", referer))
        try:
            completed = subprocess.run(command + [url], capture_output=True, timeout=14)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = exc
        else:
            decoded = completed.stdout.decode("utf-8", errors="replace")
            if len(completed.stdout) <= 1_000_000:
                try:
                    complete_json = json.loads(decoded)
                except json.JSONDecodeError:
                    complete_json = None
                if completed.returncode == 0 or (
                    curl_fallback and isinstance(complete_json, dict)
                    and isinstance(complete_json.get("data"), dict)
                ):
                    return url, decoded
            last_error = RuntimeError(f"curl_exit_{completed.returncode}")
    fail(75, f"network read failed after retry: {type(last_error).__name__}")


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
            "change": round(price - previous_close, 4),
            "change_percent": round((price - previous_close) / previous_close * 100, 4) if previous_close > 0 else 0.0,
            "status": "closed" if close_ready else "trading", "source": "tencent_quote",
        })
    if latest is None:
        fail(64, "at least one symbol is required")
    return {"quotes": quotes, "finality": finality, "source": "tencent_quote"}, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def current_bar_payload(body: str, symbols: list[dict[str, str]], freq: str, finality: str, source_url: str) -> tuple[dict[str, object], str]:
    try:
        response = json.loads(body)
        items = response["items"]
        meta = response["meta"]
    except (KeyError, TypeError, json.JSONDecodeError):
        fail(75, "MarketHub current Bar response is invalid")
    if not isinstance(items, list) or not isinstance(meta, dict) or meta.get("complete") is not True:
        fail(75, "MarketHub current Bar response is incomplete")
    by_code = {str(row.get("code") or ""): row for row in items if isinstance(row, dict)}
    if len(by_code) != len(items):
        fail(75, "MarketHub current Bar response has duplicate symbols")
    if set(by_code) != {item["symbol"] for item in symbols}:
        fail(75, "MarketHub current Bar response has missing or mismatched symbol")
    bars: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for identity_row in symbols:
        row = by_code.get(identity_row["symbol"])
        if row is None or str(row.get("freq") or "") != freq:
            fail(75, "MarketHub current Bar response has missing or mismatched symbol")
        try:
            interval_start = dt.datetime.fromisoformat(str(row["interval_start"]))
            interval_end = dt.datetime.fromisoformat(str(row["interval_end"]))
            observed_at = dt.datetime.fromisoformat(str(row["observed_at"]))
            last_trade_at = dt.datetime.fromisoformat(str(row["last_trade_at"]))
            open_price, high, low, close = (float(row[field]) for field in ("open", "high", "low", "close"))
            volume, amount = float(row["volume"]), float(row["amount"])
            freshness_ms = int(row["freshness_ms"])
        except (KeyError, TypeError, ValueError):
            fail(75, "MarketHub current Bar values are invalid")
        if any(moment.tzinfo is None for moment in (interval_start, interval_end, observed_at, last_trade_at)):
            fail(75, "MarketHub current Bar timestamp is invalid")
        if interval_end <= interval_start or not (low <= open_price <= high and low <= close <= high):
            fail(75, "MarketHub current Bar interval or OHLC is invalid")
        if volume < 0 or amount < 0 or freshness_ms < 0 or freshness_ms > 300_000:
            fail(75, "MarketHub current Bar freshness or volume is invalid")
        is_final = row.get("is_final")
        degraded = row.get("degraded")
        if not isinstance(is_final, bool) or not isinstance(degraded, bool):
            fail(75, "MarketHub current Bar status is invalid")
        if finality in {"close", "official_close"} and not is_final:
            fail(75, "MarketHub current Bar does not meet finality")
        source_semantics = str(row.get("source_semantics") or "")
        if source_semantics not in {"native", "derived"}:
            fail(75, "MarketHub current Bar source semantics is invalid")
        provider = str(row.get("provider") or "").strip()
        market_status = str(row.get("market_status") or "").strip()
        if not provider or not market_status:
            fail(75, "MarketHub current Bar source is invalid")
        latest = max(latest, observed_at) if latest else observed_at
        bars.append({
            "symbol": identity_row["symbol"], "exchange": identity_row["exchange"], "market": identity_row["market"],
            "freq": freq, "trade_time": str(row.get("trade_time") or ""),
            "interval_start": interval_start.isoformat(), "interval_end": interval_end.isoformat(),
            "open": open_price, "high": high, "low": low, "close": close, "volume": volume, "amount": amount,
            "is_suspended": bool(row.get("is_suspended")), "is_st": bool(row.get("is_st")),
            "is_final": is_final, "observed_at": observed_at.isoformat(), "last_trade_at": last_trade_at.isoformat(),
            "freshness_ms": freshness_ms, "degraded": degraded, "market_status": market_status,
            "provider": provider, "source_semantics": source_semantics, "source": "markethub_current_bar",
        })
    if latest is None:
        fail(75, "MarketHub current Bar response is empty")
    fact_as_of = latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = {"bars": bars, "finality": finality, "source": "markethub_current_bar"}
    return {**evidence, "source_urls": [source_url], "source_evidence": [{
        "url": source_url, "fact_as_of": fact_as_of, "data": evidence,
    }]}, fact_as_of


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
            "change": round(price - previous_close, 4),
            "change_percent": round((price - previous_close) / previous_close * 100, 4) if previous_close > 0 else 0.0,
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
            "change": round(price - previous_close, 4),
            "change_percent": round((price - previous_close) / previous_close * 100, 4) if previous_close > 0 else 0.0,
            "status": "closed" if close_ready else "trading", "source": "tencent_quote",
        })
    if latest is None:
        fail(64, "at least one market index is required")
    return {"indices": indices, "finality": finality, "source": "tencent_quote"}, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def minute_rows(value: object) -> list[str]:
    """Tencent nests minute rows differently for equities and indices."""
    if isinstance(value, str):
        return [value] if re.match(r"^\d{4}\s+[-+]?\d", value.strip()) else []
    if isinstance(value, list):
        return [row for item in value for row in minute_rows(item)]
    if isinstance(value, dict):
        return [row for item in value.values() for row in minute_rows(item)]
    return []


def tencent_current_bar_payload(
    symbols: list[dict[str, str]], required_at: str, freq: str, finality: str, minute_endpoint: object = None,
) -> tuple[dict[str, object], str]:
    """Convert Tencent's cumulative minute tape into a truthful derived 1m Bar.

    Tencent does not provide native OHLCVA bars at this endpoint.  The minute
    tape is therefore only accepted when it contains consecutive cumulative
    volume and amount values; the interval's close is the reported minute
    price and its open is the preceding minute price.  This is deliberately a
    narrow, independently validated fallback rather than a webpage-text path.
    """
    if freq != "1m":
        fail(64, "Tencent current Bar supports only 1m")
    try:
        cutoff = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    base = str(minute_endpoint or "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=")
    bars: list[dict[str, object]] = []
    source_evidence: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for symbol in symbols:
        source_url, body = fetch(base.replace("{symbol}", symbol["vendor_symbol"]) if "{symbol}" in base else base + symbol["vendor_symbol"])
        try:
            rows = minute_rows(json.loads(body))
        except json.JSONDecodeError:
            fail(75, "Tencent minute response is not JSON")
        tape: list[tuple[dt.datetime, float, float, float]] = []
        for row in rows:
            fields = row.strip().split()
            if len(fields) < 4 or not re.fullmatch(r"\d{4}", fields[0]):
                continue
            try:
                moment = dt.datetime.combine(cutoff.date(), dt.time(int(fields[0][:2]), int(fields[0][2:])), cutoff.tzinfo)
                price, cumulative_volume, cumulative_amount = float(fields[1]), float(fields[2]), float(fields[3])
            except ValueError:
                continue
            if price <= 0 or cumulative_volume < 0 or cumulative_amount < 0:
                fail(75, "Tencent minute row has invalid values")
            if tape and (moment <= tape[-1][0] or cumulative_volume < tape[-1][2] or cumulative_amount < tape[-1][3]):
                fail(75, "Tencent minute tape is non-monotonic")
            # The endpoint also exposes the currently forming minute.  A row
            # labelled HH:MM is only usable after its one-minute interval has
            # ended; otherwise it is future evidence relative to required_at.
            if moment + dt.timedelta(minutes=1) <= cutoff:
                tape.append((moment, price, cumulative_volume, cumulative_amount))
        if len(tape) < 2:
            fail(75, "Tencent minute tape lacks a complete interval")
        moment, close, volume_total, amount_total = tape[-1]
        prior_moment, open_price, prior_volume, prior_amount = tape[-2]
        interval_end = moment + dt.timedelta(minutes=1)
        if cutoff - interval_end > dt.timedelta(minutes=5):
            fail(75, "Tencent minute tape is stale")
        volume, amount = volume_total - prior_volume, amount_total - prior_amount
        if volume < 0 or amount < 0:
            fail(75, "Tencent minute tape is non-monotonic")
        observed_at = interval_end
        freshness_ms = int((cutoff - observed_at).total_seconds() * 1000)
        bar = {
            "symbol": symbol["symbol"], "exchange": symbol["exchange"], "market": symbol["market"],
            "freq": "1m", "trade_time": moment.isoformat(),
            "interval_start": moment.isoformat(), "interval_end": interval_end.isoformat(),
            "open": open_price, "high": max(open_price, close), "low": min(open_price, close), "close": close,
            "volume": volume, "amount": amount, "is_suspended": volume == 0,
            "is_st": False, "is_final": finality in {"close", "official_close"},
            "observed_at": observed_at.isoformat(), "last_trade_at": moment.isoformat(),
            "freshness_ms": freshness_ms, "degraded": True, "market_status": "trading",
            "provider": "tencent_minute", "source_semantics": "derived", "source": "tencent_minute_current_bar",
        }
        bars.append(bar)
        latest = max(latest, observed_at) if latest else observed_at
        source_evidence.append({"url": source_url, "fact_as_of": observed_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), "data": {"bars": [bar], "finality": finality}})
    if latest is None:
        fail(75, "Tencent minute response is empty")
    fact_as_of = latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = {"bars": bars, "finality": finality, "source": "tencent_minute_current_bar"}
    return {**evidence, "source_urls": [str(row["url"]) for row in source_evidence], "source_evidence": source_evidence}, fact_as_of


def frozen_minute(symbol: dict[str, str], required_at: str, minute_endpoint: object = None) -> tuple[float, str, str]:
    try:
        cutoff = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    base = str(minute_endpoint or "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=")
    source_url, body = fetch(base.replace("{symbol}", symbol["vendor_symbol"]) if "{symbol}" in base else base + symbol["vendor_symbol"])
    try:
        rows = minute_rows(json.loads(body))
    except json.JSONDecodeError:
        fail(75, "minute response is not JSON")
    selected: tuple[dt.datetime, float] | None = None
    for row in rows:
        fields = row.strip().split()
        if len(fields) < 2 or not re.fullmatch(r"\d{4}", fields[0]):
            continue
        try:
            moment = dt.datetime.combine(cutoff.date(), dt.time(int(fields[0][:2]), int(fields[0][2:])), cutoff.tzinfo)
            price = float(fields[1])
        except ValueError:
            continue
        if price > 0 and moment <= cutoff and (selected is None or moment > selected[0]):
            selected = (moment, price)
    if selected is None:
        fail(75, "minute response has no quote at or before required_at")
    return selected[1], selected[0].astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"), source_url


def frozen_minute_payload(
    spot: dict[str, object], symbols: list[dict[str, str]], required_at: str, kind: str,
    minute_endpoint: object = None, *, finality: str = "intraday",
) -> tuple[dict[str, object], str]:
    field = "quotes" if kind == "equity" else "indices"
    records = {str(item.get("symbol") or ""): dict(item) for item in spot.get(field, []) if isinstance(item, dict)}
    frozen: list[dict[str, object]] = []
    source_evidence: list[dict[str, object]] = []
    latest: dt.datetime | None = None
    for symbol in symbols:
        item = records.get(symbol["symbol"])
        if item is None:
            fail(75, "spot quote is missing a requested symbol")
        price, quote_at, source_url = frozen_minute(symbol, required_at, minute_endpoint)
        moment = dt.datetime.fromisoformat(quote_at.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=8)))
        closed = finality in {"close", "official_close"}
        if closed and moment.time() < dt.time(15, 0):
            fail(75, "minute response does not meet close finality")
        item.update({
            "price": price, "quote_at": quote_at,
            "trading_date": moment.date().isoformat(),
            "status": "closed" if closed else "trading", "source": "tencent_minute",
        })
        previous_close = float(item.get("previous_close") or 0)
        if previous_close <= 0:
            fail(75, "minute quote has no valid previous close")
        item["change"] = round(price - previous_close, 4)
        item["change_percent"] = round((price - previous_close) / previous_close * 100, 4)
        frozen.append(item)
        moment = dt.datetime.fromisoformat(quote_at.replace("Z", "+00:00"))
        latest = max(latest, moment) if latest else moment
        source_evidence.append({"url": source_url, "fact_as_of": quote_at, "data": {field: [item], "finality": finality}})
    if latest is None:
        fail(75, "minute response has no usable quotes")
    return {
        field: frozen, "finality": finality, "source": "tencent_minute",
        "source_urls": [str(item["url"]) for item in source_evidence], "source_evidence": source_evidence,
    }, latest.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
        "&fltt=2&invt=2&fid=f3"
        "&fs=m%3A0%2Bt%3A6%2Cm%3A0%2Bt%3A80%2Cm%3A1%2Bt%3A2%2Cm%3A1%2Bt%3A23"
        "&fields=f12%2Cf14%2Cf2%2Cf3%2Cf124"
    )


def markethub_breadth_payload(
    endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    separator = "&" if "?" in endpoint else "?"
    source_url, body = fetch(endpoint + separator + "trade_date=" + quote_plus(required.date().isoformat()))
    try:
        snapshot = json.loads(body)
        coverage = snapshot["coverage"]
        lineage = snapshot["lineage"]
        values = {
            key: int(snapshot[key]) for key in (
                "up", "down", "flat", "unpriced", "suspended", "universe_count",
            )
        }
        eligible = int(coverage["eligible_count"])
        priced = int(coverage["priced_count"])
        suspended = int(coverage["suspended_count"])
        missing = int(coverage["missing_count"])
        invalid = int(coverage["invalid_price_count"])
        accounted = int(coverage["accounted_count"])
        ratio = float(coverage["coverage_ratio"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail(75, "MarketHub breadth response is invalid")
    if (
        snapshot.get("contract") != "markethub-cn-a-share-market-breadth-v1"
        or snapshot.get("trade_date") != required.date().isoformat()
        or snapshot.get("status") != "complete"
        or snapshot.get("finality") != "final"
        or not isinstance(snapshot.get("source"), str)
        or not isinstance(lineage, dict) or not str(lineage.get("dataset_version") or "").strip()
        or any(value < 0 for value in values.values())
        or missing != 0 or invalid != 0 or values["unpriced"] != 0
        or ratio != 1.0 or eligible != values["universe_count"]
        or accounted != eligible or priced + suspended != eligible
        or values["suspended"] != suspended
        or values["up"] + values["down"] + values["flat"] != priced
    ):
        fail(75, "MarketHub breadth response is incomplete")
    try:
        observed = dt.datetime.fromisoformat(str(snapshot["fact_as_of"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        fail(75, "MarketHub breadth fact time is invalid")
    if observed.tzinfo is None:
        fail(75, "MarketHub breadth fact time is invalid")
    local_observed = observed.astimezone(dt.timezone(dt.timedelta(hours=8)))
    if local_observed.date() != required.date():
        fail(75, "MarketHub breadth trading date is invalid")
    if finality in {"close", "official_close"} and local_observed.time() != dt.time(15, 0):
        fail(75, "MarketHub breadth does not meet close finality")
    fact_as_of = observed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    breadth = {**values, "coverage_ratio": ratio}
    data = {
        "is_trading_day": True, "trading_date": required.date().isoformat(),
        "source": str(snapshot["source"]), "source_urls": [source_url],
        "breadth": breadth, "finality": finality,
        "source_finality": str(snapshot["finality"]),
        "lineage": lineage,
        "coverage": coverage,
    }
    data["source_evidence"] = [{
        "url": source_url, "fact_as_of": fact_as_of,
        "data": {"trading_date": data["trading_date"], "breadth": breadth, "finality": finality,
                 "lineage": data["lineage"]},
    }]
    return data, fact_as_of


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
    # A supplier may refresh immutable closing rows after 15:00.  The close
    # fact becomes effective at the exchange close; acquisition remains a
    # separate timestamp in the tool receipt.
    effective = (
        dt.datetime.combine(local.date(), dt.time(15, 0), local.tzinfo)
        if finality in {"close", "official_close"} else observed
    )
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
    fact_as_of = effective.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    data["source_evidence"] = [{"url": first_url, "fact_as_of": fact_as_of, "data": {
        "trading_date": data["trading_date"], "breadth": data["breadth"], "finality": finality,
    }}]
    return data, fact_as_of


def turnover_summary(data: dict[str, object]) -> str:
    current = float(data["current_amount"]) / 100_000_000
    previous = float(data["previous_amount"]) / 100_000_000
    change = float(data["change_amount"]) / 100_000_000
    ratio = float(data["change_ratio"]) * 100
    scope_note = str(data.get("scope_note") or "统一口径为SSE+SZSE、单位CNY")
    return (
        f"{data['trading_date']}两市成交额{current:.2f}亿元，"
        f"上一交易日{data['previous_trading_date']}成交额{previous:.2f}亿元，"
        f"较前一交易日{change:+.2f}亿元（{ratio:+.2f}%）；{scope_note}。"
    )


def yi_cny_amount(value: object, source: str) -> int:
    try:
        amount = Decimal(str(value).replace(",", "").strip()) * Decimal("100000000")
    except (InvalidOperation, ValueError):
        fail(75, f"{source} turnover amount is invalid")
    if amount <= 0 or amount != amount.to_integral_value():
        fail(75, f"{source} turnover amount is invalid")
    return int(amount)


def sse_official_turnover(body: str, trading_date: str) -> int | None:
    match = re.fullmatch(r"\s*jsonpCallback\((.*)\)\s*;?\s*", body, flags=re.DOTALL)
    if match is None:
        fail(75, "SSE turnover response is not valid JSONP")
    try:
        payload = json.loads(match.group(1))
        rows = payload["result"]
    except (KeyError, TypeError, json.JSONDecodeError):
        fail(75, "SSE turnover response is invalid")
    if not isinstance(rows, list):
        fail(75, "SSE turnover response is invalid")
    if not rows:
        return None
    selected = [row for row in rows if isinstance(row, dict) and str(row.get("PRODUCT_CODE") or "") == "17"]
    if len(selected) != 1:
        fail(75, "SSE turnover response has no unique stock total")
    row = selected[0]
    if str(row.get("TRADE_DATE") or "") != trading_date.replace("-", ""):
        fail(75, "SSE turnover trading date is invalid")
    return yi_cny_amount(row.get("TRADE_AMT"), "SSE")


def szse_official_turnover(body: str, trading_date: str) -> int | None:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        fail(75, "SZSE turnover response is not JSON")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        fail(75, "SZSE turnover response is invalid")
    report = payload[0]
    if report.get("error") is not None:
        fail(75, "SZSE turnover response reports an error")
    metadata = report.get("metadata")
    conditions = metadata.get("conditions") if isinstance(metadata, dict) else None
    echoed_dates = [
        str(row.get("defaultValue") or "") for row in conditions or []
        if isinstance(row, dict) and row.get("name") == "txtQueryDate"
    ]
    if echoed_dates != [trading_date]:
        fail(75, "SZSE turnover trading date is invalid")
    rows = report.get("data")
    if not isinstance(rows, list):
        fail(75, "SZSE turnover response is invalid")
    if not rows:
        return None
    selected = [
        row for row in rows if isinstance(row, dict)
        and "成交金额" in clean_text(row.get("zbmc")) and "亿元" in clean_text(row.get("zbmc"))
    ]
    if len(selected) != 1:
        fail(75, "SZSE turnover response has no unique stock amount row")
    return yi_cny_amount(selected[0].get("gp"), "SZSE")


def official_exchange_turnover_session(
    sse_endpoint: str, szse_endpoint: str, trading_date: str,
) -> dict[str, object] | None:
    sse_separator = "&" if "?" in sse_endpoint else "?"
    sse_url, sse_body = fetch(
        sse_endpoint + sse_separator + "SEARCH_DATE=" + quote_plus(trading_date),
        referer="https://www.sse.com.cn/",
    )
    szse_separator = "&" if "?" in szse_endpoint else "?"
    szse_url, szse_body = fetch(
        szse_endpoint + szse_separator + "txtQueryDate=" + quote_plus(trading_date),
        referer="https://www.szse.cn/market/stock/situation/daily/index.html",
    )
    sse_amount = sse_official_turnover(sse_body, trading_date)
    szse_amount = szse_official_turnover(szse_body, trading_date)
    if (sse_amount is None) != (szse_amount is None):
        fail(75, "official exchange turnover session is incomplete")
    if sse_amount is None or szse_amount is None:
        return None
    return {
        "trading_date": trading_date,
        "amount": sse_amount + szse_amount,
        "markets": [
            {"exchange": "SSE", "market_total_id": "PRODUCT_CODE=17", "amount": sse_amount, "unit": "CNY"},
            {"exchange": "SZSE", "market_total_id": "gp", "amount": szse_amount, "unit": "CNY"},
        ],
        "sources": [
            {"url": sse_url, "exchange": "SSE", "amount": sse_amount},
            {"url": szse_url, "exchange": "SZSE", "amount": szse_amount},
        ],
    }


def official_exchange_turnover_payload(
    sse_endpoint: str, szse_endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    target_date = required.date().isoformat()
    current = official_exchange_turnover_session(sse_endpoint, szse_endpoint, target_date)
    if current is None:
        fail(75, "official exchange turnover is not published for target date")
    previous: dict[str, object] | None = None
    for days in range(1, 11):
        candidate = (required.date() - dt.timedelta(days=days)).isoformat()
        previous = official_exchange_turnover_session(sse_endpoint, szse_endpoint, candidate)
        if previous is not None:
            break
    if previous is None:
        fail(75, "official exchange turnover has no previous common trading session")
    current_amount = int(current["amount"])
    previous_amount = int(previous["amount"])
    data: dict[str, object] = {
        "trading_date": target_date, "previous_trading_date": str(previous["trading_date"]),
        "scope": "SSE+SZSE", "scope_definition": "exchange_published_stock_total",
        "scope_note": (
            "口径为交易所公布股票合计：上交所含主板A/B股与科创板且不含股票回购，"
            "深交所采用日度概况深市合计；单位CNY，不是纯A股口径"
        ),
        "unit": "CNY", "current_amount": current_amount, "previous_amount": previous_amount,
        "change_amount": current_amount - previous_amount,
        "change_ratio": (current_amount - previous_amount) / previous_amount,
        "current_markets": current["markets"], "previous_markets": previous["markets"],
        "source": "official_sse_szse_daily_overview",
        "source_urls": [row["url"] for row in [*current["sources"], *previous["sources"]]],
        "finality": finality,
    }
    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = turnover_summary(data)
    data["source_evidence"] = [{
        "url": row["url"], "fact_as_of": fact_as_of,
        "data": {"summary": summary, "exchange": row["exchange"], "amount": row["amount"],
                 "trading_date": session["trading_date"], "unit": "CNY", "finality": finality},
    } for session in (current, previous) for row in session["sources"]]
    return data, fact_as_of


def eastmoney_turnover_payload(
    endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    target_date = required.date().isoformat()
    base = safe_url(endpoint)
    sources: list[str] = []
    by_exchange: dict[str, dict[str, float]] = {}
    for exchange, secid in (("SSE", "1.000001"), ("SZSE", "0.399106")):
        separator = "&" if "?" in base else "?"
        url, body = fetch(
            base + separator + "secid=" + quote_plus(secid)
            + "&ut=fa5fd1943c7b386f172d6893dbfba10b&klt=101&fqt=0&lmt=10&end=" + required.strftime("%Y%m%d")
            + "&fields1=f1%2Cf2%2Cf3%2Cf4%2Cf5%2Cf6"
            + "&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57%2Cf58%2Cf59%2Cf60%2Cf61",
            referer="https://quote.eastmoney.com/",
        )
        try:
            rows = json.loads(body)["data"]["klines"]
        except (KeyError, TypeError, json.JSONDecodeError):
            fail(75, "turnover history response is invalid")
        parsed: dict[str, float] = {}
        for row in rows if isinstance(rows, list) else []:
            fields = str(row).split(",")
            if len(fields) < 7:
                continue
            try:
                parsed[fields[0]] = float(fields[6])
            except ValueError:
                continue
        if target_date not in parsed:
            fail(75, "turnover history has no target trading session")
        sources.append(url)
        by_exchange[exchange] = parsed
    common_dates = sorted(set(by_exchange["SSE"]) & set(by_exchange["SZSE"]))
    previous_dates = [value for value in common_dates if value < target_date]
    if not previous_dates:
        fail(75, "turnover history has no previous common trading session")
    previous_date = previous_dates[-1]
    current_amount = sum(values[target_date] for values in by_exchange.values())
    previous_amount = sum(values[previous_date] for values in by_exchange.values())
    if current_amount < 0 or previous_amount <= 0:
        fail(75, "turnover amounts are invalid")
    data: dict[str, object] = {
        "trading_date": target_date, "previous_trading_date": previous_date,
        "scope": "SSE+SZSE", "scope_definition": "exchange_composite_index_turnover",
        "scope_note": "口径为上证综合指数与深证综合指数成交额、单位CNY",
        "unit": "CNY", "current_amount": current_amount,
        "previous_amount": previous_amount, "change_amount": current_amount - previous_amount,
        "change_ratio": (current_amount - previous_amount) / previous_amount,
        "current_markets": [
            {"exchange": exchange, "market_total_id": secid,
             "amount": by_exchange[exchange][target_date], "unit": "CNY"}
            for exchange, secid in (("SSE", "1.000001"), ("SZSE", "0.399106"))
        ],
        "previous_markets": [
            {"exchange": exchange, "market_total_id": secid,
             "amount": by_exchange[exchange][previous_date], "unit": "CNY"}
            for exchange, secid in (("SSE", "1.000001"), ("SZSE", "0.399106"))
        ],
        "source": "eastmoney_index_daily_turnover", "source_urls": sources,
        "finality": finality,
    }
    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = turnover_summary(data)
    data["source_evidence"] = [{
        "url": url, "fact_as_of": fact_as_of,
        "data": {"summary": summary, "exchange": exchange,
                 "current_amount": by_exchange[exchange][target_date],
                 "previous_amount": by_exchange[exchange][previous_date],
                 "scope": "SSE+SZSE", "unit": "CNY", "finality": finality},
    } for exchange, url in zip(("SSE", "SZSE"), sources)]
    return data, fact_as_of


def markethub_turnover_payload(
    endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    separator = "&" if "?" in endpoint else "?"
    source_url, body = fetch(endpoint + separator + "trade_date=" + quote_plus(required.date().isoformat()))
    try:
        payload = json.loads(body)
        data = {
            "trading_date": str(payload["trading_date"]),
            "previous_trading_date": str(payload["previous_trading_date"]),
            "scope": str(payload["scope"]), "unit": str(payload["unit"]),
            "current_amount": float(payload["current_amount"]),
            "previous_amount": float(payload["previous_amount"]),
            "change_amount": float(payload["change_amount"]),
            "change_ratio": float(payload["change_ratio"]),
            "current_markets": list(payload["current_markets"]),
            "previous_markets": list(payload["previous_markets"]),
            "scope_definition": str(payload["scope_definition"]),
            "scope_note": str(payload["scope_note"]),
            "source": str(payload["source"]), "source_urls": [source_url],
            "finality": finality,
        }
        observed = dt.datetime.fromisoformat(str(payload["fact_as_of"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail(75, "MarketHub turnover response is invalid")
    if payload.get("contract") != "markethub-cn-market-turnover-compare-v1" or observed.tzinfo is None:
        fail(75, "MarketHub turnover response is invalid")
    fact_as_of = observed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    data["source_evidence"] = [{
        "url": source_url, "fact_as_of": fact_as_of,
        "data": {"summary": turnover_summary(data), "scope": data["scope"],
                 "unit": data["unit"], "finality": finality},
    }]
    return data, fact_as_of


def turnover_page_url(endpoint: str, page: int) -> str:
    separator = "&" if "?" in endpoint else "?"
    return (
        endpoint + separator + "pn=" + str(page)
        + "&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
        + "&fltt=2&invt=2&fid=f3"
        + "&fs=m%3A0%2Bt%3A6%2Cm%3A0%2Bt%3A80%2Cm%3A1%2Bt%3A2%2Cm%3A1%2Bt%3A23"
        + "&fields=f12%2Cf6%2Cf124"
    )


def tencent_current_turnover(
    endpoint: str, required: dt.datetime, finality: str,
) -> tuple[float, list[dict[str, object]], list[str], list[dt.datetime]]:
    separator = "" if endpoint.endswith(("=", ",")) else "&q="
    source_url, body = fetch(endpoint + separator + "sh000001,sz399106")
    records = {
        match.group(1): match.group(2).split("~")
        for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', body)
    }
    markets: list[dict[str, object]] = []
    moments: list[dt.datetime] = []
    for vendor_symbol, symbol, exchange in (
        ("sh000001", "000001", "SSE"), ("sz399106", "399106", "SZSE"),
    ):
        fields = records.get(vendor_symbol)
        if not fields or len(fields) <= 35 or fields[2] != symbol:
            fail(75, "Tencent turnover response has missing or mismatched market identity")
        try:
            moment = dt.datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=required.tzinfo)
            components = fields[35].split("/")
            amount = Decimal(components[2])
        except (IndexError, InvalidOperation, ValueError):
            fail(75, "Tencent turnover response has an invalid exact amount field")
        if moment.date() != required.date():
            fail(75, "Tencent turnover trading date is invalid")
        if finality in {"close", "official_close"} and moment.time() < dt.time(15, 0):
            fail(75, "Tencent turnover response does not meet close finality")
        if amount <= 0 or amount != amount.to_integral_value():
            fail(75, "Tencent turnover amount is invalid")
        moments.append(moment)
        markets.append({
            "exchange": exchange, "market_total_id": vendor_symbol,
            "amount": int(amount), "unit": "CNY",
        })
    return float(sum(int(row["amount"]) for row in markets)), markets, [source_url], moments


def snapshot_turnover_payload(
    current_endpoint: str, snapshot_endpoint: str, required_at: str, finality: str,
    *, current_provider: str = "eastmoney",
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    seen: set[str] = set()
    if current_provider == "tencent":
        current_amount, current_markets, page_urls, moments = tencent_current_turnover(
            current_endpoint, required, finality,
        )
        current_coverage = {"current_market_count": len(current_markets)}
        current_source = "tencent_index_spot+markethub_daily_snapshot"
    else:
        first_url, first_body = fetch(turnover_page_url(current_endpoint, 1))
        try:
            first_data = json.loads(first_body)["data"]
            total = int(first_data["total"])
            current_rows = list(first_data["diff"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            fail(75, "current turnover snapshot is invalid")
        pages = min(64, (total + 99) // 100)
        page_urls = [first_url]
        if pages > 1:
            def read_page(page: int) -> tuple[str, list[object]]:
                url, body = fetch(turnover_page_url(current_endpoint, page))
                try:
                    values = json.loads(body)["data"]["diff"]
                except (KeyError, TypeError, json.JSONDecodeError):
                    fail(75, "current turnover page is invalid")
                return url, values if isinstance(values, list) else []
            with ThreadPoolExecutor(max_workers=min(8, pages - 1)) as pool:
                for url, rows in pool.map(read_page, range(2, pages + 1)):
                    page_urls.append(url)
                    current_rows.extend(row for row in rows if isinstance(row, dict))
        if total <= 0 or len(current_rows) < total:
            fail(75, "current turnover snapshot is incomplete")
        current_amount = 0.0
        moments = []
        market_amounts = {"SSE": 0.0, "SZSE": 0.0}
        for row in current_rows[:total]:
            try:
                code = str(row["f12"])
                amount = float(row["f6"])
                moment = dt.datetime.fromtimestamp(int(row["f124"]), tz=dt.timezone.utc)
            except (KeyError, TypeError, ValueError, OSError):
                # Suspended rows can be unpriced; they contribute zero turnover.
                continue
            if not re.fullmatch(r"\d{6}", code) or code in seen or amount < 0:
                fail(75, "current turnover row is invalid")
            exchange = "SSE" if code.startswith("6") else "SZSE"
            seen.add(code)
            current_amount += amount
            market_amounts[exchange] += amount
            moments.append(moment)
        if len(seen) < 1000 or not moments or any(amount <= 0 for amount in market_amounts.values()):
            fail(75, "current turnover scope is incomplete")
        if any(moment.astimezone(required.tzinfo).date() != required.date() for moment in moments):
            fail(75, "current turnover trading date is invalid")
        current_markets = [
            {"exchange": "SSE", "market_total_id": "eastmoney_sse_equities", "amount": market_amounts["SSE"], "unit": "CNY"},
            {"exchange": "SZSE", "market_total_id": "eastmoney_szse_equities", "amount": market_amounts["SZSE"], "unit": "CNY"},
        ]
        current_coverage = {"current_security_count": len(seen)}
        current_source = "eastmoney_spot+markethub_daily_snapshot"

    previous_rows: list[dict[str, object]] = []
    previous_date = ""
    snapshot_url = ""
    checked_urls: list[str] = []
    for days in range(1, 11):
        candidate = required.date() - dt.timedelta(days=days)
        separator = "&" if "?" in snapshot_endpoint else "?"
        requested_url = (
            snapshot_endpoint + separator + "trade_date=" + candidate.isoformat()
            + "&limit=10000&skip_suspended=false"
        )
        resolved_url, body = fetch(requested_url)
        checked_urls.append(resolved_url)
        try:
            rows = json.loads(body)
        except json.JSONDecodeError:
            fail(75, "previous turnover snapshot is invalid")
        if not isinstance(rows, list):
            fail(75, "previous turnover snapshot is invalid")
        selected = [row for row in rows if isinstance(row, dict)]
        if selected:
            previous_rows = selected
            previous_date = candidate.isoformat()
            snapshot_url = resolved_url
            break
    if len(previous_rows) < 1000 or not previous_date:
        fail(75, "previous turnover snapshot is incomplete")
    previous_amount = 0.0
    previous_seen: set[str] = set()
    previous_market_amounts = {"SSE": 0.0, "SZSE": 0.0}
    for row in previous_rows:
        code = str(row.get("code") or "")
        # MarketHub's stock snapshot contains CN equities from all exchanges;
        # the SSE+SZSE scope excludes BSE codes (4/8/9 prefixes).
        if not re.fullmatch(r"[036]\d{5}", code):
            continue
        if code in previous_seen or str(row.get("trade_time") or "")[:10] != previous_date:
            fail(75, "previous turnover row is invalid")
        try:
            amount = float(row.get("amount"))
        except (TypeError, ValueError):
            fail(75, "previous turnover row is invalid")
        if amount < 0:
            fail(75, "previous turnover row is invalid")
        previous_seen.add(code)
        previous_amount += amount
        previous_market_amounts["SSE" if code.startswith("6") else "SZSE"] += amount
    if (
        len(previous_seen) < 1000 or previous_amount <= 0
        or any(amount <= 0 for amount in previous_market_amounts.values())
    ):
        fail(75, "previous turnover scope is incomplete")
    previous_markets = [
        {"exchange": "SSE", "market_total_id": "markethub_sse_equities",
         "amount": previous_market_amounts["SSE"], "unit": "CNY"},
        {"exchange": "SZSE", "market_total_id": "markethub_szse_equities",
         "amount": previous_market_amounts["SZSE"], "unit": "CNY"},
    ]
    data: dict[str, object] = {
        "trading_date": required.date().isoformat(), "previous_trading_date": previous_date,
        "scope": "SSE+SZSE", "scope_definition": "cross_provider_exchange_stock_totals",
        "scope_note": "口径为两市股票成交额的公开全市场快照聚合、单位CNY",
        "unit": "CNY", "current_amount": current_amount,
        "previous_amount": previous_amount, "change_amount": current_amount - previous_amount,
        "change_ratio": (current_amount - previous_amount) / previous_amount,
        "current_markets": current_markets, "previous_markets": previous_markets,
        "source": current_source,
        "source_urls": [*page_urls, *checked_urls], "finality": finality,
        "coverage": {**current_coverage, "previous_security_count": len(previous_seen)},
    }
    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = turnover_summary(data)
    data["source_evidence"] = [
        {"url": page_urls[0], "fact_as_of": fact_as_of,
         "data": {"summary": summary, **current_coverage, "finality": finality}},
        {"url": snapshot_url, "fact_as_of": fact_as_of,
         "data": {"summary": summary, "previous_security_count": len(previous_seen), "finality": finality}},
    ]
    return data, fact_as_of


def eastmoney_sector_snapshot_payload(
    board_endpoint: str, constituent_endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    source_urls: list[str] = []
    selected: list[tuple[str, dict[str, object]]] = []
    distributions: dict[str, dict[str, dict[str, object]]] = {"industry": {}, "theme": {}}
    moments: list[dt.datetime] = []
    for group, fs in (("industry", "m:90+t:2"), ("theme", "m:90+t:3")):
        ordered: list[dict[str, object]] = []
        expected_total: int | None = None
        page = 1
        while expected_total is None or len(distributions[group]) < expected_total:
            separator = "&" if "?" in board_endpoint else "?"
            url, body = fetch(
                board_endpoint + separator + "pn=" + str(page) + "&pz=100&np=1&fltt=2&invt=2&fid=f3&po=1"
                + "&fs=" + quote_plus(fs) + "&fields=f12%2Cf14%2Cf3%2Cf124"
            )
            try:
                response_data = json.loads(body)["data"]
                rows = response_data["diff"]
                total = int(response_data.get("total") or len(rows))
                boards = [{
                    "board_id": str(row["f12"]), "name": clean_text(row["f14"]).strip(),
                    "kind": group, "change_percent": float(row["f3"]),
                } for row in rows]
                row_moments = [dt.datetime.fromtimestamp(int(row["f124"]), tz=dt.timezone.utc) for row in rows]
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, OSError):
                fail(75, "sector board response is invalid")
            if not boards or any(not board["board_id"] or not board["name"] for board in boards):
                fail(75, "sector board identity is invalid")
            source_urls.append(url)
            moments.extend(row_moments)
            for board in boards:
                distributions[group][str(board["board_id"])] = board
            ordered.extend(boards)
            expected_total = total
            page += 1
            if page > 20 or not rows:
                break
        if expected_total is None or len(distributions[group]) != expected_total:
            fail(75, "sector board distribution is incomplete")
        selected.extend((("leaders", ordered[0]), ("laggards", ordered[-1])))
    leaders: list[dict[str, object]] = []
    laggards: list[dict[str, object]] = []
    for direction, board in selected:
        separator = "&" if "?" in constituent_endpoint else "?"
        url, body = fetch(
            constituent_endpoint + separator + "pn=1&pz=1&np=1&fltt=2&invt=2&fid=f6&po=1"
            + "&fs=" + quote_plus("b:" + str(board["board_id"]))
            + "&fields=f12%2Cf14%2Cf3%2Cf6%2Cf124"
        )
        try:
            row = json.loads(body)["data"]["diff"][0]
            core = {
                "symbol": str(row["f12"]), "name": clean_text(row["f14"]).strip(),
                "amount": float(row["f6"]), "change_percent": float(row["f3"]),
            }
            moment = dt.datetime.fromtimestamp(int(row["f124"]), tz=dt.timezone.utc)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, OSError):
            fail(75, "sector constituent response is invalid")
        board["core"] = core
        source_urls.append(url)
        moments.append(moment)
        (leaders if direction == "leaders" else laggards).append(board)
    local_moments = [value.astimezone(required.tzinfo) for value in moments]
    if any(value.date() != required.date() for value in local_moments):
        fail(75, "sector snapshot trading date is invalid")
    if finality in {"close", "official_close"} and any(value.time() < dt.time(15, 0) for value in local_moments):
        fail(75, "sector snapshot does not meet close finality")
    distribution = {}
    for group, board_map in distributions.items():
        changes = sorted(float(row["change_percent"]) for row in board_map.values())
        midpoint = len(changes) // 2
        median = changes[midpoint] if len(changes) % 2 else (changes[midpoint - 1] + changes[midpoint]) / 2
        distribution[group] = {
            "total": len(changes), "up": sum(value > 0 for value in changes),
            "down": sum(value < 0 for value in changes), "flat": sum(value == 0 for value in changes),
            "median_change_percent": round(median, 4),
        }
    data: dict[str, object] = {
        "trading_date": required.date().isoformat(), "leaders": leaders, "laggards": laggards,
        "distribution": distribution,
        "source": "eastmoney_board_and_constituent_snapshot", "source_urls": source_urls,
        "finality": finality,
    }
    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0, 1), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = "；".join(
        [f"{row['name']}板块领涨，容量核心{row['core']['name']}({row['core']['symbol']})成交额{float(row['core']['amount']) / 100_000_000:.2f}亿元"
         for row in leaders]
        + [f"{row['name']}板块领跌，容量核心{row['core']['name']}({row['core']['symbol']})成交额{float(row['core']['amount']) / 100_000_000:.2f}亿元"
           for row in laggards]
    )
    data["source_evidence"] = [{
        "url": url, "fact_as_of": fact_as_of,
        "data": {"summary": f"{data['trading_date']} {summary}", "leaders": leaders,
                 "laggards": laggards, "distribution": distribution, "finality": finality},
    } for url in source_urls]
    return data, fact_as_of


def eastmoney_fund_flow_payload(
    endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    expected_date = required.date().isoformat()
    markets: list[dict[str, object]] = []
    source_urls: list[str] = []
    fields = ("main_net_inflow", "small_net_inflow", "medium_net_inflow", "large_net_inflow", "super_large_net_inflow")
    for exchange, secid in (("SSE", "1.000001"), ("SZSE", "0.399001")):
        separator = "&" if "?" in endpoint else "?"
        url, body = fetch(
            endpoint + separator + "lmt=120&klt=101&fields1=f1%2Cf2%2Cf3%2Cf7"
            + "&fields2=f51%2Cf52%2Cf53%2Cf54%2Cf55%2Cf56%2Cf57"
            + "&ut=b2884a393a59ad64002292a3e90d46a5"
            + "&secid=" + quote_plus(secid),
            referer="https://data.eastmoney.com/zjlx/dpzjlx.html",
            curl_fallback=True,
        )
        try:
            rows = json.loads(body)["data"]["klines"]
            values = next(str(row).split(",") for row in rows if str(row).split(",", 1)[0] == expected_date)
            numbers = [float(value) for value in values[1:6]]
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            fail(75, "fund flow response does not contain the requested trading date")
        if len(numbers) != 5:
            fail(75, "fund flow response is invalid")
        markets.append({"exchange": exchange, "index_secid": secid, **dict(zip(fields, numbers))})
        source_urls.append(url)
    combined = {field: sum(float(row[field]) for row in markets) for field in fields}
    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = (
        f"{expected_date} 沪深两市主力资金净流入{combined['main_net_inflow'] / 100_000_000:.2f}亿元，"
        f"超大单{combined['super_large_net_inflow'] / 100_000_000:.2f}亿元，"
        f"大单{combined['large_net_inflow'] / 100_000_000:.2f}亿元"
    )
    data: dict[str, object] = {
        "trading_date": expected_date, "scope": "SSE+SZSE", "unit": "CNY",
        "markets": markets, "combined": combined, "finality": finality,
        "source": "eastmoney_index_fund_flow", "source_urls": source_urls,
    }
    data["source_evidence"] = [{
        "url": url, "fact_as_of": fact_as_of,
        "data": {"summary": summary, "market": market, "combined": combined, "finality": finality},
    } for url, market in zip(source_urls, markets)]
    return data, fact_as_of


def article_fund_flow_payload(
    base: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    expected_date = required.date().isoformat()
    inflow_by_name: dict[str, dict[str, object]] = {}
    outflow_by_name: dict[str, dict[str, object]] = {}
    selected_articles: list[dict[str, object]] = []
    for source_key in ("ths_important_news", "cls_depth_article"):
        _url, body = fetch(
            base.rstrip("/") + "/api/articles/range?source=" + quote_plus(source_key)
            + "&start_date=" + quote_plus(expected_date)
            + "&end_date=" + quote_plus(expected_date)
        )
        try:
            payload = json.loads(body)
            groups = payload["groups"]
            group = next(
                row for row in groups
                if isinstance(row, dict) and str(row.get("source_key") or "") == source_key
            )
        except (KeyError, StopIteration, TypeError, json.JSONDecodeError):
            fail(75, "fund flow article response is invalid")
        rows = group.get("articles")
        if not isinstance(rows, list):
            fail(75, "fund flow article response is invalid")
        for row in rows:
            if not isinstance(row, dict):
                continue
            published_text = str(row.get("published_at") or "").strip()
            try:
                published = dt.datetime.fromisoformat(published_text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=required.tzinfo)
            published = published.astimezone(required.tzinfo)
            if published.date() != required.date() or published.time() < dt.time(15, 0):
                continue
            article_url = str(row.get("source_url") or "").strip()
            parsed = urlparse(article_url)
            hostname = str(parsed.hostname or "").lower()
            if (
                parsed.scheme not in {"http", "https"}
                or not (hostname.endswith("10jqka.com.cn") or hostname.endswith("cls.cn"))
            ):
                continue
            content = clean_text(row.get("content") or row.get("subtitle"))
            article_inflows: list[dict[str, object]] = []
            article_outflows: list[dict[str, object]] = []
            for match in re.finditer(
                r"【([^】\r\n]{1,40})】获主力资金净流入([+-]?\d+(?:\.\d+)?)亿", content,
            ):
                name = match.group(1).strip()
                value = float(match.group(2)) * 100_000_000
                item = {"name": name, "net_inflow": value, "unit": "CNY"}
                inflow_by_name[name] = item
                article_inflows.append(item)
            for match in re.finditer(
                r"([\u4e00-\u9fffA-Za-z0-9Ⅱ]+)板块主力资金净流出居首", content,
            ):
                name = match.group(1).strip()
                item = {"name": name}
                outflow_by_name[name] = item
                article_outflows.append(item)
            if article_inflows or article_outflows:
                selected_articles.append({
                    "source": source_key,
                    "source_url": article_url,
                    "published_at": published.isoformat(),
                    "title": clean_text(row.get("title")).strip()[:300],
                    "sector_inflow_leaders": article_inflows,
                    "sector_outflow_leaders": article_outflows,
                })
    inflows = sorted(inflow_by_name.values(), key=lambda item: float(item["net_inflow"]), reverse=True)
    outflows = list(outflow_by_name.values())
    source_urls = list(dict.fromkeys(str(row["source_url"]) for row in selected_articles))
    if len(inflows) < 3 or not outflows or len(source_urls) < 2:
        fail(75, "fund flow articles lack quantified directional coverage")

    fact_as_of = dt.datetime.combine(
        required.date(), dt.time(15, 0), required.tzinfo,
    ).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    data: dict[str, object] = {
        "trading_date": expected_date,
        "scope": "SSE+SZSE",
        "unit": "CNY",
        "coverage_level": "directional_sector",
        "sector_inflow_leaders": inflows[:10],
        "sector_outflow_leaders": outflows[:10],
        "limitations": ["full_market_net_flow_unavailable", "order_size_breakdown_unavailable"],
        "finality": finality,
        "source": "verified_close_article_fund_flow",
        "source_urls": source_urls,
    }
    data["source_evidence"] = [{
        "url": row["source_url"],
        "fact_as_of": fact_as_of,
        "data": {
            "summary": (
                f"{expected_date} close article checked for quantified sector inflows "
                "and the leading sector outflow; full-market and order-size totals remain unavailable"
            ),
            "coverage_level": "directional_sector",
            "title": row["title"],
            "published_at": row["published_at"],
            "sector_inflow_leaders": row["sector_inflow_leaders"],
            "sector_outflow_leaders": row["sector_outflow_leaders"],
        },
    } for row in selected_articles]
    return data, fact_as_of


def announcement_snapshot_payload(
    base: str, symbols: list[dict[str, str]], start_date: str, end_date: str, required_at: str,
) -> tuple[dict[str, object], str]:
    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
        observed = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00"))
    except ValueError:
        fail(64, "announcement window must use ISO dates and timestamp")
    if observed.tzinfo is None or start > end:
        fail(64, "announcement window is invalid")
    announcements: list[dict[str, object]] = []
    source_urls: list[str] = []
    source_evidence: list[dict[str, object]] = []
    for item in symbols:
        url, body = fetch(base.rstrip("/") + "/api/cninfo/search?q=" + quote_plus(item["symbol"]))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            fail(75, "announcement service response is not JSON")
        rows = payload.get("公告") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            rows = payload.get("announcements") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            fail(75, "announcement service response is invalid")
        normalized: list[dict[str, object]] = []
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("代码") or row.get("symbol") or "").strip()
            date_text = str(row.get("公告日期") or row.get("announcement_date") or row.get("date") or "")[:10]
            try:
                announcement_date = dt.date.fromisoformat(date_text)
            except ValueError:
                continue
            if symbol != item["symbol"] or not (start <= announcement_date <= end):
                continue
            title = clean_text(row.get("公告标题") or row.get("title")).strip()[:300]
            source_url = clean_text(
                row.get("公告链接") or row.get("source_url") or row.get("url") or url
            ).strip()
            identity = (symbol, title, announcement_date.isoformat())
            if not title or identity in seen:
                continue
            seen.add(identity)
            content = clean_text(row.get("公告内容") or row.get("content")).strip()[:1200]
            published_at = clean_text(row.get("发布时间") or row.get("published_at")).strip()
            if not published_at:
                published_at = announcement_date.isoformat() + "T00:00:00+08:00"
            normalized.append({
                "symbol": symbol,
                "issuer": clean_text(row.get("简称") or row.get("issuer") or row.get("name")).strip(),
                "title": title, "content": content,
                "published_at": published_at,
                "published_time_precision": "timestamp" if "T" in clean_text(row.get("发布时间") or row.get("published_at")) else "date",
                "announcement_date": announcement_date.isoformat(),
                "source_url": source_url,
                "content_verified": bool(content),
                "impact_status": "pending_assessment",
            })
        announcements.extend(normalized)
        source_urls.append(url)
        pagination = payload.get("pagination") if isinstance(payload, dict) and isinstance(payload.get("pagination"), dict) else {}
        pagination_complete = not bool(
            (isinstance(payload, dict) and payload.get("has_more") is True)
            or pagination.get("has_more") is True
            or (
                pagination.get("total_pages") is not None
                and int(pagination.get("page") or 0) < int(pagination.get("total_pages") or 0)
            )
        )
        enumeration_proof = {
            "authority": "cninfo", "query_symbol": item["symbol"],
            "start_date": start_date, "end_date": end_date,
            "pagination_complete": pagination_complete,
        }
        source_evidence.append({
            "url": url, "fact_as_of": observed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "data": {"summary": f"已核查{item['symbol']}在{start_date}至{end_date}的公告，共{len(normalized)}条",
                     "checked_symbol": item["symbol"], "announcements": normalized,
                     "enumeration_proof": enumeration_proof},
        })
    fact_as_of = observed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "checked_symbols": [item["symbol"] for item in symbols], "start_date": start_date,
        "end_date": end_date, "announcements": announcements, "source": "cninfo_disclosure_search",
        "source_urls": source_urls, "source_evidence": source_evidence,
        "enumeration_proofs": [row["data"]["enumeration_proof"] for row in source_evidence],
    }, fact_as_of


def market_event_snapshot_payload(
    base: str, start_at: str, end_at: str,
) -> tuple[dict[str, object], str]:
    try:
        start = dt.datetime.fromisoformat(start_at.replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(end_at.replace("Z", "+00:00"))
    except ValueError:
        fail(64, "market event window must use ISO timestamps")
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        fail(64, "market event window is invalid")
    start = start.astimezone(dt.timezone.utc)
    end = end.astimezone(dt.timezone.utc)
    local_timezone = dt.timezone(dt.timedelta(hours=8))
    sources = (
        "eastmoney_daily_topic_report", "cls_depth_article", "ths_important_news",
    )
    normalized: list[dict[str, object]] = []
    source_urls: list[str] = []
    source_checks: list[dict[str, object]] = []
    for source in sources:
        url, body = fetch(
            base.rstrip("/") + "/api/articles/range?source=" + quote_plus(source)
            + "&start_date=" + quote_plus(start.astimezone(local_timezone).date().isoformat())
            + "&end_date=" + quote_plus(end.astimezone(local_timezone).date().isoformat())
        )
        try:
            payload = json.loads(body)
            groups = payload["groups"]
            group = next(row for row in groups if isinstance(row, dict) and row.get("source_key") == source)
            rows = group["articles"]
            reported_count = int(group["count"])
        except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            fail(75, "market event service response is invalid")
        if not isinstance(rows, list) or reported_count < len(rows):
            fail(75, "market event service response is invalid")
        matched: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            published_text = str(row.get("published_at") or "").strip()
            try:
                published = dt.datetime.fromisoformat(published_text.replace("Z", "+00:00"))
            except ValueError:
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=local_timezone)
            published = published.astimezone(dt.timezone.utc)
            if not start < published <= end:
                continue
            article_url = str(row.get("source_url") or row.get("detail_url") or "").strip()
            parsed_article_url = urlparse(article_url)
            if (
                parsed_article_url.scheme not in {"http", "https"}
                or not parsed_article_url.netloc
                or parsed_article_url.username
                or parsed_article_url.password
            ):
                continue
            title = clean_text(row.get("title")).strip()[:300]
            if not title:
                continue
            matched.append({
                "source": source, "article_id": clean_text(row.get("article_id")).strip()[:200],
                "published_at": published.isoformat().replace("+00:00", "Z"),
                "title": title,
                "content": clean_text(row.get("content") or row.get("subtitle")).strip()[:600],
                "source_url": article_url,
            })
        relevance_terms = (
            "A股", "沪指", "深证", "创业板", "市场", "收盘", "资金", "政策",
            "证监会", "央行", "风险", "行业", "板块", "交易所",
        )
        matched.sort(
            key=lambda item: sum(
                term in (str(item["title"]) + str(item["content"]))
                for term in relevance_terms
            ),
            reverse=True,
        )
        selected = matched[:5]
        normalized.extend(selected)
        source_urls.append(url)
        source_checks.append({
            "source": source, "source_url": url, "reported_count": reported_count,
            "matched_count": len(matched), "selected_count": len(selected),
        })
    fact_as_of = end.isoformat().replace("+00:00", "Z")
    source_evidence: list[dict[str, object]] = [{
        "url": row["source_url"], "fact_as_of": fact_as_of,
        "data": {
            "summary": (
                f"checked {row['source']} for announcements, policy and risk events "
                f"from {start_at} through {end_at}; matched {row['matched_count']} records"
            ),
            "checked_terms": ["公告", "政策", "风险"], "window_start": start_at,
            "window_end": end_at, **row,
        },
    } for row in source_checks]
    source_evidence.extend({
        "url": row["source_url"], "fact_as_of": row["published_at"],
        "data": {key: value for key, value in row.items() if key != "source_url"},
    } for row in normalized)
    return {
        "checked_sources": list(sources), "start_at": start_at, "end_at": end_at,
        "matched_count": sum(int(row["matched_count"]) for row in source_checks),
        "articles": normalized, "source_checks": source_checks,
        "source": "yosef_bounded_market_event_snapshot",
        "source_urls": [*source_urls, *(str(row["source_url"]) for row in normalized)],
        "source_evidence": source_evidence,
    }, fact_as_of


def markethub_sector_snapshot_payload(
    endpoint: str, required_at: str, finality: str,
) -> tuple[dict[str, object], str]:
    try:
        required = dt.datetime.fromisoformat(required_at.replace("Z", "+00:00")).astimezone(
            dt.timezone(dt.timedelta(hours=8)))
    except ValueError:
        fail(64, "required_at must be an ISO timestamp")
    separator = "&" if "?" in endpoint else "?"
    source_url, body = fetch(endpoint + separator + "trade_date=" + quote_plus(required.date().isoformat()))
    try:
        payload = json.loads(body)
        leaders = list(payload["leaders"])
        laggards = list(payload["laggards"])
        observed = dt.datetime.fromisoformat(str(payload["fact_as_of"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        fail(75, "MarketHub sector response is invalid")
    if payload.get("contract") != "markethub-cn-market-sector-snapshot-v1" or observed.tzinfo is None:
        fail(75, "MarketHub sector response is invalid")
    data: dict[str, object] = {
        "trading_date": str(payload["trading_date"]), "leaders": leaders, "laggards": laggards,
        "source": str(payload["source"]), "source_urls": [source_url], "finality": finality,
    }
    if isinstance(payload.get("distribution"), dict):
        data["distribution"] = payload["distribution"]
    fact_as_of = observed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    summary = "；".join(
        [f"{row['name']}板块领涨，容量核心{row['core']['name']}({row['core']['symbol']})成交额{float(row['core']['amount']) / 100_000_000:.2f}亿元"
         for row in leaders]
        + [f"{row['name']}板块领跌，容量核心{row['core']['name']}({row['core']['symbol']})成交额{float(row['core']['amount']) / 100_000_000:.2f}亿元"
           for row in laggards]
    )
    data["source_evidence"] = [{
        "url": source_url, "fact_as_of": fact_as_of,
        "data": {"summary": f"{data['trading_date']} {summary}", "leaders": leaders,
                 "laggards": laggards, "distribution": data.get("distribution"), "finality": finality},
    }]
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
        spot_finality = "intraday" if finality in {"close", "official_close"} else finality
        payload, fact_as_of = quote_payload(body, normalized, spot_finality)
        if finality in {"intraday", "close", "official_close"}:
            payload, fact_as_of = frozen_minute_payload(
                payload, normalized, str(request.get("required_at") or ""), "equity",
                inputs.get("tencent_minute_url"), finality=finality,
            )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_equity_current_bar":
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols or len({str(value).strip() for value in symbols}) != len(symbols):
            fail(64, "symbols must be a non-empty, deduplicated array")
        freq = str(inputs.get("freq") or "")
        if freq not in {"1m", "30m"}:
            fail(64, "current Bar frequency must be 1m or 30m")
        finality = str(request.get("finality") or "observed")
        if finality not in {"observed", "realtime", "intraday", "close", "official_close"}:
            fail(64, "unsupported current Bar finality")
        normalized = [identity(symbol) for symbol in symbols]
        base_url = safe_url(inputs.get("markethub_url") or "http://yosef-server:8803/api/stocks/quotes")
        separator = "&" if "?" in base_url else "?"
        url = base_url + separator + "codes=" + quote_plus(",".join(item["symbol"] for item in normalized)) + "&freq=" + freq + "&datetime=now&count=1&adjust=none"
        resolved_url, body = fetch(url)
        payload, fact_as_of = current_bar_payload(body, normalized, freq, finality, resolved_url)
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_equity_current_bar_tencent":
        symbols = inputs.get("symbols")
        if not isinstance(symbols, list) or not symbols or len({str(value).strip() for value in symbols}) != len(symbols):
            fail(64, "symbols must be a non-empty, deduplicated array")
        freq = str(inputs.get("freq") or "")
        finality = str(request.get("finality") or "observed")
        if finality not in {"observed", "realtime", "intraday", "close", "official_close"}:
            fail(64, "unsupported current Bar finality")
        payload, fact_as_of = tencent_current_bar_payload(
            [identity(symbol) for symbol in symbols], str(request.get("required_at") or ""), freq,
            finality, inputs.get("tencent_minute_url"),
        )
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
        spot_finality = "intraday" if finality in {"close", "official_close"} else finality
        payload, fact_as_of = index_payload(body, normalized, spot_finality)
        if finality in {"intraday", "close", "official_close"}:
            payload, fact_as_of = frozen_minute_payload(
                payload, normalized, str(request.get("required_at") or ""), "index",
                inputs.get("tencent_minute_url"), finality=finality,
            )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode in {"cn_market_breadth", "cn_market_breadth_markethub", "cn_market_breadth_eastmoney"}:
        finality = str(request.get("finality") or "observed")
        if finality not in {"intraday", "realtime", "close", "official_close"}:
            fail(64, "unsupported market finality")
        markethub_url = inputs.get("markethub_url")
        if mode == "cn_market_breadth_markethub" or (
            mode == "cn_market_breadth"
            and (markethub_url or (finality in {"close", "official_close"} and not inputs.get("breadth_url")))
        ):
            payload, fact_as_of = markethub_breadth_payload(
                safe_url(markethub_url or "http://yosef-server:8803/api/stocks/market-breadth"),
                str(request.get("required_at") or ""), finality,
            )
        else:
            payload, fact_as_of = market_breadth_payload(
                safe_url(inputs.get("breadth_url") or "https://push2delay.eastmoney.com/api/qt/clist/get"), finality,
            )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode in {
        "cn_market_turnover_compare", "cn_market_turnover_compare_eastmoney",
        "cn_market_turnover_compare_eastmoney_history",
        "cn_market_turnover_compare_official_exchanges", "cn_market_turnover_compare_markethub",
        "cn_market_turnover_compare_eastmoney_spot_markethub",
        "cn_market_turnover_compare_tencent", "cn_market_turnover_compare_tencent_spot_markethub",
    }:
        finality = str(request.get("finality") or "observed")
        if finality not in {"close", "official_close"}:
            fail(64, "turnover comparison requires close finality")
        required_at = str(request.get("required_at") or "")
        if mode == "cn_market_turnover_compare_official_exchanges":
            payload, fact_as_of = official_exchange_turnover_payload(
                safe_url(inputs.get("sse_turnover_url") or (
                    "https://query.sse.com.cn/commonQuery.do?jsonCallBack=jsonpCallback"
                    "&sqlId=COMMON_SSE_SJ_GPSJ_CJGK_MRGK_C"
                    "&PRODUCT_CODE=01%2C02%2C03%2C11%2C17&type=inParams"
                )),
                safe_url(inputs.get("szse_turnover_url") or (
                    "https://www.szse.cn/api/report/ShowReport/data?SHOWTYPE=JSON"
                    "&CATALOGID=scsj_gprdgk_after"
                )),
                required_at, finality,
            )
        elif mode in {
            "cn_market_turnover_compare_markethub",
            "cn_market_turnover_compare_eastmoney_spot_markethub",
        }:
            if inputs.get("markethub_turnover_url"):
                payload, fact_as_of = markethub_turnover_payload(
                    safe_url(inputs.get("markethub_turnover_url")), required_at, finality,
                )
            else:
                payload, fact_as_of = snapshot_turnover_payload(
                    safe_url(inputs.get("eastmoney_spot_url") or "https://push2delay.eastmoney.com/api/qt/clist/get"),
                    safe_url(inputs.get("markethub_snapshot_url") or "http://yosef-server:8803/api/stocks/quotes/daily-snapshot"),
                    required_at, finality,
                )
        elif mode in {
            "cn_market_turnover_compare", "cn_market_turnover_compare_eastmoney",
            "cn_market_turnover_compare_eastmoney_history",
        }:
            payload, fact_as_of = eastmoney_turnover_payload(
                safe_url(inputs.get("eastmoney_kline_url") or "http://push2his.eastmoney.com/api/qt/stock/kline/get"),
                required_at, finality,
            )
        else:
            payload, fact_as_of = snapshot_turnover_payload(
                safe_url(inputs.get("tencent_turnover_url") or "https://qt.gtimg.cn/q="),
                safe_url(inputs.get("markethub_snapshot_url") or "http://yosef-server:8803/api/stocks/quotes/daily-snapshot"),
                required_at, finality, current_provider="tencent",
            )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode in {
        "cn_market_sector_snapshot", "cn_market_sector_snapshot_eastmoney",
        "cn_market_sector_snapshot_markethub",
    }:
        finality = str(request.get("finality") or "observed")
        if finality not in {"close", "official_close"}:
            fail(64, "sector snapshot requires close finality")
        required_at = str(request.get("required_at") or "")
        if mode == "cn_market_sector_snapshot_markethub":
            payload, fact_as_of = markethub_sector_snapshot_payload(
                safe_url(inputs.get("markethub_sector_url") or "http://yosef-server:8803/api/stocks/market-sectors"),
                required_at, finality,
            )
        else:
            payload, fact_as_of = eastmoney_sector_snapshot_payload(
                safe_url(inputs.get("eastmoney_board_url") or "https://push2delay.eastmoney.com/api/qt/clist/get"),
                safe_url(inputs.get("eastmoney_constituent_url") or "https://push2delay.eastmoney.com/api/qt/clist/get"),
                required_at, finality,
            )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode in {
        "cn_market_fund_flow_snapshot_eastmoney_history",
        "cn_market_fund_flow_snapshot_eastmoney_history_alt",
        "cn_market_fund_flow_snapshot_article_digest",
    }:
        finality = str(request.get("finality") or "observed")
        if finality not in {"close", "official_close"}:
            fail(64, "fund flow snapshot requires close finality")
        if mode.endswith("_article_digest"):
            payload, fact_as_of = article_fund_flow_payload(
                safe_url(inputs.get("article_base_url") or "http://yosef-server:8815"),
                str(request.get("required_at") or ""), finality,
            )
            result(payload, fact_as_of=fact_as_of)
            return
        if mode.endswith("_alt"):
            endpoint = inputs.get("eastmoney_history_alt_url") or "https://33.push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        else:
            endpoint = inputs.get("eastmoney_history_url") or "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        payload, fact_as_of = eastmoney_fund_flow_payload(
            safe_url(endpoint), str(request.get("required_at") or ""), finality,
        )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_equity_announcement_snapshot":
        raw_symbols = inputs.get("symbols")
        if not isinstance(raw_symbols, list) or not raw_symbols:
            fail(64, "symbols must be a non-empty array")
        symbols = [identity(symbol) for symbol in raw_symbols]
        start_date = str(inputs.get("start_date") or "")
        end_date = str(inputs.get("end_date") or "")
        if not start_date or not end_date:
            fail(64, "start_date and end_date are required")
        payload, fact_as_of = announcement_snapshot_payload(
            safe_url(inputs.get("base_url") or "http://yosef-server:8815"), symbols,
            start_date, end_date, str(request.get("required_at") or ""),
        )
        result(payload, fact_as_of=fact_as_of)
        return
    if mode == "cn_market_event_snapshot":
        start_at = str(inputs.get("start_at") or "")
        end_at = str(inputs.get("end_at") or "")
        if not start_at or not end_at:
            fail(64, "start_at and end_at are required")
        payload, fact_as_of = market_event_snapshot_payload(
            safe_url(inputs.get("base_url") or "http://yosef-server:8815"), start_at, end_at,
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
        query = clean_text(inputs.get("query")).strip()
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
