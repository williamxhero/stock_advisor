"""Runtime-owned AgentRoleSpec/v1.

Agent roles are internal capability boundaries, not user-facing personas.  A
role may read a bounded, versioned packet and return a bounded artifact, but
it never owns a market fact, portfolio fact, MemoryHub record, schedule,
production strategy, or published message.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_contract import CONTRACT as AGENT_CONTRACT
from .agent_contract import VERSION as AGENT_CONTRACT_VERSION
from .agent_contract import validate_input as validate_agent_input

CONTRACT = "AgentRoleSpec/v1"
VERSION = 1
REPLAY_CONTRACT = "AgentRoleReplay/v1"
INTERNAL_VISIBILITY = "internal"
ROLE_IDS = (
    "fundamental",
    "market_structure",
    "news_event",
    "propagation_sentiment",
    "bull",
    "bear",
    "risk",
    "coordinator",
)
STAGES = frozenset({"m0_research", "m1_research", "m2", "outcome_research", "chat_research"})
STAGE_ROLES: dict[str, tuple[str, ...]] = {
    "m0_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
    "m1_research": ROLE_IDS,
    "m2": ("coordinator",),
    "outcome_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
    "chat_research": ("fundamental", "market_structure", "news_event", "propagation_sentiment", "risk", "coordinator"),
}
STATUSES = frozenset({"pending", "running", "succeeded", "partial", "blocked", "failed", "skipped", "unknown"})
EFFECTS = frozenset({"evidence_only", "support", "oppose", "block", "coordinate", "unknown"})
_COORDINATOR_EFFECTS = {
    "pending": "unknown",
    "running": "unknown",
    "succeeded": "coordinate",
    "partial": "block",
    "blocked": "block",
    "failed": "block",
    "skipped": "block",
    "unknown": "unknown",
}

COORDINATOR_CONTRACT = "CoordinatorSpec/v1"
COORDINATOR_VERSION = 1
COORDINATOR_TERMINAL_STATUSES = frozenset({"succeeded", "partial", "blocked", "failed", "skipped"})
COORDINATOR_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"pending", "running", "blocked", "skipped"}),
    "running": frozenset({"running", "succeeded", "partial", "blocked", "failed", "skipped", "unknown"}),
    "succeeded": frozenset({"succeeded", "pending"}),
    "partial": frozenset({"partial", "pending"}),
    "blocked": frozenset({"blocked", "pending"}),
    "failed": frozenset({"failed", "pending"}),
    "skipped": frozenset({"skipped", "pending"}),
    "unknown": frozenset({"unknown", "pending", "running"}),
}

# SPEC-95 is deliberately represented as data at the runtime boundary.  The
# graph is also useful to callers which replay a run without importing the
# implementation details of the six tickets.
SPEC95_NODE_IDS = tuple(f"SPEC-95.{index}" for index in range(1, 7))
SPEC95_ISSUE_NUMBER = 95
SPEC95_BASELINE_CONTRACT = "Spec95Baseline/v1"
SPEC95_BASELINE_FILENAME = "spec95-baseline.json"
SPEC95_DEPENDENCY_GRAPH: dict[str, list[str]] = {
    SPEC95_NODE_IDS[0]: [],
    SPEC95_NODE_IDS[1]: [SPEC95_NODE_IDS[0]],
    SPEC95_NODE_IDS[2]: [SPEC95_NODE_IDS[1]],
    SPEC95_NODE_IDS[3]: [SPEC95_NODE_IDS[2]],
    SPEC95_NODE_IDS[4]: [SPEC95_NODE_IDS[3]],
    SPEC95_NODE_IDS[5]: [SPEC95_NODE_IDS[4]],
    "coordinator": [SPEC95_NODE_IDS[5]],
}


def _spec95_gate(value: Any) -> bool:
    """Read a qualification gate without treating a truthy object as proof."""
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        flags = [value[key] for key in ("passed", "verified") if key in value]
        # A receipt with contradictory flags is not positive evidence.  This
        # matters when a producer includes both its legacy ``passed`` field
        # and the newer ``verified`` field.
        return bool(flags) and all(flag is True for flag in flags)
    return False


def validate_spec95_baseline(value: Any) -> dict[str, Any] | None:
    """Validate the shape and consistency of a SPEC-95 baseline receipt.

    ``install_qualification`` only proves that a local frozen replay works.
    It cannot prove the state of issue #95 or any delivery/synchronization
    evidence. This helper checks receipt contents only; its output is not
    authority to authorize downstream work.
    """
    if not isinstance(value, dict):
        return None
    # The issue snapshot is the authority for this receipt. Top-level
    # convenience fields are accepted only as redundant copies below; they
    # cannot manufacture an issue state in a packet supplied by a caller.
    issue = value.get("issue") if isinstance(value.get("issue"), dict) else None
    if issue is None or issue.get("number") != SPEC95_ISSUE_NUMBER:
        return None
    issue_number = value.get("issue_number")
    if issue_number is not None and issue_number != issue["number"]:
        return None
    issue_state_value = value.get("issue_state")
    if issue.get("state") is None:
        return None
    if issue_state_value is not None and str(issue_state_value).casefold() != str(issue["state"]).casefold():
        return None
    issue_state_value = issue["state"]
    issue_state = str(issue_state_value or "").casefold()
    if issue_state not in {"closed", "completed", "done", "succeeded"}:
        return None
    declared = value.get("declared_specs")
    issue_declared = issue.get("declared_specs")
    # The declaration must come from the issue snapshot itself.  A
    # caller-supplied top-level copy cannot establish what issue #95 declared.
    if not isinstance(issue_declared, (list, tuple, set)):
        return None
    if declared is not None and declared != issue_declared:
        return None
    declared = issue_declared
    if not isinstance(declared, (list, tuple, set)):
        return None
    declared_ids = {
        str(item.get("id") or item.get("spec") or item.get("key")) if isinstance(item, dict) else str(item)
        for item in declared
    }
    if declared_ids != set(SPEC95_NODE_IDS):
        return None
    synchronization_values = [
        value[key] for key in ("synchronization_state", "sync_state", "synchronization")
        if key in value
    ]
    if synchronization_values and any(item != synchronization_values[0] for item in synchronization_values[1:]):
        return None
    synchronization = synchronization_values[0] if synchronization_values else None
    if isinstance(synchronization, dict):
        synchronized = _spec95_gate(synchronization)
    else:
        synchronized = synchronization is True or str(synchronization or "").casefold() in {"synchronized", "synchronised", "in_sync", "verified"}
    if not synchronized:
        return None
    issue_state_values = [value[key] for key in ("spec_issue_states", "issue_states") if key in value]
    if issue_state_values and any(item != issue_state_values[0] for item in issue_state_values[1:]):
        return None
    issue_states = issue_state_values[0] if issue_state_values else None
    declared_issue_states = {
            str(item.get("id") or item.get("spec") or item.get("key")): item.get("state")
            for item in declared
            if isinstance(item, dict)
        } if all(isinstance(item, dict) for item in declared) else None
    if issue_states is not None and declared_issue_states is not None and issue_states != declared_issue_states:
        return None
    if not isinstance(issue_states, dict):
        issue_states = declared_issue_states
    if not isinstance(issue_states, dict) or set(map(str, issue_states)) != set(SPEC95_NODE_IDS):
        return None
    if any(str(issue_states[node]).casefold() not in {"closed", "completed", "done", "succeeded"} for node in SPEC95_NODE_IDS):
        return None
    dependency_values = [value[key] for key in ("dependency_evidence", "dependency_evidence_gates") if key in value]
    delivery_values = [value[key] for key in ("delivery_evidence", "delivery_evidence_gates") if key in value]
    if dependency_values and any(item != dependency_values[0] for item in dependency_values[1:]):
        return None
    if delivery_values and any(item != delivery_values[0] for item in delivery_values[1:]):
        return None
    dependency = dependency_values[0] if dependency_values else None
    delivery = delivery_values[0] if delivery_values else None
    if isinstance(dependency, dict) and isinstance(dependency.get("nodes"), dict):
        dependency = dependency["nodes"]
    if isinstance(delivery, dict) and isinstance(delivery.get("nodes"), dict):
        delivery = delivery["nodes"]
    if not isinstance(dependency, dict) or not isinstance(delivery, dict):
        return None
    if not set(SPEC95_NODE_IDS) <= set(dependency) or not set(SPEC95_NODE_IDS) <= set(delivery):
        return None
    if not all(_spec95_gate(dependency.get(node)) for node in SPEC95_NODE_IDS):
        return None
    if not all(_spec95_gate(delivery.get(node)) for node in SPEC95_NODE_IDS):
        return None
    normalized = copy.deepcopy(value)
    normalized["spec_issue_states"] = {
        node: str(issue_states[node]).casefold() for node in SPEC95_NODE_IDS
    }
    return normalized


def load_authoritative_spec95_baseline(runtime_root: str | os.PathLike[str] | None = None) -> dict[str, Any] | None:
    """Read the runtime-owned SPEC-95 receipt, never packet metadata.

    The receipt is outside the stage packet and is written by a local runtime
    acquisition/import step. A missing, malformed, or tampered receipt returns
    ``None`` so the coordinator remains fail-closed.
    """
    if runtime_root is None:
        configured_root = os.environ.get("AI_TRADING_COMPANION_RUNTIME_ROOT")
        if configured_root:
            runtime_root = configured_root
        else:
            home = os.environ.get("AI_TRADING_COMPANION_HOME")
            runtime_root = Path(home) / "runtime" if home else Path("D:/APP/AITradingCompanion/runtime")
    path = Path(runtime_root) / "coordinator-state" / SPEC95_BASELINE_FILENAME
    try:
        with path.open(encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        not isinstance(envelope, dict)
        or envelope.get("contract") != SPEC95_BASELINE_CONTRACT
        or envelope.get("version") != 1
    ):
        return None
    baseline = envelope.get("baseline")
    verified = validate_spec95_baseline(baseline)
    if verified is None:
        return None
    expected_digest = envelope.get("baseline_sha256")
    if not isinstance(expected_digest, str) or expected_digest != sha256(verified):
        return None
    return verified


class Spec95BaselineStore:
    """Persist and load the runtime-owned SPEC-95 qualification receipt."""

    def __init__(self, runtime_root: str | os.PathLike[str]):
        self.path = Path(runtime_root) / "coordinator-state" / SPEC95_BASELINE_FILENAME

    def load(self) -> dict[str, Any] | None:
        return load_authoritative_spec95_baseline(self.path.parent.parent)

    def persist(self, baseline: dict[str, Any]) -> dict[str, Any]:
        verified = validate_spec95_baseline(baseline)
        if verified is None:
            raise ValueError("invalid SPEC-95 baseline receipt")
        envelope = {
            "contract": SPEC95_BASELINE_CONTRACT,
            "version": 1,
            "baseline": verified,
            "baseline_sha256": sha256(verified),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(envelope, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        return copy.deepcopy(verified)


def spec95_runtime_qualification(baseline: dict[str, Any] | None = None) -> tuple[dict[str, str], dict[str, bool]]:
    """Translate a verified SPEC-95 baseline receipt into runtime gates.

    The legacy return shape is retained for installed-contract callers.  The
    returned maps are not an authorization: only a validated baseline receipt
    may set ``spec95_baseline_verified`` and pass the runtime coordinator gate.
    """
    verified = validate_spec95_baseline(baseline)
    if verified is None:
        # Absence of a receipt is an unverified prerequisite, not evidence
        # that the six tickets completed.  Keep the packet shape deterministic
        # while making the CoordinatorSpec gate fail closed.
        return ({node: "blocked" for node in SPEC95_NODE_IDS}, {node: False for node in SPEC95_NODE_IDS})
    return (
        {node: str(verified["spec_issue_states"][node]).casefold() for node in SPEC95_NODE_IDS},
        {node: True for node in SPEC95_NODE_IDS},
    )


def attach_runtime_qualification(packet: dict[str, Any]) -> dict[str, Any]:
    """Add the runtime-owned SPEC-95 gates before a packet is hashed.

    This is intentionally additive only when both fields are absent.  A
    packet carrying partial, stale, or contradictory qualification metadata
    must remain unchanged so ``spec95_dependency_states`` can fail closed.
    """
    value = copy.deepcopy(packet)
    # Packets, evidence, and context are caller-controlled inputs. Only the
    # runtime-owned receipt can elevate runtime-owned gates.
    baseline = load_authoritative_spec95_baseline()
    has_issue_states = "spec_issue_states" in value
    has_evidence_gates = "spec_evidence_gates" in value
    if not has_issue_states and not has_evidence_gates:
        issue_states, evidence_gates = spec95_runtime_qualification(baseline)
        value["spec_issue_states"] = issue_states
        value["spec_evidence_gates"] = evidence_gates
        value["spec95_baseline_verified"] = baseline is not None
    elif has_issue_states and has_evidence_gates:
        supplied_states = value.get("spec_issue_states")
        supplied_gates = value.get("spec_evidence_gates")
        expected_states, expected_gates = spec95_runtime_qualification(baseline)
        matches_verified_baseline = (
            baseline is not None
            and supplied_states == expected_states
            and supplied_gates == expected_gates
        )
        value["spec95_baseline_verified"] = matches_verified_baseline
        if not matches_verified_baseline:
            # Complete-looking metadata without a verified receipt is not an
            # authorization.  Keep partial metadata untouched for diagnostic
            # replay, but neutralize a full forged/contradictory pair.
            if (
                isinstance(supplied_states, dict)
                and set(map(str, supplied_states)) == set(SPEC95_NODE_IDS)
                and isinstance(supplied_gates, dict)
                and set(map(str, supplied_gates)) == set(SPEC95_NODE_IDS)
            ):
                value["spec_issue_states"] = {node: "blocked" for node in SPEC95_NODE_IDS}
                value["spec_evidence_gates"] = {node: False for node in SPEC95_NODE_IDS}
    return value


def spec95_dependency_states(
    issue_states: dict[str, str] | None = None,
    evidence_gates: dict[str, bool] | None = None,
) -> dict[str, str]:
    """Translate issue/evidence gates into fail-closed CoordinatorSpec state.

    Every SPEC-95 node must have an explicitly verified issue state and
    evidence gate.  Missing or failed prerequisites are blocked rather than
    being inferred as complete; this prevents an ordinary packet that omits
    the qualification metadata from authorizing downstream work.
    """
    issue_states = {str(key): str(value).casefold() for key, value in (issue_states or {}).items()}
    evidence_gates = {str(key): value for key, value in (evidence_gates or {}).items()}
    states: dict[str, str] = {}
    for node in SPEC95_NODE_IDS:
        issue = issue_states.get(node)
        if issue is None:
            state = "blocked"
        elif issue in {"closed", "completed", "done", "succeeded"}:
            state = "succeeded"
        elif issue in {"blocked", "failed", "rejected", "cancelled", "canceled"}:
            state = "blocked"
        else:
            state = "pending"
        # Evidence must be explicitly true.  Missing, false, or malformed
        # gate values are all unverified and therefore fail closed.
        if evidence_gates.get(node) is not True:
            state = "blocked"
        states[node] = state
    # A failed prerequisite closes the downstream frontier.  Propagate only
    # terminal failure; pending work remains waiting and can still recover.
    for node in SPEC95_NODE_IDS[1:]:
        dependency = SPEC95_DEPENDENCY_GRAPH[node][0]
        if states[dependency] in {"partial", "blocked", "failed", "skipped", "unknown"}:
            states[node] = "blocked"
    states["coordinator"] = "pending"
    if states[SPEC95_NODE_IDS[-1]] in {"partial", "blocked", "failed", "skipped", "unknown"}:
        states["coordinator"] = "blocked"
    return states


def spec95_frontier(
    issue_states: dict[str, str] | None = None,
    evidence_gates: dict[str, bool] | None = None,
) -> dict[str, list[str]]:
    """Return the runnable/blocked SPEC-95 frontier."""
    return coordinator_frontier(
        SPEC95_DEPENDENCY_GRAPH,
        spec95_dependency_states(issue_states, evidence_gates),
    )


def validate_coordinator_transition(previous: str, current: str) -> None:
    """Validate the externally observable CoordinatorSpec lifecycle."""
    if previous not in STATUSES or current not in STATUSES:
        raise ValueError("invalid CoordinatorSpec lifecycle status")
    if current not in COORDINATOR_TRANSITIONS[previous]:
        raise ValueError(f"invalid CoordinatorSpec transition: {previous} -> {current}")


def validate_dependency_graph(graph: dict[str, list[str] | tuple[str, ...]]) -> dict[str, list[str]]:
    """Return a canonical, acyclic dependency graph or fail closed."""
    if not isinstance(graph, dict) or not graph:
        raise ValueError("CoordinatorSpec dependency graph must be a non-empty object")
    nodes = {str(node) for node in graph}
    if any(not node.strip() for node in nodes):
        raise ValueError("CoordinatorSpec dependency graph contains an empty node")
    canonical: dict[str, list[str]] = {}
    for node, dependencies in graph.items():
        node = str(node)
        if not isinstance(dependencies, (list, tuple, set)):
            raise ValueError("CoordinatorSpec dependencies must be lists")
        deps = sorted({str(dep) for dep in dependencies})
        if node in deps or any(dep not in nodes for dep in deps):
            raise ValueError("CoordinatorSpec dependency graph contains an invalid edge")
        canonical[node] = deps
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("CoordinatorSpec dependency graph contains a cycle")
        if node in visited:
            return
        visiting.add(node)
        for dependency in canonical[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(canonical):
        visit(node)
    return canonical


def coordinator_frontier(
    graph: dict[str, list[str] | tuple[str, ...]],
    states: dict[str, str],
) -> dict[str, list[str]]:
    """Compute runnable and fail-closed nodes from a declared SPEC graph."""
    canonical = validate_dependency_graph(graph)
    normalized = {str(node): str(state) for node, state in states.items()}
    unknown_nodes = sorted(set(normalized) - set(canonical))
    if unknown_nodes:
        raise ValueError("CoordinatorSpec states contain unknown nodes: " + ", ".join(unknown_nodes))
    if any(status not in STATUSES for status in normalized.values()):
        raise ValueError("CoordinatorSpec states contain an invalid lifecycle status")
    frontier: list[str] = []
    blocked: list[str] = []
    waiting: list[str] = []
    for node in sorted(canonical):
        state = normalized.get(node, "pending")
        if state != "pending":
            continue
        dependency_states = [normalized.get(dep, "pending") for dep in canonical[node]]
        if any(dep_state in {"partial", "blocked", "failed", "skipped", "unknown"} for dep_state in dependency_states):
            blocked.append(node)
        elif all(dep_state == "succeeded" for dep_state in dependency_states):
            frontier.append(node)
        else:
            waiting.append(node)
    return {"frontier": frontier, "blocked": blocked, "waiting": waiting}


@dataclass
class CoordinatorLifecycle:
    """Small runtime-owned lifecycle journal used by CoordinatorSpec."""

    node_id: str
    status: str = "pending"
    records: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if not self.node_id.strip():
            raise ValueError("CoordinatorSpec node_id is required")
        if self.status not in STATUSES:
            raise ValueError("invalid CoordinatorSpec lifecycle status")
        self.records = list(self.records or [])
        if not self.records:
            # Pure artifact construction must be replayable. Durable callers
            # provide a real timestamp through transition(..., at=...).
            self.records.append({"node_id": self.node_id, "from": None, "to": self.status, "at": 0.0})

    def transition(self, status: str, *, at: float | None = None, reason: str | None = None) -> dict[str, Any]:
        validate_coordinator_transition(self.status, status)
        record = {"node_id": self.node_id, "from": self.status, "to": status, "at": at if at is not None else time.time()}
        if reason:
            record["reason"] = str(reason)
        self.status = status
        self.records.append(record)
        return copy.deepcopy(record)

    def as_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "status": self.status, "records": copy.deepcopy(self.records)}


@dataclass
class CoordinatorSpec:
    """Declared dependency graph plus observable state for one coordinator run."""

    dependency_graph: dict[str, list[str] | tuple[str, ...]]
    states: dict[str, str] | None = None
    lifecycles: dict[str, CoordinatorLifecycle] | None = None

    def __post_init__(self) -> None:
        self.dependency_graph = validate_dependency_graph(self.dependency_graph)
        self.states = {node: "pending" for node in self.dependency_graph} | {
            str(node): str(status) for node, status in (self.states or {}).items()
        }
        if set(self.states) != set(self.dependency_graph) or any(status not in STATUSES for status in self.states.values()):
            raise ValueError("CoordinatorSpec states must match the dependency graph")
        self.lifecycles = dict(self.lifecycles or {})
        for node, status in self.states.items():
            self.lifecycles.setdefault(node, CoordinatorLifecycle(node, status=status))

    def transition(self, node_id: str, status: str, *, at: float | None = None, reason: str | None = None) -> dict[str, Any]:
        if node_id not in self.dependency_graph:
            raise ValueError("CoordinatorSpec transition references an unknown node")
        record = self.lifecycles[node_id].transition(status, at=at, reason=reason)
        self.states[node_id] = status
        return record

    def frontier(self) -> dict[str, list[str]]:
        return coordinator_frontier(self.dependency_graph, self.states)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": COORDINATOR_CONTRACT,
            "version": COORDINATOR_VERSION,
            "dependency_graph": copy.deepcopy(self.dependency_graph),
            "states": copy.deepcopy(self.states),
            "frontier": self.frontier(),
            "lifecycles": {node: lifecycle.as_dict() for node, lifecycle in self.lifecycles.items()},
        }


def build_coordinator_spec(
    dependency_graph: dict[str, list[str] | tuple[str, ...]],
    *,
    states: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the canonical externally observable CoordinatorSpec envelope."""
    return CoordinatorSpec(dependency_graph, states=states).as_dict()


class CoordinatorStateStore:
    """Durable, atomic state for takeover/retry and duplicate execution guards."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except FileNotFoundError:
            return {}

    def _write(self, value: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="coordinator-", suffix=".json", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @contextmanager
    def _locked(self):
        """Serialize read/claim/write across concurrent runtime processes."""
        lock_path = self.path + ".lock"
        os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
        with open(lock_path, "a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def get(self, node_id: str) -> dict[str, Any] | None:
        with self._locked():
            return copy.deepcopy(self._read().get(node_id))

    def snapshot_graph(
        self,
        graph: dict[str, list[str] | tuple[str, ...]],
        states: dict[str, str],
        *,
        now: float | None = None,
        preserve_running: bool = True,
    ) -> dict[str, dict[str, Any]]:
        """Persist every declared node's qualification lifecycle atomically."""
        canonical = validate_dependency_graph(graph)
        if set(states) != set(canonical) or any(value not in STATUSES for value in states.values()):
            raise ValueError("CoordinatorSpec graph snapshot states must match the dependency graph")
        timestamp = time.time() if now is None else float(now)

        def lifecycle_for(node: str, desired: str, current: dict[str, Any] | None) -> CoordinatorLifecycle:
            if isinstance(current, dict) and isinstance(current.get("lifecycle"), list):
                lifecycle = CoordinatorLifecycle(
                    node,
                    status=str(current.get("status") or "pending"),
                    records=current["lifecycle"],
                )
            else:
                lifecycle = CoordinatorLifecycle(node, status="pending")
            if lifecycle.status == desired:
                return lifecycle
            if lifecycle.status in COORDINATOR_TERMINAL_STATUSES and desired == "pending":
                return lifecycle
            if lifecycle.status == "running":
                if desired == "pending":
                    return lifecycle
                lifecycle.transition(desired, at=timestamp, reason="qualification snapshot refreshed")
                return lifecycle
            if lifecycle.status != "pending":
                lifecycle.transition("pending", at=timestamp, reason="qualification snapshot refreshed")
            if desired == "pending":
                return lifecycle
            if desired in {"succeeded", "partial", "failed"}:
                lifecycle.transition("running", at=timestamp, reason="qualification snapshot")
            lifecycle.transition(desired, at=timestamp, reason="qualification snapshot")
            return lifecycle

        with self._locked():
            state = self._read()
            result: dict[str, dict[str, Any]] = {}
            for node in canonical:
                current = state.get(node) if isinstance(state.get(node), dict) else None
                running_claim = preserve_running and isinstance(current, dict) and current.get("status") == "running"
                terminal_claim = (
                    isinstance(current, dict)
                    and current.get("status") in COORDINATOR_TERMINAL_STATUSES
                    and states[node] == "pending"
                )
                lifecycle = (
                    CoordinatorLifecycle(node, status="running", records=current.get("lifecycle"))
                    if running_claim else (
                        CoordinatorLifecycle(
                            node, status=str(current.get("status")), records=current.get("lifecycle"),
                        ) if terminal_claim else lifecycle_for(node, states[node], current)
                    )
                )
                persisted_status = (
                    str(current.get("status")) if running_claim or terminal_claim else states[node]
                )
                state[node] = {
                    "node_id": node,
                    "idempotency_key": str((current or {}).get("idempotency_key") or f"qualification:{node}"),
                    "status": persisted_status,
                    "lease_until": (current or {}).get("lease_until"),
                    "execution_count": int((current or {}).get("execution_count") or 0),
                    "execution_generation": int((current or {}).get("execution_generation") or 0),
                    "lifecycle": lifecycle.records,
                }
                result[node] = lifecycle.as_dict()
            self._write(state)
            return result

    def claim(self, node_id: str, idempotency_key: str, *, lease_seconds: float = 300, now: float | None = None) -> dict[str, Any]:
        if not node_id.strip() or not idempotency_key.strip():
            raise ValueError("CoordinatorSpec node_id and idempotency_key are required")
        timestamp = time.time() if now is None else float(now)
        with self._locked():
            state = self._read()
            current = state.get(node_id)
            if isinstance(current, dict) and current.get("idempotency_key") == idempotency_key:
                if current.get("status") in COORDINATOR_TERMINAL_STATUSES:
                    return {**copy.deepcopy(current), "duplicate": True, "takeover": False}
                if current.get("status") == "running" and float(current.get("lease_until") or 0) > timestamp:
                    return {**copy.deepcopy(current), "duplicate": True, "takeover": False}
            previous = str(current.get("status") if isinstance(current, dict) else "pending")
            takeover = previous == "running" and float(current.get("lease_until") or 0) <= timestamp if isinstance(current, dict) else False
            if previous not in STATUSES:
                previous = "pending"
            if previous != "pending" and not takeover:
                validate_coordinator_transition(previous, "pending")
            lifecycle = CoordinatorLifecycle(node_id, status=previous if takeover else "pending", records=(current or {}).get("lifecycle") if isinstance(current, dict) else None)
            if lifecycle.status != "running":
                lifecycle.transition("running", at=timestamp, reason="takeover" if takeover else "claimed")
            execution_generation = int((current or {}).get("execution_generation") or 0) + 1
            record = {"node_id": node_id, "idempotency_key": idempotency_key, "status": "running", "lease_until": timestamp + max(1, lease_seconds), "execution_count": int((current or {}).get("execution_count") or 0) + 1, "execution_generation": execution_generation, "lifecycle": lifecycle.records}
            state[node_id] = record
            self._write(state)
            return {**copy.deepcopy(record), "duplicate": False, "takeover": takeover}

    def finish(
        self,
        node_id: str,
        status: str,
        *,
        idempotency_key: str,
        execution_generation: int | None = None,
        reason: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("invalid CoordinatorSpec finish status")
        with self._locked():
            state = self._read()
            current = state.get(node_id)
            if not isinstance(current, dict) or current.get("idempotency_key") != idempotency_key:
                raise ValueError("CoordinatorSpec finish does not match the active claim")
            # The scheduling key identifies the logical work item, not the
            # lease owner.  A stale worker can therefore have the same key
            # after a takeover; only the generation identifies the worker
            # which is allowed to close this claim.
            if execution_generation is None:
                raise ValueError("CoordinatorSpec finish requires an execution generation")
            if current.get("execution_generation") != execution_generation:
                raise ValueError("CoordinatorSpec finish does not own the active execution generation")
            current_status = str(current.get("status") or "running")
            if current_status in COORDINATOR_TERMINAL_STATUSES:
                if current_status != status:
                    raise ValueError("CoordinatorSpec finish cannot change a completed claim")
                return copy.deepcopy(current)
            lease_until = current.get("lease_until")
            timestamp = time.time() if now is None else float(now)
            if lease_until is not None and float(lease_until) <= timestamp:
                raise ValueError("CoordinatorSpec finish does not own the active lease")
            lifecycle = CoordinatorLifecycle(node_id, status=current_status, records=current.get("lifecycle"))
            lifecycle.transition(status, at=timestamp, reason=reason)
            current.update({"status": status, "lease_until": None, "lifecycle": lifecycle.records})
            state[node_id] = current
            self._write(state)
            return copy.deepcopy(current)

_FORBIDDEN_KEYS = frozenset({
    "chain_of_thought", "cot", "thoughts", "reasoning_trace", "private_reasoning",
    "scratchpad", "deliberation", "hidden_reasoning", "h0", "h0_text",
    "h0_propositions", "h0_actions", "m2_draft", "credentials", "api_key",
})
_FORBIDDEN_WRITES = frozenset({
    "markethub", "markethub_facts", "portfolio", "positions", "memoryhub",
    "memoryhub_formal_records", "schedule", "production_strategy", "published_messages",
    "exchange", "orders", "broker",
})
_M1_FORBIDDEN_KEYS = frozenset({
    "h0", "h0_text", "h0_propositions", "h0_actions", "m2", "m2_draft",
    "user_chat_after_cutoff", "post_h0_chat", "private_facts_after_h0",
})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _walk_keys(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"AgentRoleSpec forbids protected context at {path}.{key}")
            if normalized in _FORBIDDEN_WRITES:
                raise ValueError(f"AgentRoleSpec forbids fact-system writes at {path}.{key}")
            _walk_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_keys(child, path=f"{path}[{index}]")


def _find_forbidden(value: Any, names: frozenset[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in names:
                return str(key)
            found = _find_forbidden(child, names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_forbidden(child, names)
            if found:
                return found
    return None


def _refs(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty references")
    return sorted(set(value))


def _definition(
    role: str,
    *,
    allowed_inputs: tuple[str, ...],
    forbidden_actions: tuple[str, ...],
    output_kinds: tuple[str, ...],
    effects: tuple[str, ...],
    may_block: bool = False,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role,
        "visibility": INTERNAL_VISIBILITY,
        "allowed_inputs": list(allowed_inputs),
        "forbidden_actions": list(forbidden_actions),
        "output_kinds": list(output_kinds),
        "allowed_effects": list(effects),
        "may_block": may_block,
        "write_permissions": [],
    }


_COMMON_FORBIDDEN = (
    "write_market_facts", "write_portfolio_facts", "write_memoryhub_records",
    "write_schedule", "write_production_strategy", "publish_user_message",
    "place_order", "change_permissions", "expose_internal_role",
)

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "fundamental": _definition(
        "fundamental", allowed_inputs=("evidence_snapshot", "public_market_facts", "financial_evidence", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("invent_missing_financial_fields",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "unknown"),
    ),
    "market_structure": _definition(
        "market_structure", allowed_inputs=("evidence_snapshot", "public_market_facts", "price_structure", "market_breadth", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("turn_one_quote_into_market_direction",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "news_event": _definition(
        "news_event", allowed_inputs=("evidence_snapshot", "public_market_facts", "news_evidence", "event_timeline"),
        forbidden_actions=_COMMON_FORBIDDEN + ("promote_unverified_news_to_fact",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "propagation_sentiment": _definition(
        "propagation_sentiment", allowed_inputs=("evidence_snapshot", "public_market_facts", "propagation_evidence", "sentiment_proxies"),
        forbidden_actions=_COMMON_FORBIDDEN + ("infer_truth_from_propagation",),
        output_kinds=("claim", "unknown", "risk"), effects=("evidence_only", "support", "oppose", "unknown"),
    ),
    "bull": _definition(
        "bull", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("suppress_counterevidence", "publish_a_trade_instruction"),
        output_kinds=("claim", "reasoning", "risk", "unknown"), effects=("support", "unknown"),
    ),
    "bear": _definition(
        "bear", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "quantresearch_readonly"),
        forbidden_actions=_COMMON_FORBIDDEN + ("suppress_supporting_evidence", "publish_a_trade_instruction"),
        output_kinds=("claim", "reasoning", "risk", "unknown"), effects=("oppose", "unknown"),
    ),
    "risk": _definition(
        "risk", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "portfolio_snapshot_readonly", "risk_policy"),
        forbidden_actions=_COMMON_FORBIDDEN + ("change_risk_policy", "approve_missing_facts"),
        output_kinds=("risk", "unknown", "claim"), effects=("block", "oppose", "support", "unknown"), may_block=True,
    ),
    "coordinator": _definition(
        "coordinator", allowed_inputs=("evidence_snapshot", "public_evidence", "role_outputs", "risk_policy", "h0_frozen"),
        forbidden_actions=_COMMON_FORBIDDEN + ("rewrite_role_artifacts", "hide_unresolved_conflict"),
        output_kinds=("conclusion", "claim", "reasoning", "risk", "unknown"), effects=("coordinate", "block", "support", "oppose", "unknown"), may_block=True,
    ),
}


def role_definition(role: str) -> dict[str, Any]:
    try:
        return copy.deepcopy(ROLE_SPECS[role])
    except KeyError as exc:
        raise ValueError(f"unsupported AgentRoleSpec role: {role}") from exc


def _validate_common_identity(value: dict[str, Any], *, kind: str) -> None:
    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        raise ValueError(f"unsupported AgentRoleSpec {kind}")
    if value.get("version") != VERSION or value.get("visibility") != INTERNAL_VISIBILITY:
        raise ValueError(f"invalid AgentRoleSpec {kind} identity")
    role = value.get("role")
    if role not in ROLE_SPECS:
        raise ValueError("invalid AgentRoleSpec role")
    stage = value.get("stage")
    if stage not in STAGES:
        raise ValueError("invalid AgentRoleSpec stage")
    _walk_keys(value)


def validate_input(value: dict[str, Any]) -> None:
    _validate_common_identity(value, kind="input")
    required = {"contract", "version", "role", "stage", "visibility", "agent_contract_sha256", "input_refs", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentRoleSpec input missing: " + ", ".join(missing))
    unknown_fields = sorted(set(value) - required)
    if unknown_fields:
        raise ValueError("AgentRoleSpec input contains unsupported fields: " + ", ".join(unknown_fields))
    if not isinstance(value["agent_contract_sha256"], str) or not value["agent_contract_sha256"]:
        raise ValueError("AgentRoleSpec input requires AgentContractSpec provenance")
    refs = value["input_refs"]
    if not isinstance(refs, dict):
        raise TypeError("AgentRoleSpec input_refs must be an object")
    allowed = set(ROLE_SPECS[value["role"]]["allowed_inputs"])
    unknown = sorted(set(refs) - allowed)
    if unknown:
        raise ValueError(f"AgentRoleSpec input contains disallowed inputs: {', '.join(unknown)}")
    for key, rows in refs.items():
        _refs(rows, f"input_refs.{key}")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("AgentRoleSpec roles are read-only")
    if not isinstance(value["provenance"], dict) or not str(value["provenance"].get("as_of") or ""):
        raise ValueError("AgentRoleSpec input requires provenance.as_of")
    if value["stage"] == "m1_research":
        forbidden = _find_forbidden(value, _M1_FORBIDDEN_KEYS)
        if forbidden:
            raise ValueError(f"M1 role input exposes forbidden context: {forbidden}")


def validate_output(value: dict[str, Any]) -> None:
    _validate_common_identity(value, kind="output")
    required = {"contract", "version", "role", "stage", "visibility", "status", "decision_effect", "propositions", "evidence_refs", "counterevidence_refs", "risks", "unknowns", "provenance", "permissions"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError("AgentRoleSpec output missing: " + ", ".join(missing))
    unknown_fields = sorted(set(value) - required)
    if unknown_fields:
        raise ValueError("AgentRoleSpec output contains unsupported fields: " + ", ".join(unknown_fields))
    if value["status"] not in STATUSES:
        raise ValueError("invalid AgentRoleSpec status")
    definition = ROLE_SPECS[value["role"]]
    if value["decision_effect"] not in definition["allowed_effects"]:
        raise ValueError("AgentRoleSpec decision effect is not allowed for role")
    if value["role"] == "coordinator":
        expected_effect = _COORDINATOR_EFFECTS[value["status"]]
        if value["decision_effect"] != expected_effect:
            raise ValueError("AgentRoleSpec coordinator status and decision_effect disagree")
        attempt_id = value.get("provenance", {}).get("attempt_id") if isinstance(value.get("provenance"), dict) else None
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ValueError("AgentRoleSpec coordinator output requires provenance.attempt_id")
    if not isinstance(value["permissions"], dict) or value["permissions"].get("write_permissions") != []:
        raise ValueError("AgentRoleSpec outputs are read-only")
    coordinator_spec = value.get("provenance", {}).get("coordinator_spec") if isinstance(value.get("provenance"), dict) else None
    if coordinator_spec is not None:
        if not isinstance(coordinator_spec, dict) or coordinator_spec.get("contract") != COORDINATOR_CONTRACT:
            raise ValueError("invalid CoordinatorSpec provenance")
        graph = validate_dependency_graph(coordinator_spec.get("dependency_graph"))
        states = coordinator_spec.get("states", coordinator_spec.get("dependency_states"))
        if not isinstance(states, dict) or set(states) != set(graph):
            raise ValueError("CoordinatorSpec states do not match dependency graph")
        frontier = coordinator_frontier(graph, states)
        if coordinator_spec.get("frontier", coordinator_spec.get("executable_frontier")) != frontier:
            raise ValueError("CoordinatorSpec executable frontier is stale")
        lifecycles = coordinator_spec.get("lifecycles")
        if not isinstance(lifecycles, dict) or set(lifecycles) != set(graph):
            raise ValueError("CoordinatorSpec lifecycles must cover every dependency node")
        for node, lifecycle in lifecycles.items():
            if (not isinstance(lifecycle, dict)
                    or lifecycle.get("node_id") != node
                    or lifecycle.get("status") != states[node]
                    or not isinstance(lifecycle.get("records"), list)):
                raise ValueError(f"CoordinatorSpec lifecycle is invalid for {node}")
    for field in ("evidence_refs", "counterevidence_refs"):
        _refs(value[field], field)
    for field in ("propositions", "risks", "unknowns"):
        if not isinstance(value[field], list) or any(not isinstance(row, dict) for row in value[field]):
            raise ValueError(f"AgentRoleSpec {field} must contain structured objects")
    proposition_ids: set[str] = set()
    for proposition in value["propositions"]:
        identifier = str(proposition.get("id") or "")
        kind = str(proposition.get("kind") or "")
        if not identifier or identifier in proposition_ids or kind not in definition["output_kinds"]:
            raise ValueError("AgentRoleSpec propositions require unique ids and an allowed kind")
        proposition_ids.add(identifier)
        _refs(proposition.get("evidence_refs"), "proposition.evidence_refs")
        _refs(proposition.get("counterevidence_refs"), "proposition.counterevidence_refs")
        if kind in {"claim", "conclusion"} and not proposition["evidence_refs"]:
            raise ValueError(f"AgentRoleSpec proposition {identifier} must cite evidence")
    if not isinstance(value["provenance"], dict) or not str(value["provenance"].get("input_sha256") or ""):
        raise ValueError("AgentRoleSpec output requires input provenance")
    if any(len(text) > 4_000 for text in _strings(value)):
        raise ValueError("AgentRoleSpec contains an unbounded narrative field")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def build_input(
    agent_contract: dict[str, Any],
    *,
    role: str,
    stage: str,
    input_refs: dict[str, list[str]],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    validate_agent_input(agent_contract)
    if agent_contract.get("contract") != AGENT_CONTRACT or agent_contract.get("version") != AGENT_CONTRACT_VERSION:
        raise ValueError("AgentRoleSpec requires AgentContractSpec/v1 input")
    definition = role_definition(role)
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role,
        "stage": stage,
        "visibility": INTERNAL_VISIBILITY,
        "agent_contract_sha256": sha256(agent_contract),
        "input_refs": copy.deepcopy(input_refs),
        "provenance": {**copy.deepcopy(provenance), "as_of": provenance.get("as_of") or agent_contract["controlled_context"]["as_of"]},
        "permissions": {"write_permissions": [], "read_permissions": list(definition["allowed_inputs"])},
    }
    validate_input(value)
    return value


def build_output(
    role_input: dict[str, Any],
    *,
    status: str,
    decision_effect: str,
    propositions: list[dict[str, Any]] | None = None,
    evidence_refs: list[str] | None = None,
    counterevidence_refs: list[str] | None = None,
    risks: list[dict[str, Any]] | None = None,
    unknowns: list[dict[str, Any]] | None = None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    validate_input(role_input)
    value = {
        "contract": CONTRACT,
        "version": VERSION,
        "role": role_input["role"],
        "stage": role_input["stage"],
        "visibility": INTERNAL_VISIBILITY,
        "status": status,
        "decision_effect": decision_effect,
        "propositions": copy.deepcopy(propositions or []),
        "evidence_refs": sorted(set(evidence_refs or [])),
        "counterevidence_refs": sorted(set(counterevidence_refs or [])),
        "risks": copy.deepcopy(risks or []),
        "unknowns": copy.deepcopy(unknowns or []),
        "provenance": {**copy.deepcopy(provenance or {}), "input_sha256": sha256(role_input)},
        "permissions": {"write_permissions": [], "read_permissions": list(ROLE_SPECS[role_input["role"]]["allowed_inputs"])},
    }
    validate_output(value)
    return value


def build_role_inputs(agent_contract: dict[str, Any], *, stage: str, input_refs: dict[str, list[str]], provenance: dict[str, Any]) -> list[dict[str, Any]]:
    """Create the deterministic internal roster for a stage.

    The roster contains references, not copied private facts.  M1 therefore
    cannot accidentally inherit H0 through a role bundle.
    """
    if stage not in STAGES:
        raise ValueError("unsupported AgentRoleSpec stage")
    roles: list[dict[str, Any]] = []
    for role in STAGE_ROLES[stage]:
        refs = {key: list(values) for key, values in input_refs.items() if key in ROLE_SPECS[role]["allowed_inputs"]}
        if stage != "m2":
            refs.pop("h0_frozen", None)
        roles.append(build_input(agent_contract, role=role, stage=stage, input_refs=refs, provenance=provenance))
    return roles


def attach_role_inputs(packet: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """Attach the internal roster to a research packet and re-hash it.

    Only stable references are attached.  In particular this function does
    not copy H0 or any MemoryHub/portfolio payload into a role input.
    """
    value = copy.deepcopy(packet)
    agent_contract = value.get("agent_contract")
    if not isinstance(agent_contract, dict):
        raise TypeError("AgentRoleSpec requires an attached AgentContractSpec")
    refs: dict[str, list[str]] = {
        "evidence_snapshot": [str((agent_contract.get("evidence_snapshot") or {}).get("snapshot_id") or "snapshot:pending")],
        "public_evidence": [str(value.get("evidence_bundle_sha256") or value.get("sha256") or "packet:pending")],
        "public_market_facts": ["packet:public_market_facts"],
        "financial_evidence": ["packet:financial_evidence"],
        "news_evidence": ["packet:news_evidence"],
        "event_timeline": ["packet:event_timeline"],
        "propagation_evidence": ["packet:propagation_evidence"],
        "sentiment_proxies": ["packet:sentiment_proxies"],
        "price_structure": ["packet:price_structure"],
        "market_breadth": ["packet:market_breadth"],
        "quantresearch_readonly": ["packet:quantresearch_readonly"],
        "role_outputs": ["roles:pending"],
        "risk_policy": ["runtime:risk-policy-v1"],
    }
    if stage == "m2":
        refs["h0_frozen"] = ["cycle:h0-frozen"]
    value["agent_role_inputs"] = build_role_inputs(
        agent_contract, stage=stage, input_refs=refs,
        provenance={
            "cycle_id": value.get("cycle_id"), "attempt_id": value.get("attempt_id"),
            "as_of": agent_contract["controlled_context"]["as_of"],
        },
    )
    value.pop("sha256", None)
    value["sha256"] = sha256(value)
    return value


def internal_artifact(value: dict[str, Any]) -> dict[str, Any]:
    """Return a copy suitable for audit metadata; never a user-facing payload."""
    if value.get("visibility") != INTERNAL_VISIBILITY:
        raise ValueError("AgentRoleSpec artifact must remain internal")
    if value.get("contract") == CONTRACT:
        validate_output(value)
    else:
        validate_input(value)
    return copy.deepcopy(value)


def build_runtime_coordinator_output(
    role_inputs: list[dict[str, Any]],
    *,
    status: str,
    evidence_refs: list[str],
    unknowns: list[str],
    attempt_id: str,
    bundle_sha256: str | None,
    dependency_graph: dict[str, list[str] | tuple[str, ...]] | None = None,
    dependency_states: dict[str, str] | None = None,
    node_id: str = "coordinator",
    state_store: CoordinatorStateStore | None = None,
    lease_seconds: float = 300,
    now: float | None = None,
    idempotency_key: str | None = None,
    claim_already_held: bool = False,
    execution_generation: int | None = None,
) -> dict[str, Any]:
    """Close one runtime research attempt with a bounded coordinator artifact.

    Providers may return their domain-specific result shape, so the runtime
    records a deterministic coordinator envelope even when no provider-side
    role output was emitted.  This is a qualification/status artifact, not a
    replacement for the provider result and never a user-facing persona.
    """
    coordinators = [item for item in role_inputs if isinstance(item, dict) and item.get("role") == "coordinator"]
    if not coordinators:
        raise ValueError("AgentRoleSpec coordinator input is missing")
    if len(coordinators) != 1:
        raise ValueError("AgentRoleSpec coordinator input is ambiguous")
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        raise ValueError("AgentRoleSpec coordinator output requires attempt_id")
    if bundle_sha256 is not None and (not isinstance(bundle_sha256, str) or not bundle_sha256.strip()):
        raise ValueError("AgentRoleSpec coordinator bundle_sha256 must be non-empty when provided")

    coordinator = coordinators[0]
    validate_input(coordinator)
    if status not in _COORDINATOR_EFFECTS:
        raise ValueError("invalid AgentRoleSpec coordinator status")
    graph = dependency_graph or {node_id: []}
    canonical_graph = validate_dependency_graph(graph)
    states = {node: "pending" for node in canonical_graph}
    states.update({str(key): str(value) for key, value in (dependency_states or {}).items()})
    states.setdefault(node_id, status)
    if node_id not in canonical_graph:
        raise ValueError("CoordinatorSpec node_id is missing from dependency graph")
    if any(value not in STATUSES for value in states.values()):
        raise ValueError("CoordinatorSpec states contain an invalid lifecycle status")
    dependencies = canonical_graph[node_id]
    if status == "succeeded" and any(states.get(dependency) != "succeeded" for dependency in dependencies):
        # A coordinator must never report successful work while a declared
        # prerequisite is pending or failed.  This is the runtime stop gate
        # for downstream SPEC nodes.
        status = "blocked"
    states[node_id] = status
    frontier = coordinator_frontier(graph, states)
    durable_idempotency_key = idempotency_key or (
        f"{attempt_id}:{node_id}:{sha256({ 'status': status, 'evidence_refs': sorted(set(evidence_refs)), 'unknowns': sorted(map(str, unknowns)) })}"
    )
    lifecycle = CoordinatorLifecycle(node_id, status="pending")
    lifecycle_at = now if now is not None else 0.0
    if status != "pending":
        lifecycle.transition("running", at=lifecycle_at, reason="runtime coordinator started")
        lifecycle.transition(status, at=lifecycle_at, reason="runtime coordinator result")
    recovery: dict[str, Any] = {"persisted": False, "duplicate": False, "takeover": False}
    lifecycles = {
        node: CoordinatorLifecycle(node, status=states[node]).as_dict()
        for node in canonical_graph
    }
    if state_store is not None:
        if claim_already_held:
            lifecycles = {}
            for node in canonical_graph:
                record = state_store.get(node)
                if record is None:
                    raise ValueError("CoordinatorSpec runtime graph state is missing a claimed node")
                lifecycles[node] = CoordinatorLifecycle(
                    node,
                    status=str(record.get("status") or states[node]),
                    records=record.get("lifecycle"),
                ).as_dict()
            claim = state_store.get(node_id)
        else:
            lifecycles = state_store.snapshot_graph(canonical_graph, states, now=now)
            claim = state_store.claim(node_id, durable_idempotency_key, lease_seconds=lease_seconds, now=now)
        if claim_already_held:
            if not claim or claim.get("idempotency_key") != durable_idempotency_key or claim.get("status") != "running":
                raise ValueError("CoordinatorSpec runtime claim is no longer active")
            if execution_generation is None or claim.get("execution_generation") != execution_generation:
                raise ValueError("CoordinatorSpec runtime claim generation is no longer active")
            claim = {**claim, "duplicate": False, "takeover": False}
        recovery = {key: claim.get(key) for key in ("persisted", "duplicate", "takeover") if key in claim}
        recovery.update({
            "persisted": True,
            "execution_count": claim.get("execution_count"),
            "execution_generation": claim.get("execution_generation"),
        })
        if not claim.get("duplicate"):
            finished = state_store.finish(
                node_id,
                status,
                idempotency_key=durable_idempotency_key,
                execution_generation=claim.get("execution_generation"),
                reason="runtime coordinator result",
                now=now,
            )
            lifecycle = CoordinatorLifecycle(node_id, status=status, records=finished.get("lifecycle"))
        else:
            lifecycle = CoordinatorLifecycle(node_id, status=str(claim.get("status") or status), records=claim.get("lifecycle"))
            status = str(claim.get("status") or status)
        lifecycles[node_id] = lifecycle.as_dict()
    return build_output(
        coordinator,
        status=status,
        decision_effect=_COORDINATOR_EFFECTS[status],
        evidence_refs=evidence_refs,
        unknowns=[{"description": str(item)} for item in unknowns],
        provenance={
            "attempt_id": attempt_id, "bundle_sha256": bundle_sha256,
            "coordinator_spec": {
                "contract": COORDINATOR_CONTRACT,
                "version": COORDINATOR_VERSION,
                "dependency_graph": canonical_graph,
                "states": states,
                "frontier": frontier,
                "lifecycles": lifecycles,
            },
            "coordinator_recovery": recovery,
        },
    )


def frozen_replay(role_input: dict[str, Any], role_output: dict[str, Any]) -> dict[str, Any]:
    """Re-qualify a frozen role artifact without changing either source value."""
    validate_input(role_input)
    validate_output(role_output)
    if role_input["role"] != role_output["role"] or role_input["stage"] != role_output["stage"]:
        raise ValueError("AgentRoleSpec replay identity mismatch")
    input_hash = sha256(role_input)
    output_hash = sha256(role_output)
    if role_output["provenance"].get("input_sha256") != input_hash:
        raise ValueError("AgentRoleSpec replay provenance mismatch")
    return {
        "contract": REPLAY_CONTRACT,
        "version": VERSION,
        "role": role_input["role"],
        "stage": role_input["stage"],
        "source_input_sha256": input_hash,
        "source_output_sha256": output_hash,
        "qualification": {
            "valid": True,
            "status": role_output["status"],
            "decision_effect": role_output["decision_effect"],
            "evidence_refs": list(role_output["evidence_refs"]),
            "counterevidence_refs": list(role_output["counterevidence_refs"]),
        },
        "evaluation_vector": {
            "delivery_speed": {"state": "not_measured_in_frozen_replay"},
            "qualification_probability": {"state": "not_estimated_in_frozen_replay"},
            "research_quality": {"structured": True, "evidence_ref_count": len(role_output["evidence_refs"])},
            "judgment_outcome": {"status": role_output["status"], "effect": role_output["decision_effect"]},
            "safety_reliability": {
                "read_only": role_output["permissions"]["write_permissions"] == [],
                "internal_visibility": role_output["visibility"] == INTERNAL_VISIBILITY,
                "m1_h0_blind": role_output["stage"] != "m1_research" or _find_forbidden(role_input, _M1_FORBIDDEN_KEYS) is None,
            },
        },
    }


def install_qualification() -> dict[str, Any]:
    """Run two deterministic, source-independent frozen replays."""
    from .agent_contract import build_input as build_agent_input

    packet = {
        "cycle_id": "agent-role-install", "stage": "m1_research", "as_of": "2026-01-01T00:00:00Z",
        "evidence_snapshot": {"contract": "evidence-snapshot-spec/v1", "snapshot_id": "install-snapshot", "as_of": "2026-01-01T00:00:00Z", "content_hash": "install-evidence"},
        "memories": [], "sha256": "install-packet",
    }
    agent_input = build_agent_input(packet, capability="research:m1_research")
    role_input = build_input(
        agent_input, role="coordinator", stage="m1_research",
        input_refs={"evidence_snapshot": ["install-snapshot"], "public_evidence": ["install-evidence"], "role_outputs": ["roles:install"]},
        provenance={"cycle_id": packet["cycle_id"], "as_of": packet["as_of"]},
    )
    role_output = build_output(
        role_input, status="succeeded", decision_effect="coordinate",
        evidence_refs=["install-evidence"], provenance={"attempt_id": "install-attempt"},
    )
    first = frozen_replay(role_input, role_output)
    second = frozen_replay(copy.deepcopy(role_input), copy.deepcopy(role_output))
    return {
        "contract": "AgentRoleInstallQualification/v1",
        "qualified": first == second,
        "replay_sha256": sha256(first),
        "source_input_sha256": first["source_input_sha256"],
        "source_output_sha256": first["source_output_sha256"],
        "evaluation_vector": first["evaluation_vector"],
    }


if __name__ == "__main__":
    print(canonical_json(install_qualification()))
