from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable


def canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class LocalExchange:
    """Atomic, idempotent file exchange. The two sides never share a database."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def ensure(self) -> None:
        for direction in ("to-client", "to-runtime"):
            for state in ("pending", "processing", "processed", "dead-letter"):
                (self.root / direction / state).mkdir(parents=True, exist_ok=True)

    def send(self, direction: str, message_id: str, value: dict[str, Any]) -> Path:
        if direction not in {"to-client", "to-runtime"}:
            raise ValueError(f"invalid exchange direction: {direction}")
        self.ensure()
        body = canonical_json(value)
        digest = hashlib.sha256(body).hexdigest()
        value.setdefault("sha256", digest)
        body = canonical_json(value)
        target = self.root / direction / "pending" / f"{message_id}.json"
        if target.exists():
            if target.read_bytes() != body:
                raise ValueError(f"exchange id conflict: {message_id}")
            return target
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return target

    def receive(self, direction: str) -> list[tuple[Path, dict[str, Any]]]:
        return self.receive_matching(direction, lambda _value: True)

    def receive_matching(
        self, direction: str, predicate: Callable[[dict[str, Any]], bool],
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Claim only messages selected by ``predicate``.

        This lets a foreground chat consume its own stop command while leaving
        unrelated work in pending for the regular Exchange consumer.
        """
        self.ensure()
        pending = self.root / direction / "pending"
        processing = self.root / direction / "processing"
        result: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted(pending.glob("*.json")):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                # The regular consumer owns malformed-message quarantine.
                continue
            if not isinstance(candidate, dict) or not predicate(candidate):
                continue
            claimed = processing / path.name
            try:
                os.replace(path, claimed)
            except FileNotFoundError:
                continue
            try:
                raw = claimed.read_text(encoding="utf-8-sig")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("exchange message must be an object")
                result.append((claimed, value))
            except Exception as exc:
                (self.root / direction / "dead-letter" / claimed.name).write_text(
                    json.dumps({"error": str(exc), "raw": claimed.read_text(encoding="utf-8", errors="replace")}, ensure_ascii=False),
                    encoding="utf-8",
                )
                claimed.unlink(missing_ok=True)
        return result

    def acknowledge(self, direction: str, claimed: Path) -> None:
        destination = self.root / direction / "processed" / claimed.name
        os.replace(claimed, destination)

    def reject(self, direction: str, claimed: Path, reason: str) -> None:
        """Preserve a failed command plus its reason for a human retry decision."""
        self.ensure()
        destination = self.root / direction / "dead-letter" / claimed.name
        payload = {
            "error": reason,
            "received": json.loads(claimed.read_text(encoding="utf-8-sig")),
        }
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
        claimed.unlink(missing_ok=True)
