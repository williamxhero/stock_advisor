from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .store import now


class LearningProjectionRenderer:
    """Rebuilds a human-readable Markdown view; SQLite remains the fact source."""

    def __init__(self, root: Path, store: Any) -> None:
        self.root = Path(root)
        self.store = store
        self.target = self.root / "data" / "state" / "20_COMPANION_MEMORY.md"

    def render(self) -> Path:
        snapshots = self.store.judgment_snapshots()
        policy = self.store.workflow_policy("research")
        with self.store.connection() as connection:
            outcomes = [dict(row) for row in connection.execute(
                """SELECT o.*,s.kind,a.body_markdown AS judgment_text
                   FROM outcome_checkpoint o JOIN judgment_snapshot s ON s.snapshot_id=o.snapshot_id
                   JOIN narrative_artifact a ON a.artifact_id=s.artifact_id
                   WHERE o.status='complete' ORDER BY o.as_of DESC LIMIT 80"""
            )]
            proposals = [dict(row) for row in connection.execute(
                "SELECT * FROM knowledge_change_proposal ORDER BY created_at DESC LIMIT 80"
            )]
        lines = [
            "# 伴生研判记忆投影",
            "",
            "> 本文件由确定性 renderer 从本地 SQLite 重建。请勿手工编辑；自然语言原文和事件记录以 SQLite 为准。",
            "",
            f"生成时间：{now()}",
            "",
            "## 当前已批准工作流策略",
            "",
        ]
        if policy:
            lines.extend(["```json", json.dumps(policy, ensure_ascii=False, indent=2), "```", ""])
        else:
            lines.extend(["尚无已批准的工作流变更。", ""])
        lines.extend(["## 判断快照", ""])
        if not snapshots:
            lines.extend(["尚无判断快照。", ""])
        for row in snapshots[-120:]:
            snapshot = json.loads(row["snapshot_json"])
            lines.extend([
                f"### {row['kind'].upper()} · {row['as_of']} · {row['verification_status']}",
                "",
                f"- 标的：{', '.join(snapshot.get('subjects') or ['未声明'])}",
                f"- 方向：{snapshot.get('direction', 'unknown')}",
                f"- 周期：{snapshot.get('horizon') or '未声明'}",
                f"- 基准：{snapshot.get('benchmark') or '未声明'}",
                f"- 原始命题：{' / '.join(snapshot.get('original_claims') or [])}",
                "",
            ])
        lines.extend(["## 结果检查", ""])
        if not outcomes:
            lines.extend(["尚无已完成结果检查。", ""])
        for row in outcomes:
            result = json.loads(row["outcome_json"])
            lines.extend([
                f"- {row['horizon']} · {result.get('verification_status', 'unverified')} · {result.get('summary', '')}",
            ])
        lines.extend(["", "## 工作流提案", ""])
        if not proposals:
            lines.extend(["尚无工作流提案。", ""])
        for row in proposals:
            proposal = json.loads(row["changeset_json"])
            lines.append(f"- [{row['state']}] {proposal.get('title', '未命名提案')}：{proposal.get('change', '')}")
        text = "\n".join(lines).rstrip() + "\n"
        self.target.parent.mkdir(parents=True, exist_ok=True)
        temp = self.target.with_suffix(self.target.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, self.target)
        return self.target
