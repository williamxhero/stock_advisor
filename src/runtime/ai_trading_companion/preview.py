"""Isolated rerun bundles and explicit, provider-free approval imports."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings_path
from .evidence_gate import EvidenceGate
from .router import CognitiveRouter
from .secret_guard import assert_safe
from .store import CompanionStore, digest, now


BUNDLE_SCHEMA_VERSION = 3
PREVIEW_SIGNING_KEY_FIELD = "signing_key"


def canonical_hash(value: dict[str, Any]) -> str:
    payload = {
        key: item for key, item in value.items()
        if key not in {"bundle_sha256", "bundle_hmac_sha256"}
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def preview_signing_key(*, create: bool, home: Path | None = None) -> str:
    """Read the preview signer from the runtime-local settings file.

    Preview approval must work from a copied, isolated runtime home.  Keeping the
    signer in the same local configuration also removes the Windows Credential
    Manager dependency without ever returning the value through a UI, audit, or
    bundle payload.
    """
    configured_home = os.environ.get("AI_TRADING_COMPANION_HOME")
    if home is None and configured_home:
        home = Path(configured_home)
    elif home is None:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is required")
        home = Path(local_app_data) / "AITradingCompanion"
    path = settings_path(home)
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
    key = str(preview.get(PREVIEW_SIGNING_KEY_FIELD) or "").strip()
    if key:
        return key
    if not create:
        raise ValueError("preview signing key is unavailable from local settings")
    key = secrets.token_hex(32)
    data["preview"] = {**preview, PREVIEW_SIGNING_KEY_FIELD: key}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return key


def seal_bundle(bundle: dict[str, Any], signing_key: str | None = None) -> dict[str, Any]:
    bundle.pop("bundle_sha256", None)
    bundle.pop("bundle_hmac_sha256", None)
    bundle["bundle_sha256"] = canonical_hash(bundle)
    key = signing_key or preview_signing_key(create=True)
    bundle["bundle_hmac_sha256"] = hmac.new(
        key.encode("utf-8"), bundle["bundle_sha256"].encode("ascii"), hashlib.sha256,
    ).hexdigest()
    return bundle


def source_fingerprint(store: CompanionStore, cycle_id: str, cutoff: str, *, connection: sqlite3.Connection | None = None) -> str:
    def collect(current: sqlite3.Connection) -> str:
        cycle = store.get_cycle(cycle_id, connection=current)
        artifacts = [dict(row) for row in current.execute(
            """SELECT artifact_id,kind,revision,actor,body_sha256,as_of,occurred_at,known_at
               FROM narrative_artifact WHERE cycle_id=? AND known_at<=? ORDER BY sealed_at,kind,revision""",
            (cycle_id, cutoff),
        )]
        manifest = {
            "cycle": {key: cycle.get(key) for key in (
            "cycle_id", "task_key", "scheduled_for", "schedule_id", "schedule_revision", "created_at",
                "as_of", "state", "revision", "has_h0", "updated_at",
            )},
            "artifacts": artifacts,
        }
        return canonical_hash(manifest)
    if connection is not None:
        return collect(connection)
    with store.connection() as current:
        return collect(current)


def find_source_cycle(database: Path, trading_date: str, task_key: str = "daily.review.1520") -> dict[str, Any]:
    uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """SELECT c.* FROM companion_cycle c
               JOIN companion_schedule_claim claim ON claim.cycle_id=c.cycle_id
               WHERE c.task_key=? AND substr(c.scheduled_for,1,10)=?
               ORDER BY c.created_at LIMIT 1""",
            (task_key, trading_date),
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise ValueError(f"no original {task_key} cycle exists for {trading_date}")
    return dict(row)


def prepare_preview_home(product_home: Path, database: Path, preview_id: str) -> tuple[Path, Path]:
    root = product_home / "runtime" / "previews" / preview_id
    if root.exists():
        raise ValueError(f"preview already exists: {preview_id}")
    work = root / "work"
    (work / "data").mkdir(parents=True)
    source = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    destination = sqlite3.connect(work / "data" / "trading-companion.sqlite3")
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    for relative in ("config", "workspace"):
        source_path = product_home / relative
        if source_path.exists():
            shutil.copytree(source_path, work / relative, dirs_exist_ok=True)
    return root, work


def launch_preview(
    product_home: Path,
    database: Path,
    install_root: Path,
    source_cycle_id: str,
    preview_id: str,
    known_at: str,
) -> dict[str, Any]:
    root, work = prepare_preview_home(product_home, database, preview_id)
    env = os.environ.copy()
    env.update({
        "AI_TRADING_COMPANION_HOME": str(work),
        "AI_TRADING_COMPANION_DATABASE": str(work / "data" / "trading-companion.sqlite3"),
        "AI_TRADING_COMPANION_RUNTIME": str(work / "runtime"),
        "AI_TRADING_COMPANION_INSTALL_ROOT": str(install_root),
    })
    command = [
        sys.executable, "-m", "ai_trading_companion", "_preview-worker",
        "--source-cycle-id", source_cycle_id, "--preview-id", preview_id,
        "--known-at", known_at, "--bundle-path", str(root / "bundle.json"),
    ]
    completed = subprocess.run(command, env=env, cwd=install_root, capture_output=True, text=True, timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "preview worker failed")[-4000:])
    bundle = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    # The worker seals the bundle with the signer copied into its isolated
    # runtime home.  Verify with that same signer; the formal runtime must not
    # need a signing key merely to inspect an isolated preview.
    verify_bundle(bundle, signing_key=preview_signing_key(create=False, home=work))
    shutil.rmtree(work, ignore_errors=True)
    return bundle


def build_bundle(
    store: CompanionStore,
    preview_cycle_id: str,
    source_cycle_id: str,
    preview_id: str,
    known_at: str,
) -> dict[str, Any]:
    cycle = store.get_cycle(preview_cycle_id)
    source = store.get_cycle(source_cycle_id)
    artifacts = store.artifacts(preview_cycle_id)
    attempts = store.attempts(preview_cycle_id)
    for attempt in attempts:
        for key in ("usage_json", "verifier_json", "broker_attempts_json", "tool_trace_json", "input_packet_json", "output_json"):
            attempt[key.removesuffix("_json")] = json.loads(attempt.get(key) or ("[]" if key in {"tool_trace_json", "broker_attempts_json"} else "{}"))
    with store.connection() as connection:
        snapshots = [dict(row) for row in connection.execute(
            "SELECT * FROM judgment_snapshot WHERE cycle_id=? ORDER BY created_at", (preview_cycle_id,),
        )]
        checkpoints = [dict(row) for row in connection.execute(
            "SELECT * FROM stage_checkpoint WHERE cycle_id=? ORDER BY created_at", (preview_cycle_id,),
        )]
        ledger = [dict(row) for row in connection.execute(
            "SELECT * FROM evidence_ledger_entry WHERE cycle_id=? ORDER BY known_at,evidence_id", (preview_cycle_id,),
        )]
    for checkpoint in checkpoints:
        checkpoint["output"] = json.loads(checkpoint.pop("output_json"))
    for snapshot in snapshots:
        snapshot["snapshot"] = json.loads(snapshot.pop("snapshot_json"))
    evidence = {}
    for artifact in artifacts:
        if artifact["kind"] in {"evidence", "m1_evidence"}:
            evidence[artifact["kind"]] = json.loads(artifact["body_markdown"])
    evidence_coverage = []
    for attempt in attempts:
        if attempt.get("stage") not in {"m0_research", "m1_research"}:
            continue
        output = attempt.get("output") or {}
        evidence_coverage.append({
            "stage": attempt.get("stage"),
            "packet_sha256": attempt.get("input_sha256"),
            "qualified": bool((attempt.get("verifier") or {}).get("passed")),
            "coverage": output.get("coverage") or [],
            "critical_gaps": output.get("critical_gaps") or [],
            "source_count": len(output.get("sources") or []),
        })
    report_body = {
        item["kind"]: item["body_markdown"] for item in artifacts
        if item["kind"] in {"m0", "m1", "m2", "stage_failure"}
    }
    bundle: dict[str, Any] = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "preview_id": preview_id,
        "source_cycle_id": source_cycle_id,
        "source_task_key": source["task_key"],
        "source_scheduled_for": source["scheduled_for"],
        "source_fingerprint": source_fingerprint(store, source_cycle_id, known_at),
        "preview_cycle_id": preview_cycle_id,
        "task_key": cycle["task_key"],
        "scheduled_for": cycle["scheduled_for"],
        "known_at": known_at,
        "replay_mode": "original_cycle_inputs",
        "qualification_version": 2,
        "cycle_state": cycle["state"],
        "preview_status": "passed" if {"evidence", "m0", "m1_evidence", "m1"}.issubset({item["kind"] for item in artifacts}) else "failed",
        "schedule_snapshot": json.loads(cycle.get("schedule_snapshot_json") or "{}"),
        "artifacts": artifacts,
        "attempts": attempts,
        "evidence_coverage": evidence_coverage,
        "report_body": report_body,
        "stage_checkpoints": checkpoints,
        "evidence_ledger": ledger,
        "judgment_snapshots": snapshots,
        "evidence": evidence,
    }
    assert_safe(json.dumps(bundle, ensure_ascii=False), boundary="preview bundle")
    return seal_bundle(bundle)


def write_bundle(bundle: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _packet_hash(packet: dict[str, Any]) -> str:
    payload = {key: value for key, value in packet.items() if key != "sha256"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return digest(raw)


def verify_bundle(
    bundle: dict[str, Any], *, require_qualified: bool = False,
    signing_key: str | None = None,
) -> None:
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ValueError("unsupported preview bundle schema")
    if bundle.get("bundle_sha256") != canonical_hash(bundle):
        raise ValueError("preview bundle hash mismatch")
    key = signing_key or preview_signing_key(create=False)
    expected_signature = hmac.new(
        key.encode("utf-8"), str(bundle.get("bundle_sha256") or "").encode("ascii"), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(str(bundle.get("bundle_hmac_sha256") or ""), expected_signature):
        raise ValueError("preview bundle signature mismatch")
    assert_safe(json.dumps(bundle, ensure_ascii=False), boundary="preview approval")
    artifacts = bundle.get("artifacts") or []
    for artifact in artifacts:
        if artifact.get("cycle_id") != bundle.get("preview_cycle_id"):
            raise ValueError("preview artifact belongs to another cycle")
        if artifact.get("body_sha256") != digest(str(artifact.get("body_markdown") or "")):
            raise ValueError(f"preview artifact hash mismatch: {artifact.get('kind')}")
    if not require_qualified:
        return
    if bundle.get("preview_status") != "passed":
        raise ValueError("failed preview cannot be approved")
    required = {artifact["kind"] for artifact in artifacts}
    if not {"evidence", "m0", "m1_evidence", "m1"}.issubset(required):
        raise ValueError("preview bundle is incomplete")
    attempts = {attempt["attempt_id"]: attempt for attempt in bundle.get("attempts") or []}
    successful_stages = {
        attempt.get("stage") for attempt in attempts.values()
        if attempt.get("status") == "succeeded" and (attempt.get("verifier") or {}).get("passed")
    }
    required_stages = {"m0_research", "m0_compose", "m1_research", "m1_judgment"}
    if any(artifact["kind"] == "m2" for artifact in bundle.get("artifacts") or []):
        required_stages.add("m2")
    if not required_stages.issubset(successful_stages):
        raise ValueError(f"preview lacks qualified attempts: {sorted(required_stages - successful_stages)}")
    checkpoints = {checkpoint["stage"]: checkpoint for checkpoint in bundle.get("stage_checkpoints") or []}
    if not required_stages.issubset(checkpoints):
        raise ValueError(f"preview lacks required checkpoints: {sorted(required_stages - checkpoints.keys())}")
    for stage in required_stages:
        checkpoint = checkpoints[stage]
        attempt = attempts.get(checkpoint.get("attempt_id"))
        if not attempt or attempt.get("stage") != stage:
            raise ValueError(f"checkpoint attempt mismatch: {stage}")
        if checkpoint.get("packet_sha256") != attempt.get("input_sha256"):
            raise ValueError(f"checkpoint packet mismatch: {stage}")
        input_packet = attempt.get("input_packet") or {}
        if input_packet.get("sha256") != attempt.get("input_sha256"):
            raise ValueError(f"attempt packet is not bound to input hash: {stage}")
        if _packet_hash(input_packet) != attempt.get("input_sha256"):
            raise ValueError(f"attempt packet content hash mismatch: {stage}")
        if checkpoint.get("output") != attempt.get("output"):
            raise ValueError(f"checkpoint output mismatch: {stage}")
        raw = json.dumps(checkpoint.get("output") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if checkpoint.get("output_sha256") != digest(raw):
            raise ValueError(f"checkpoint hash mismatch: {stage}")
        fresh_verifier = CognitiveRouter().verify(stage, input_packet, checkpoint.get("output") or {})
        if not fresh_verifier.get("passed"):
            raise ValueError(f"stage verifier rejected frozen output: {stage}")
        if stage in {"m0_research", "m1_research"}:
            evidence_verifier = EvidenceGate().evaluate(
                checkpoint.get("output") or {}, input_packet.get("evidence_requirements") or [],
                attempt.get("tool_trace") or [], str(input_packet.get("as_of") or ""),
            )
            if not evidence_verifier.get("passed"):
                raise ValueError(f"evidence gate rejected frozen output: {stage}")
    if "m2" in required_stages:
        m1_output = checkpoints["m1_judgment"]["output"]
        m1_snapshot = m1_output.get("snapshot") if isinstance(m1_output.get("snapshot"), dict) else {}
        if not bool(m1_output.get("judgment_qualified")) or not bool(m1_snapshot.get("qualified")):
            raise ValueError("M2 requires a qualified frozen M1 judgment")
    artifacts_by_kind = {artifact["kind"]: artifact for artifact in artifacts}
    bindings = {
        "evidence": ("m0_research", None), "m0": ("m0_compose", "m0_markdown"),
        "m1_evidence": ("m1_research", None), "m1": ("m1_judgment", "m1_markdown"),
        "m2": ("m2", "m2_markdown"),
    }
    for kind, (stage, field) in bindings.items():
        artifact = artifacts_by_kind.get(kind)
        if not artifact:
            continue
        output = checkpoints[stage]["output"]
        if field is None:
            try:
                body = json.loads(artifact["body_markdown"])
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid structured artifact: {kind}") from exc
            if body != output:
                raise ValueError(f"artifact is not bound to checkpoint: {kind}")
        elif artifact["body_markdown"] != output.get(field):
            raise ValueError(f"artifact is not bound to checkpoint: {kind}")


def approve_bundle(
    store: CompanionStore, bundle: dict[str, Any], *, signing_key: str | None = None,
) -> dict[str, Any]:
    """Import exact frozen outputs. This function never invokes the Broker."""
    verify_bundle(bundle, require_qualified=True, signing_key=signing_key)
    preview_id = str(bundle["preview_id"])
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT * FROM preview_import WHERE preview_id=?", (preview_id,)).fetchone()
        if existing:
            if existing["bundle_sha256"] != bundle["bundle_sha256"]:
                raise ValueError("preview id was already imported with another hash")
            return {"cycle": store.get_cycle(existing["cycle_id"], connection=connection), "already_imported": True}
        source = store.get_cycle(str(bundle["source_cycle_id"]), connection=connection)
        if bundle.get("source_task_key") != source["task_key"] or bundle.get("task_key") != source["task_key"]:
            raise ValueError("preview task identity does not match source cycle")
        if bundle.get("source_scheduled_for") != source["scheduled_for"] or bundle.get("scheduled_for") != source["scheduled_for"]:
            raise ValueError("preview schedule identity does not match source cycle")
        if bundle.get("source_fingerprint") != source_fingerprint(store, source["cycle_id"], bundle["known_at"], connection=connection):
            raise ValueError("source cycle changed or preview provenance is invalid")
        snapshot = json.loads(source.get("schedule_snapshot_json") or "{}")
        snapshot.update({
            "diagnostic_rerun": True, "diagnostic_rerun_of": source["cycle_id"],
            "preview_id": preview_id, "bundle_sha256": bundle["bundle_sha256"],
            "known_at": bundle["known_at"], "replay_mode": bundle["replay_mode"],
        })
        cycle_id = str(uuid.uuid4())
        at = now()
        has_h0 = any(item["kind"] == "h0" for item in bundle["artifacts"])
        has_m2 = any(item["kind"] == "m2" for item in bundle["artifacts"])
        state = "complete" if has_m2 or not has_h0 else "m2_deferred"
        connection.execute(
            """INSERT INTO companion_cycle(
                 cycle_id,task_key,scheduled_for,as_of,state,revision,schedule_id,schedule_revision,
                 schedule_snapshot_json,has_h0,m1_completed_at,m2_completed_at,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
            (cycle_id, source["task_key"], bundle["known_at"], bundle["known_at"], state,
             source.get("schedule_id"), source.get("schedule_revision"), json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
             int(has_h0), bundle["known_at"], bundle["known_at"] if has_m2 else None, at, at),
        )
        artifact_map: dict[str, str] = {}
        for original in bundle["artifacts"]:
            if original["kind"] not in {"evidence", "m0", "h0", "m1_evidence", "m1", "m2"}:
                continue
            metadata = json.loads(original.get("metadata_json") or "{}")
            metadata.update({"preview_id": preview_id, "bundle_sha256": bundle["bundle_sha256"], "preview_artifact_id": original["artifact_id"]})
            imported = store.append_artifact(
                cycle_id, original["kind"], original["actor"], original["body_markdown"], original["as_of"], metadata,
                occurred_at=original.get("occurred_at"), known_at=bundle["known_at"], connection=connection,
            )
            artifact_map[original["artifact_id"]] = imported["artifact_id"]
        for original in bundle.get("evidence_ledger") or []:
            evidence_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"preview-evidence|{preview_id}|{original['evidence_id']}"))
            connection.execute(
                """INSERT INTO evidence_ledger_entry(
                     evidence_id,trading_date,cycle_id,source_url,source_title,body_text,occurred_at,known_at,
                     metadata_json,stage,content_sha256,coverage_state)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (evidence_id, original["trading_date"], cycle_id, original.get("source_url"), original.get("source_title"),
                 original["body_text"], original.get("occurred_at"), bundle["known_at"], original.get("metadata_json") or "{}",
                 original.get("stage"), original.get("content_sha256"), original.get("coverage_state") or "observed"),
            )
        attempt_map: dict[str, str] = {}
        for original in bundle["attempts"]:
            new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"preview-attempt|{preview_id}|{original['attempt_id']}"))
            attempt_map[original["attempt_id"]] = new_id
            connection.execute(
                """INSERT INTO llm_attempt(
                     attempt_id,cycle_id,stage,attempt_number,status,as_of,started_at,completed_at,input_sha256,
                     output_sha256,error,model,reasoning_effort,search_enabled,timeout_seconds,routing_reason,
                     is_shadow,duration_ms,usage_json,input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,
                     verifier_json,runner_fingerprint,broker_provider,broker_intellect,broker_fulfilled_intellect,
                     broker_request_id,broker_cost_estimate,broker_attempts_json,tool_trace_json,input_packet_json,output_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id, cycle_id, original["stage"], original["attempt_number"], original["status"], original["as_of"],
                 original["started_at"], original["completed_at"], original.get("input_sha256"), original.get("output_sha256"),
                 original.get("error"), original.get("model"), original.get("reasoning_effort"), original.get("search_enabled"),
                 original.get("timeout_seconds"), original.get("routing_reason"), original.get("is_shadow", 0), original.get("duration_ms"),
                 json.dumps(original.get("usage") or {}, ensure_ascii=False, sort_keys=True), original.get("input_tokens"),
                 original.get("cached_input_tokens"), original.get("output_tokens"), original.get("reasoning_tokens"),
                 json.dumps(original.get("verifier") or {}, ensure_ascii=False, sort_keys=True), original.get("runner_fingerprint"),
                 original.get("broker_provider"), original.get("broker_intellect"), original.get("broker_fulfilled_intellect"),
                 original.get("broker_request_id"), original.get("broker_cost_estimate"), json.dumps(original.get("broker_attempts") or [], ensure_ascii=False, sort_keys=True),
                 json.dumps(original.get("tool_trace") or [], ensure_ascii=False, sort_keys=True),
                 json.dumps(original.get("input_packet") or {}, ensure_ascii=False, sort_keys=True),
                 json.dumps(original.get("output") or {}, ensure_ascii=False, sort_keys=True)),
            )
        for checkpoint in bundle.get("stage_checkpoints") or []:
            raw_output = json.dumps(checkpoint.get("output") or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """INSERT INTO stage_checkpoint(cycle_id,stage,packet_sha256,attempt_id,output_json,output_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (cycle_id, checkpoint["stage"], checkpoint["packet_sha256"], attempt_map[checkpoint["attempt_id"]],
                 raw_output, digest(raw_output), checkpoint.get("created_at") or at),
            )
        for original in bundle.get("judgment_snapshots") or []:
            imported_artifact_id = artifact_map.get(original.get("artifact_id"))
            if not imported_artifact_id:
                continue
            snapshot_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"preview-snapshot|{preview_id}|{original['snapshot_id']}"))
            connection.execute(
                """INSERT INTO judgment_snapshot(
                     snapshot_id,artifact_id,cycle_id,kind,snapshot_json,as_of,created_at,verification_status)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (snapshot_id, imported_artifact_id, cycle_id, original["kind"],
                 json.dumps(original.get("snapshot") or {}, ensure_ascii=False, sort_keys=True),
                 original["as_of"], original.get("created_at") or at, original.get("verification_status") or "unverified"),
            )
        connection.execute(
            "INSERT INTO preview_import(preview_id,bundle_sha256,cycle_id,imported_at) VALUES(?,?,?,?)",
            (preview_id, bundle["bundle_sha256"], cycle_id, at),
        )
        cycle = store.get_cycle(cycle_id, connection=connection)
        store.queue_event(
            cycle_id, "cycle.preview_approved", {
                "cycle": cycle, "preview_id": preview_id,
                "bundle_sha256": bundle["bundle_sha256"], "known_at": bundle["known_at"],
            }, connection=connection,
        )
    return {"cycle": cycle, "already_imported": False}
