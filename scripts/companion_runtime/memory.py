from __future__ import annotations

from dataclasses import dataclass
import json
import uuid
from typing import Any, Protocol


@dataclass(frozen=True)
class MemoryQuery:
    task_key: str
    known_at: str
    text: str = ""
    limit: int = 12


class MemoryRetriever(Protocol):
    def retrieve(self, query: MemoryQuery) -> list[dict[str, Any]]: ...


class SqliteMemoryRetriever:
    """Rebuildable SQLite adapter; immutable artifacts remain the source of truth."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def retrieve(self, query: MemoryQuery) -> list[dict[str, Any]]:
        terms = [term for term in query.text.replace("，", " ").split() if len(term) >= 2][:4]
        clauses = ["a.known_at<=?", "a.kind IN ('h0','m1','m2','outcome','reflection')"]
        values: list[Any] = [query.known_at]
        if terms:
            clauses.append("(" + " OR ".join("a.body_markdown LIKE ?" for _ in terms) + ")")
            values.extend(f"%{term}%" for term in terms)
        with self.store.connection() as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT a.artifact_id,a.kind,a.body_markdown,a.as_of,a.known_at,a.metadata_json,c.task_key
                    FROM narrative_artifact a
                    JOIN companion_cycle c ON c.cycle_id=a.cycle_id
                    WHERE {' AND '.join(clauses)}
                    ORDER BY CASE WHEN c.task_key=? THEN 0 ELSE 1 END,a.known_at DESC
                    LIMIT ?""",
                [*values, query.task_key, max(query.limit * 4, 24)],
            )]
            negative_terms = ("incorrect", "partial", "timing_wrong", "excluded", "错误", "反证", "失败")
            failures = [
                row for row in rows
                if any(term in (row["body_markdown"] + row.get("metadata_json", "")).lower() for term in negative_terms)
            ]
            selected: list[dict[str, Any]] = []
            for row in [*failures[: max(2, query.limit // 3)], *rows]:
                if row["artifact_id"] not in {item["artifact_id"] for item in selected}:
                    selected.append(row)
                if len(selected) >= query.limit:
                    break
            connection.execute(
                """INSERT INTO memory_retrieval_audit(
                     retrieval_id,task_key,query_text,known_at,selected_artifact_ids_json,created_at)
                   VALUES(?,?,?,?,?,datetime('now'))""",
                (str(uuid.uuid4()), query.task_key, query.text, query.known_at,
                 json.dumps([item["artifact_id"] for item in selected], ensure_ascii=False)),
            )
            return selected
