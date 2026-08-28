"""CLI wrapper for the formal Provider + AWG runtime smoke chain."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from ai_trading_companion.provider_awg_smoke import run_smoke, write_smoke_report  # noqa: E402


def _settings_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "AITradingCompanion" / "config" / "settings.local.json" if local else Path("__missing_localappdata__")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Formal-runtime Provider + AWG MCP smoke chain")
    parser.add_argument("--settings", type=Path, default=_settings_path())
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--query", default="A股市场 今日 重要消息")
    parser.add_argument("--probe-timeout", type=float, default=10.0)
    parser.add_argument("--provider-timeout", type=float, default=45.0)
    args = parser.parse_args(argv)
    try:
        settings = json.loads(args.settings.read_text(encoding="utf-8"))
        if not isinstance(settings, dict): raise ValueError("settings root is not an object")
    except Exception:
        report = {"contract": "provider-awg-smoke-report/v2", "status": "failed", "failure_code": "SETTINGS_MISSING_OR_INVALID"}
        write_smoke_report(args.output_dir / "smoke-report.json", report, forbidden_values=[])
        print("SETTINGS_MISSING_OR_INVALID"); return 2
    report = run_smoke(settings, args.output_dir, query=str(args.query), probe_timeout=max(1.0, args.probe_timeout), provider_timeout=max(1.0, args.provider_timeout))
    label = str(report.get("status") or "failed").upper()
    if report.get("failure_code"): label += ":" + str(report["failure_code"])
    print(label)
    return 0 if report.get("status") == "passed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
