"""Bounded, versioned runtime cache for independently acquired market breadth facts."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


MARKET_BREADTH_CACHE_CONTRACT = "ai-trading-market-breadth-cache/v1"
MARKET_BREADTH_CACHE_MAX_SNAPSHOTS = 256
_CACHE_WRITE_LOCK = threading.Lock()


def _timestamp(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("market breadth cache timestamps must include a timezone")
    return parsed


class MarketBreadthSnapshotCache:
    """Select immutable breadth observations without crossing a frozen time boundary."""

    def __init__(self, path: Path, *, max_snapshots: int = MARKET_BREADTH_CACHE_MAX_SNAPSHOTS) -> None:
        self.path = path
        self.max_snapshots = max(1, int(max_snapshots))

    def snapshots(self) -> list[dict[str, Any]]:
        try:
            payload: Any = None
            for attempt in range(20):
                try:
                    payload = json.loads(self.path.read_text(encoding="utf-8"))
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.005)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        if payload.get("contract") == MARKET_BREADTH_CACHE_CONTRACT:
            rows = payload.get("snapshots")
            return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        # The previous single-snapshot format remains readable and is migrated
        # atomically the next time prefetch appends a successful observation.
        return [dict(payload)] if "fact_as_of" in payload and isinstance(payload.get("data"), dict) else []

    def select(self, *, required_at: str, window_start: str = "", finality: str) -> dict[str, Any] | None:
        try:
            end = _timestamp(required_at)
            start = _timestamp(window_start) if window_start else None
        except (TypeError, ValueError):
            return None
        candidates: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for row in self.snapshots():
            try:
                fact = _timestamp(row["fact_as_of"])
                acquired = _timestamp(row.get("acquired_at") or row["fact_as_of"])
                data = row["data"]
            except (KeyError, TypeError, ValueError):
                continue
            if not isinstance(data, dict) or str(data.get("finality") or "") != finality:
                continue
            if (start is not None and fact < start) or fact > end or acquired > end:
                continue
            candidates.append((fact, acquired, row))
        return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    def latest(self, *, finality: str) -> dict[str, Any] | None:
        candidates: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for row in self.snapshots():
            try:
                fact = _timestamp(row["fact_as_of"])
                acquired = _timestamp(row.get("acquired_at") or row["fact_as_of"])
            except (KeyError, TypeError, ValueError):
                continue
            data = row.get("data")
            if isinstance(data, dict) and str(data.get("finality") or "") == finality:
                candidates.append((fact, acquired, row))
        return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None

    def append(self, snapshot: dict[str, Any]) -> None:
        """Append one observation and atomically publish a bounded cache generation."""
        row = json.loads(json.dumps(snapshot, ensure_ascii=False))
        _timestamp(row["fact_as_of"])
        _timestamp(row["acquired_at"])
        if row.get("result_contract") != "ai-trading-tool-result/v1" or not isinstance(row.get("data"), dict):
            raise ValueError("invalid market breadth snapshot")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        with _CACHE_WRITE_LOCK:
            rows = self.snapshots()
            rows.append(row)
            valid_rows: list[tuple[datetime, datetime, dict[str, Any]]] = []
            for candidate in rows:
                try:
                    valid_rows.append((
                        _timestamp(candidate["fact_as_of"]),
                        _timestamp(candidate.get("acquired_at") or candidate["fact_as_of"]),
                        candidate,
                    ))
                except (KeyError, TypeError, ValueError):
                    continue
            kept = [item[2] for item in sorted(valid_rows, key=lambda item: (item[0], item[1]))[-self.max_snapshots:]]
            payload = {"contract": MARKET_BREADTH_CACHE_CONTRACT, "snapshots": kept}
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent,
                    prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                for attempt in range(20):
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if attempt == 19:
                            raise
                        # Windows can briefly deny replacement while a reader
                        # closes its shared handle. Never fall back to in-place
                        # writes: retry the same complete generation instead.
                        time.sleep(0.005)
                temporary = None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
