from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CodexResult:
    text: str
    session_id: str | None
    events: list[dict[str, Any]]
    usage: dict[str, Any]


class CodexCliRunner:
    def __init__(self, executable: str="codex") -> None: self.executable=executable
    def probe(self) -> dict[str, Any]:
        path=shutil.which(self.executable)
        if not path:return {"available":False,"error":"codex executable not found"}
        version=subprocess.run([path,"--version"],capture_output=True,text=True,check=False)
        status=subprocess.run([path,"login","status"],capture_output=True,text=True,check=False)
        return {"available":version.returncode==0,"path":path,"version":version.stdout.strip(),"login_ok":status.returncode==0,"login_status":status.stdout.strip()[-500:]}
    def run(
        self,
        prompt: str,
        workspace: Path,
        schema: Path,
        output: Path,
        *,
        resume_session: str | None = None,
        timeout: int = 600,
        search: bool = True,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> CodexResult:
        exe=shutil.which(self.executable)
        if not exe: raise RuntimeError("Codex CLI not found")
        workspace.mkdir(parents=True,exist_ok=True); output.parent.mkdir(parents=True,exist_ok=True)
        # Run packets from the runtime directory, which deliberately is not a Git checkout.
        # The project root is only supplied as read-only packet content, never as a write target.
        cmd=[exe]
        if search: cmd.append("--search")
        if model: cmd.extend(["--model", model])
        if reasoning_effort: cmd.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        cmd.extend(["--sandbox","read-only","--ask-for-approval","never","-C",str(workspace),"exec","--skip-git-repo-check"])
        if resume_session: cmd.extend(["resume",resume_session])
        cmd.extend(["--json","--output-schema",str(schema),"--output-last-message",str(output),"-"])
        proc=subprocess.run(cmd,input=prompt,encoding="utf-8",capture_output=True,timeout=timeout,check=False)
        events=[]; session_id=None; usage={}
        for line in proc.stdout.splitlines():
            try:
                event=json.loads(line); events.append(event)
                if event.get("type")=="thread.started": session_id=event.get("thread_id")
                if event.get("type") in {"turn.completed","turn.failed"}: usage=event.get("usage",usage)
            except json.JSONDecodeError: pass
        if proc.returncode!=0:
            failed_events = [event for event in events if event.get("type") in {"turn.failed", "error"}]
            detail = json.dumps(failed_events[-3:], ensure_ascii=False) if failed_events else ""
            raise RuntimeError((detail + "\n" + proc.stderr + "\n" + proc.stdout)[-8000:] or "Codex CLI failed")
        if not output.exists(): raise RuntimeError("Codex CLI did not write final output")
        return CodexResult(output.read_text(encoding="utf-8"),session_id,events,usage)
