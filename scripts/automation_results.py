#!/usr/bin/env python3
"""Persist stock_advisor automation results and deliver them through a file Inbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CONTRACT = "ai-decision-message/v1"
SOURCE = "stock_advisor"
DESTINATION = "ai_decision_center_local_inbox_v1"
TERMINAL_STATUSES = {"succeeded", "skipped", "failed"}
RETRY_DELAYS_SECONDS = (60, 300, 900, 3600, 21600)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime"
DEFAULT_DATABASE = DEFAULT_RUNTIME_DIR / "stock_advisor.sqlite3"


def default_inbox_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise RuntimeError("LOCALAPPDATA is not set; pass --inbox-root explicitly")
    return Path(local_app_data) / "AIDecisionCenter" / "inbox"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str, field: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exception:
        raise ValueError(f"{field} must be an ISO-8601 timestamp: {value}") from exception
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset: {value}")
    return parsed


def read_text(path: Path, field: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exception:
        raise ValueError(f"Cannot read {field}: {path}: {exception}") from exception


def normalize_summary(value: str) -> str:
    summary = " ".join(value.split())
    if not summary:
        raise ValueError("summary must not be empty")
    if len(summary) > 240:
        raise ValueError("summary must be at most 240 characters")
    return summary


def validate_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object")
    return value


@dataclass(frozen=True)
class RunPreparation:
    run_id: str
    run_directory: Path
    body_path: Path
    summary_path: Path
    payload_path: Path

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "run_directory": str(self.run_directory),
            "body_path": str(self.body_path),
            "summary_path": str(self.summary_path),
            "payload_path": str(self.payload_path),
        }


class ResultStore:
    """Deep module owning run persistence, Outbox state, and atomic Inbox delivery."""

    def __init__(
        self,
        database_path: Path = DEFAULT_DATABASE,
        runtime_directory: Path = DEFAULT_RUNTIME_DIR,
        inbox_root: Path | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.runtime_directory = Path(runtime_directory)
        self.inbox_root = Path(inbox_root) if inbox_root is not None else default_inbox_root()
        self.staging_root = self.runtime_directory / "staging"

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS automation_run (
                    run_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    project TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'skipped', 'failed')),
                    registry_id TEXT NOT NULL,
                    protocol_id TEXT NOT NULL,
                    summary TEXT,
                    payload_json TEXT,
                    response_sha256 TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS automation_response (
                    run_id TEXT PRIMARY KEY REFERENCES automation_run(run_id),
                    body_markdown TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS delivery_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES automation_run(run_id),
                    destination TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('pending', 'delivered', 'failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE (run_id, destination)
                );

                CREATE INDEX IF NOT EXISTS ix_delivery_outbox_due
                    ON delivery_outbox(state, next_attempt_at);
                PRAGMA user_version = 1;
                """
            )

    def prepare(
        self,
        *,
        task_key: str,
        task_name: str,
        task_type: str,
        scheduled_for: str,
        registry_id: str,
        protocol_id: str,
        project: str = "A股",
    ) -> RunPreparation:
        self.initialize()
        self.dispatch()
        for field_name, value in (
            ("task_key", task_key),
            ("task_name", task_name),
            ("task_type", task_type),
            ("registry_id", registry_id),
            ("protocol_id", protocol_id),
            ("project", project),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        parse_timestamp(scheduled_for, "scheduled_for")

        run_id = str(uuid.uuid4())
        run_directory = self.staging_root / run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        body_path = run_directory / "body.md"
        summary_path = run_directory / "summary.txt"
        payload_path = run_directory / "payload.json"
        body_path.write_text("", encoding="utf-8")
        summary_path.write_text("", encoding="utf-8")
        payload_path.write_text("{}\n", encoding="utf-8")

        started_at = iso_utc(utc_now())
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO automation_run
                        (run_id, source, project, task_key, task_name, task_type,
                         scheduled_for, started_at, status, registry_id, protocol_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                    """,
                    (
                        run_id,
                        SOURCE,
                        project.strip(),
                        task_key.strip(),
                        task_name.strip(),
                        task_type.strip(),
                        scheduled_for,
                        started_at,
                        registry_id.strip(),
                        protocol_id.strip(),
                    ),
                )
        except Exception:
            shutil.rmtree(run_directory, ignore_errors=True)
            raise

        return RunPreparation(run_id, run_directory, body_path, summary_path, payload_path)

    def complete(
        self,
        run_id: str,
        status: str,
        *,
        body_path: Path | None = None,
        summary_path: Path | None = None,
        payload_path: Path | None = None,
        completed_at: str | None = None,
        last_error: str | None = None,
    ) -> dict[str, Any]:
        self.initialize()
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"status must be one of {sorted(TERMINAL_STATUSES)}")

        run_directory = self.staging_root / run_id
        body = read_text(Path(body_path) if body_path else run_directory / "body.md", "body_markdown")
        if not body.strip():
            raise ValueError("body_markdown must not be empty")
        summary = normalize_summary(
            read_text(Path(summary_path) if summary_path else run_directory / "summary.txt", "summary")
        )
        raw_payload = read_text(Path(payload_path) if payload_path else run_directory / "payload.json", "payload")
        try:
            payload = validate_payload(json.loads(raw_payload))
        except json.JSONDecodeError as exception:
            raise ValueError(f"payload is not valid JSON: {exception}") from exception

        completed_at = completed_at or iso_utc(utc_now())
        parse_timestamp(completed_at, "completed_at")
        response_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM automation_run WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown run_id: {run_id}")
            if row["status"] != "running":
                existing = connection.execute(
                    "SELECT response_sha256, status FROM automation_run WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing["status"] != status or existing["response_sha256"] != response_sha256:
                    raise ValueError(f"Run {run_id} is already completed with different content")
                connection.rollback()
                dispatch_result = self.dispatch()
                return {"run_id": run_id, "idempotent": True, "dispatch": dispatch_result}

            message_id = f"stock-advisor:{run_id}"
            envelope = {
                "contract": CONTRACT,
                "message_id": message_id,
                "source": SOURCE,
                "run_id": run_id,
                "project": row["project"],
                "task_key": row["task_key"],
                "task_type": row["task_type"],
                "scheduled_for": row["scheduled_for"],
                "completed_at": completed_at,
                "status": status,
                "registry_id": row["registry_id"],
                "protocol_id": row["protocol_id"],
                "summary": summary,
                "body_markdown": body,
                "payload": payload,
                "response_sha256": response_sha256,
            }
            envelope_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
            now_text = iso_utc(utc_now())
            connection.execute(
                """
                UPDATE automation_run
                SET completed_at = ?, status = ?, summary = ?, payload_json = ?,
                    response_sha256 = ?, last_error = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (
                    completed_at,
                    status,
                    summary,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    response_sha256,
                    last_error,
                    run_id,
                ),
            )
            connection.execute(
                "INSERT INTO automation_response(run_id, body_markdown, created_at) VALUES (?, ?, ?)",
                (run_id, body, now_text),
            )
            connection.execute(
                """
                INSERT INTO delivery_outbox
                    (delivery_id, run_id, destination, envelope_json, state,
                     attempt_count, next_attempt_at, created_at)
                VALUES (?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (str(uuid.uuid4()), run_id, DESTINATION, envelope_json, now_text, now_text),
            )
            connection.commit()

        shutil.rmtree(run_directory, ignore_errors=True)
        return {"run_id": run_id, "idempotent": False, "dispatch": self.dispatch()}

    def dispatch(self) -> dict[str, int]:
        self.initialize()
        now_text = iso_utc(utc_now())
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT delivery_id, run_id, envelope_json, attempt_count
                FROM delivery_outbox
                WHERE state IN ('pending', 'failed') AND next_attempt_at <= ?
                ORDER BY created_at, delivery_id
                """,
                (now_text,),
            ).fetchall()

        delivered = 0
        failed = 0
        pending_directory = self.inbox_root / "pending"
        for row in rows:
            try:
                pending_directory.mkdir(parents=True, exist_ok=True)
                target = pending_directory / f"{row['run_id']}.json"
                payload = row["envelope_json"].encode("utf-8")
                if target.exists():
                    if target.read_bytes() != payload:
                        raise RuntimeError(f"Inbox file already exists with different content: {target}")
                else:
                    temporary = pending_directory / f"{row['run_id']}.{uuid.uuid4().hex}.tmp"
                    with temporary.open("xb") as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)

                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET state = 'delivered', attempt_count = attempt_count + 1,
                            last_error = NULL, delivered_at = ?
                        WHERE delivery_id = ?
                        """,
                        (iso_utc(utc_now()), row["delivery_id"]),
                    )
                delivered += 1
            except Exception as exception:  # Keep the durable Outbox row for later recovery.
                attempts = int(row["attempt_count"]) + 1
                delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
                next_attempt = iso_utc(utc_now() + timedelta(seconds=delay))
                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE delivery_outbox
                        SET state = 'failed', attempt_count = ?, next_attempt_at = ?, last_error = ?
                        WHERE delivery_id = ?
                        """,
                        (attempts, next_attempt, str(exception)[:2000], row["delivery_id"]),
                    )
                failed += 1
        return {"due": len(rows), "delivered": delivered, "failed": failed}

    def status(self) -> dict[str, Any]:
        self.initialize()
        with self._connection() as connection:
            runs = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM automation_run GROUP BY status"
                )
            }
            outbox = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM delivery_outbox GROUP BY state"
                )
            }
            errors = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT run_id, state, attempt_count, next_attempt_at, last_error
                    FROM delivery_outbox
                    WHERE last_error IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                )
            ]
        return {"database": str(self.database_path), "inbox": str(self.inbox_root), "runs": runs, "outbox": outbox, "errors": errors}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()


def add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--runtime-directory", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--inbox-root", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Create a durable running record and staging files")
    add_store_arguments(prepare)
    prepare.add_argument("--task-key", required=True)
    prepare.add_argument("--task-name", required=True)
    prepare.add_argument("--task-type", required=True)
    prepare.add_argument("--scheduled-for", required=True)
    prepare.add_argument("--registry-id", required=True)
    prepare.add_argument("--protocol-id", required=True)
    prepare.add_argument("--project", default="A股")

    complete = subparsers.add_parser("complete", help="Persist the final response and create an Outbox message")
    add_store_arguments(complete)
    complete.add_argument("--run-id", required=True)
    complete.add_argument("--status", required=True, choices=sorted(TERMINAL_STATUSES))
    complete.add_argument("--body-file", type=Path)
    complete.add_argument("--summary-file", type=Path)
    complete.add_argument("--payload-file", type=Path)
    complete.add_argument("--completed-at")
    complete.add_argument("--last-error")

    dispatch = subparsers.add_parser("dispatch", help="Retry due Outbox deliveries")
    add_store_arguments(dispatch)
    status = subparsers.add_parser("status", help="Show run and Outbox health")
    add_store_arguments(status)
    return parser


def store_from_args(args: argparse.Namespace) -> ResultStore:
    return ResultStore(args.database, args.runtime_directory, args.inbox_root)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        store = store_from_args(args)
        if args.command == "prepare":
            result: Any = store.prepare(
                task_key=args.task_key,
                task_name=args.task_name,
                task_type=args.task_type,
                scheduled_for=args.scheduled_for,
                registry_id=args.registry_id,
                protocol_id=args.protocol_id,
                project=args.project,
            ).to_dict()
        elif args.command == "complete":
            result = store.complete(
                args.run_id,
                args.status,
                body_path=args.body_file,
                summary_path=args.summary_file,
                payload_path=args.payload_file,
                completed_at=args.completed_at,
                last_error=args.last_error,
            )
        elif args.command == "dispatch":
            result = store.dispatch()
        else:
            result = store.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exception:
        print(json.dumps({"error": str(exception)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
