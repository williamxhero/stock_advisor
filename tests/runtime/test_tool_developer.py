from __future__ import annotations

import json
from pathlib import Path

from ai_trading_companion.tool_developer import ToolDeveloperWorker


def test_worker_creates_a_resumable_sandbox_verified_candidate_without_publishing(tmp_path: Path) -> None:
    root = tmp_path / "tool-developer"
    worker = ToolDeveloperWorker(root)
    need = {
        "need_id": "need-1", "capability": "public_company_facts",
        "output_contract": {"fields": ["symbol", "facts"]},
        "source_hints": ["https://public.example.test/api"],
    }

    first = worker.run(need, stop_after="candidate")
    resumed = ToolDeveloperWorker(root).run(need)
    candidate = root / "jobs" / "need-1" / "candidate"

    assert first["stage"] == "candidate"
    assert resumed["stage"] == "sandbox_verified"
    assert (candidate / "manifest.json").exists()
    assert (candidate / "schema.json").exists()
    assert (candidate / "tool.py").exists()
    assert (candidate / "requirements.lock").read_text(encoding="utf-8") == "# stdlib-only candidate\n"
    assert (candidate / "fixtures" / "smoke-request.json").exists()
    assert (candidate / "tests" / "contract-check.json").exists()
    assert json.loads((root / "jobs" / "need-1" / "checkpoint.json").read_text(encoding="utf-8"))["stage"] == "sandbox_verified"
    assert not (root / "tools" / "public_company_facts" / "current.json").exists()


def test_worker_rejects_path_escape_and_does_not_touch_runtime_tool_root(tmp_path: Path) -> None:
    worker = ToolDeveloperWorker(tmp_path / "developer")
    try:
        worker.run({"need_id": "../escape", "capability": "x", "output_contract": {}, "source_hints": []})
    except ValueError as exc:
        assert "invalid_tool_developer" in str(exc)
    else:
        raise AssertionError("path escape was accepted")
