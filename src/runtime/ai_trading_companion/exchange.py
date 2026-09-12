from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable


MAX_CAUSAL_RETRIES = 3


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

    @staticmethod
    def _causal_fields(value: dict[str, Any]) -> tuple[str, int] | None:
        stream = value.get("causal_stream")
        sequence = value.get("causal_sequence")
        if stream is None and sequence is None:
            return None
        if not isinstance(stream, str) or not stream.strip():
            raise ValueError("causal_stream must be a non-empty string")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("causal_sequence must be a positive integer")
        return stream, sequence

    @classmethod
    def _causal_sort_key(cls, path: Path, value: dict[str, Any]) -> tuple[int, str, int, str]:
        causal = cls._causal_fields(value)
        if causal is None:
            # Legacy commands remain readable during migration. New commands
            # never use this branch because the desktop adds causal metadata.
            return (1, "", 0, path.name)
        stream, sequence = causal
        return (0, stream, sequence, path.name)

    def _retry_state_path(self, direction: str) -> Path:
        return self.root / direction / "causal-retries.json"

    def _read_retry_state(self, direction: str) -> dict[str, int]:
        path = self._retry_state_path(direction)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {
            str(key): int(count) for key, count in value.items()
            if isinstance(key, str) and isinstance(count, int) and count > 0
        } if isinstance(value, dict) else {}

    def _write_retry_state(self, direction: str, value: dict[str, int]) -> None:
        target = self._retry_state_path(direction)
        temporary = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, target)

    def _clear_retry(self, direction: str, command_id: str | None) -> None:
        if not command_id:
            return
        state = self._read_retry_state(direction)
        if command_id in state:
            state.pop(command_id, None)
            self._write_retry_state(direction, state)

    def _quarantine_malformed(self, direction: str, path: Path, error: str) -> None:
        destination = self.root / direction / "dead-letter" / path.name
        raw = path.read_text(encoding="utf-8", errors="replace")
        payload = {"error": error, "raw": raw, "recovery": {"bounded": True, "malformed": True}}
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
        path.unlink(missing_ok=True)

    def _recover_processing(self, direction: str) -> None:
        pending = self.root / direction / "pending"
        processing = self.root / direction / "processing"
        for path in sorted(processing.glob("*.json")):
            target = pending / path.name
            if target.exists():
                try:
                    if target.read_bytes() == path.read_bytes():
                        path.unlink()
                        continue
                except OSError:
                    pass
                self._quarantine_malformed(direction, path, "conflicting exchange file during restart recovery")
                continue
            try:
                os.replace(path, target)
            except FileNotFoundError:
                continue

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
        self._recover_processing(direction)
        pending = self.root / direction / "pending"
        processing = self.root / direction / "processing"
        result: list[tuple[Path, dict[str, Any]]] = []
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for path in pending.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._quarantine_malformed(direction, path, f"malformed exchange message: {exc}")
                continue
            if not isinstance(candidate, dict):
                self._quarantine_malformed(direction, path, "exchange message must be an object")
                continue
            try:
                selected = predicate(candidate)
                self._causal_sort_key(path, candidate)
            except ValueError as exc:
                self._quarantine_malformed(direction, path, str(exc))
                continue
            if not selected:
                continue
            candidates.append((path, candidate))
        candidates.sort(key=lambda item: self._causal_sort_key(*item))
        for path, _candidate in candidates:
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
                self._quarantine_malformed(direction, claimed, str(exc))
                claimed.unlink(missing_ok=True)
        return result

    def causal_predecessor_ready(self, direction: str, value: dict[str, Any]) -> bool:
        causal = self._causal_fields(value)
        if causal is None or causal[1] == 1:
            return True
        stream, sequence = causal
        processed = self.root / direction / "processed"
        for path in processed.glob("*.json"):
            try:
                candidate = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(candidate, dict) and self._causal_fields(candidate) == (stream, sequence - 1):
                    return True
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue
        return False

    def defer(self, direction: str, claimed: Path, reason: str) -> dict[str, Any]:
        """Return a causally blocked command to pending, with a persisted bound."""
        self.ensure()
        received = json.loads(claimed.read_text(encoding="utf-8-sig"))
        command_id = str(received.get("command_id") or claimed.stem)
        state = self._read_retry_state(direction)
        attempts = state.get(command_id, 0) + 1
        if attempts < MAX_CAUSAL_RETRIES:
            state[command_id] = attempts
            self._write_retry_state(direction, state)
            os.replace(claimed, self.root / direction / "pending" / claimed.name)
            return {"deferred": True, "attempts": attempts, "command_id": command_id, "reason": reason}

        destination = self.root / direction / "dead-letter" / claimed.name
        payload = {
            "error": reason,
            "received": received,
            "recovery": {"attempts": attempts, "bounded": True, "causal": True},
        }
        temporary = destination.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)
        claimed.unlink(missing_ok=True)
        state.pop(command_id, None)
        self._write_retry_state(direction, state)
        return {"deferred": False, "attempts": attempts, "command_id": command_id, "reason": reason}

    def acknowledge(self, direction: str, claimed: Path) -> None:
        command_id = None
        try:
            value = json.loads(claimed.read_text(encoding="utf-8-sig"))
            command_id = str(value.get("command_id") or "") if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        destination = self.root / direction / "processed" / claimed.name
        os.replace(claimed, destination)
        self._clear_retry(direction, command_id)

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
        command_id = str(payload["received"].get("command_id") or "") if isinstance(payload.get("received"), dict) else None
        self._clear_retry(direction, command_id)
