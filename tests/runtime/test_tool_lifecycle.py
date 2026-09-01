from __future__ import annotations

import json
from pathlib import Path

from ai_trading_companion.tool_developer import ToolDeveloperWorker
from ai_trading_companion.tool_lifecycle import ToolLifecycleManager
from ai_trading_companion.tooling import FactRequest, ToolCatalog, ToolRunner


def _need() -> dict:
    return {"need_id": "need-1", "capability": "public_company_facts", "output_contract": {"fields": ["facts"]}, "source_hints": []}


def test_publish_runs_candidate_and_canary_before_atomically_selecting_version(tmp_path: Path) -> None:
    developer = tmp_path / "developer"
    ToolDeveloperWorker(developer).run(_need())
    tools = tmp_path / "tools"
    manager = ToolLifecycleManager(tools)
    request = FactRequest(1, "public_company_facts", "2026-09-01T07:00:00Z", 2.0, {})

    published = manager.publish(developer / "jobs" / "need-1" / "candidate", request)
    resolved = ToolRunner(ToolCatalog(tools)).resolve_with_fallback(request)

    assert published["state"] == "published"
    assert resolved.succeeded
    assert json.loads((tools / "public_company_facts" / "current.json").read_text(encoding="utf-8"))["version"] == "0.1.0"
    assert (tools / ".lifecycle" / "audit.ndjson").exists()


def test_failed_canary_keeps_prior_current_and_creates_a_repair_need(tmp_path: Path) -> None:
    developer = tmp_path / "developer"
    ToolDeveloperWorker(developer).run(_need())
    candidate = developer / "jobs" / "need-1" / "candidate"
    tools = tmp_path / "tools"
    repairs: list[dict] = []
    manager = ToolLifecycleManager(tools, need_reporter=repairs.append)
    request = FactRequest(1, "public_company_facts", "2026-09-01T07:00:00Z", 2.0, {})
    manager.publish(candidate, request)
    manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8")); manifest["version"] = "0.2.0"
    (candidate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (candidate / "tool.py").write_text("print('not json')\n", encoding="utf-8")

    failed = manager.publish(candidate, request)

    assert failed["state"] == "rejected"
    assert json.loads((tools / "public_company_facts" / "current.json").read_text(encoding="utf-8"))["version"] == "0.1.0"
    assert repairs[0]["capability"] == "public_company_facts"
    assert repairs[0]["urgency"] == "high"
