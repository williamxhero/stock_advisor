"""Isolated, one-shot candidate-package worker for formal data tools."""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_STAGES = ("discovered", "candidate", "sandbox_verified")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_segment(value: object) -> str:
    text = str(value or "")
    if not text or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-" for character in text):
        raise ValueError("invalid_tool_developer_identifier")
    return text


class ToolDeveloperWorker:
    """Writes only its job/candidate tree and exits after a bounded stage transition."""

    def __init__(self, development_root: Path) -> None:
        self.root = Path(development_root).resolve()

    def run(self, need: dict[str, Any], *, stop_after: str | None = None) -> dict[str, Any]:
        if not isinstance(need, dict):
            raise ValueError("invalid_tool_developer_need")
        need_id, capability = _safe_segment(need.get("need_id")), _safe_segment(need.get("capability"))
        if not isinstance(need.get("output_contract"), dict):
            raise ValueError("invalid_tool_developer_need")
        if stop_after is not None and stop_after not in _STAGES:
            raise ValueError("invalid_tool_developer_stop_stage")
        job_root = self.root / "jobs" / need_id
        candidate = job_root / "candidate"
        checkpoint_path = job_root / "checkpoint.json"
        checkpoint = self._read_checkpoint(checkpoint_path)
        current = checkpoint.get("stage") if checkpoint else None
        for stage in _STAGES:
            if current is not None and _STAGES.index(stage) <= _STAGES.index(current):
                continue
            if stage == "discovered":
                self._discover(job_root, need)
            elif stage == "candidate":
                self._candidate(candidate, capability, need)
            else:
                self._sandbox_verify(candidate)
            checkpoint = {"contract": "ai-trading-tool-developer-checkpoint/v1", "need_id": need_id,
                          "capability": capability, "stage": stage, "updated_at": _now()}
            self._write_json(checkpoint_path, checkpoint)
            current = stage
            if stop_after == stage:
                break
        return checkpoint

    @staticmethod
    def _read_checkpoint(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict) or value.get("contract") != "ai-trading-tool-developer-checkpoint/v1" or value.get("stage") not in _STAGES:
            raise ValueError("invalid_tool_developer_checkpoint")
        return value

    def _discover(self, job_root: Path, need: dict[str, Any]) -> None:
        hints = [str(value) for value in need.get("source_hints") or [] if isinstance(value, str) and value.startswith(("http://", "https://"))]
        self._write_json(job_root / "discovery.json", {
            "contract": "ai-trading-tool-discovery/v1", "need_id": need["need_id"],
            "capability": need["capability"], "output_contract": need["output_contract"], "source_hints": hints,
            "created_at": _now(),
        })

    def _candidate(self, candidate: Path, capability: str, need: dict[str, Any]) -> None:
        candidate.mkdir(parents=True, exist_ok=True)
        self._write_json(candidate / "manifest.json", {
            "contract": "ai-trading-tool-candidate-manifest/v1", "capability": capability, "version": "0.1.0",
            "state": "candidate", "entrypoint": "tool.py", "output_contract": need["output_contract"],
        })
        self._write_json(candidate / "schema.json", {
            "contract": "ai-trading-tool-result/v1", "required": ["contract", "fact_as_of", "data"],
        })
        (candidate / "requirements.lock").write_text("# stdlib-only candidate\n", encoding="utf-8")
        self._write_json(candidate / "fixtures" / "smoke-request.json", {
            "contract": "ai-trading-fact-request/v1", "version": 1, "capability": capability,
            "required_at": "2026-09-01T07:00:00Z", "inputs": {}, "context": {}, "freshness_seconds": 0, "finality": "observed",
        })
        self._write_json(candidate / "tests" / "contract-check.json", {
            "contract": "ai-trading-tool-candidate-test/v1", "checks": ["result_contract", "json_stdout_only"],
        })
        (candidate / "tool.py").write_text(
            "import datetime as dt\nimport json, sys\njson.load(sys.stdin)\nprint(json.dumps({'contract':'ai-trading-tool-result/v1','fact_as_of':dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00','Z'),'data':{'candidate':True}}))\n",
            encoding="utf-8",
        )

    def _sandbox_verify(self, candidate: Path) -> None:
        request = (candidate / "fixtures" / "smoke-request.json").read_bytes()
        outcome = subprocess.run([sys.executable, "tool.py"], cwd=candidate, input=request, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, timeout=10, check=False)
        if outcome.returncode != 0:
            raise RuntimeError("tool_developer_sandbox_process_failed")
        try:
            output = json.loads(outcome.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("tool_developer_sandbox_invalid_json") from exc
        if not isinstance(output, dict) or set(output) != {"contract", "fact_as_of", "data"} or output.get("contract") != "ai-trading-tool-result/v1":
            raise RuntimeError("tool_developer_sandbox_contract_failed")
        self._write_json(candidate / "build-record.json", {
            "contract": "ai-trading-tool-candidate-build/v1", "state": "sandbox_verified", "verified_at": _now(),
        })

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one bounded Tool Developer candidate job")
    parser.add_argument("--development-root", required=True, type=Path)
    parser.add_argument("--need-file", required=True, type=Path)
    parser.add_argument("--stop-after", choices=_STAGES)
    args = parser.parse_args()
    need = json.loads(args.need_file.read_text(encoding="utf-8"))
    print(json.dumps(ToolDeveloperWorker(args.development_root).run(need, stop_after=args.stop_after), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
