"""Read-only frozen cycle receipts; never rerun a model or publish history."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from typing import Any

from .cycle_contract import SPEC_VERSION, validate_m1_blind_packet
from .decision_cycle import assert_m1_blind, canonical_json
from .evidence_snapshot import validate_snapshot
from .store import CompanionStore
from .decision_cycle import cycle_contract
from .stage_expression import normalize_stage_output

CONTRACT = 'CompanionDecisionCycleReplay/v1'


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if instant.tzinfo is None:
        raise ValueError('cycle replay boundary must be timezone-aware')
    return instant


def freeze_cycle(store: CompanionStore, cycle_id: str) -> dict[str, Any]:
    """Capture persisted provenance, including missing historical inputs as missing."""
    with store.connection() as connection:
        connection.execute('BEGIN')
        cycle = store.get_cycle(cycle_id, connection=connection)
        source = {}
        # Fixed table names only; all facts come from one read transaction.
        for name, table, order in (
            ('stages', 'companion_stage_run', 'stage,attempt'),
            ('attempts', 'llm_attempt', 'started_at,attempt_number,attempt_id'),
            ('evidence_snapshots', 'evidence_snapshot', 'version,snapshot_id'),
            ('artifacts', 'narrative_artifact', 'sealed_at,artifact_id'),
            ('cycle_events', 'companion_cycle_event', 'sequence'),
            ('stage_events', 'companion_stage_event', 'created_at,event_id'),
            ('judgment_snapshots', 'judgment_snapshot', 'created_at,snapshot_id'),
            ('stage_checkpoints', 'stage_checkpoint', 'created_at'),
        ):
            source[name] = [dict(row) for row in connection.execute(
                f'SELECT * FROM {table} WHERE cycle_id=? ORDER BY {order}', (cycle_id,)
            )]
        source['cycle'] = cycle_contract(cycle, source.pop('stages'))
        source['cycle_record'] = cycle
        source['evidence_snapshots'] = [store._snapshot_from_row(row) for row in source['evidence_snapshots']]
    return {'contract': CONTRACT, 'source': source, 'source_sha256': _hash(source)}


def replay_cycle(frozen: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct recorded publication eligibility, not model wording or trading success."""
    if frozen.get('contract') != CONTRACT or _hash(frozen.get('source')) != frozen.get('source_sha256'):
        raise ValueError('cycle replay integrity mismatch')
    source = copy.deepcopy(frozen['source'])
    if source['cycle']['cycle_spec_version'] != SPEC_VERSION:
        raise ValueError('unsupported cycle SPEC version')
    for snapshot in source['evidence_snapshots']:
        validate_snapshot(snapshot)
    attempts = []
    for attempt in source['attempts']:
        packet = json.loads(attempt.get('input_packet_json') or 'null')
        verifier = json.loads(attempt.get('verifier_json') or '{}')
        if str(attempt['stage']).startswith('m1') and packet is not None:
            validate_m1_blind_packet(packet)
            # Replay the exact frozen boundary.  Human material sealed after
            # this attempt started was unavailable to the original M1 input
            # and must not change the historical leakage result.
            boundary = attempt.get('started_at') or attempt.get('as_of')
            human_texts = [
                a.get('body_markdown', '')
                for a in source['artifacts']
                if a.get('actor') == 'human'
                and (not boundary or not a.get('sealed_at')
                     or _instant(a['sealed_at']) <= _instant(boundary))
            ]
            assert_m1_blind(packet, human_texts=human_texts)
        persisted_hash = str(attempt.get('input_sha256') or '').strip()
        input_integrity = 'missing' if packet is None or not persisted_hash else 'verified'
        if isinstance(packet, dict) and 'sha256' in packet and persisted_hash and packet['sha256'] != persisted_hash:
            raise ValueError('cycle replay input integrity mismatch')
        if packet is not None and persisted_hash and _hash({k: v for k, v in packet.items() if k != 'sha256'}) != persisted_hash:
            # Old deterministic fallbacks retained the provider retry hash after
            # removing verification_repair. Report this frozen discrepancy; do
            # not repair history or confer current qualification on that input.
            if (attempt.get('runner_fingerprint') == 'runtime-safe-fallback/v1'
                    and attempt.get('routing_reason') == 'verified-stage-safe-fallback'
                    and verifier.get('fallback') is True):
                input_integrity = 'historical_fallback_hash_mismatch'
            else:
                raise ValueError('cycle replay input integrity mismatch')
        output = json.loads(attempt.get('output_json') or 'null')
        conclusion_qualified = None
        if attempt['stage'] == 'm1_judgment' and isinstance(output, dict):
            normalized = normalize_stage_output('m1_judgment', output)
            conclusion_qualified = normalized.qualified
            if normalized.snapshot.get('qualified') is False:
                conclusion_qualified = False
        attempts.append({
            'attempt_id': attempt['attempt_id'], 'status': attempt['status'],
            'qualified': attempt['status'] == 'succeeded' and verifier.get('passed') is True and input_integrity == 'verified' and output is not None and conclusion_qualified is not False,
            'input_integrity': input_integrity,
            'conclusion_qualified': conclusion_qualified,
            'historically_qualified': attempt['status'] == 'succeeded' and verifier.get('passed') is True,
            'input_reconstructable': packet is not None,
            'verifier': verifier,
        })
    for checkpoint in source['stage_checkpoints']:
        attempt = next((a for a in source['attempts'] if a['attempt_id'] == checkpoint['attempt_id']), None)
        if (not attempt or checkpoint['packet_sha256'] != attempt.get('input_sha256')
                or json.loads(checkpoint['output_json']) != json.loads(attempt.get('output_json') or 'null')
                or _hash(json.loads(checkpoint['output_json'])) != checkpoint['output_sha256']):
            raise ValueError('cycle replay checkpoint integrity mismatch')
    m1_attempts = [a for a in source['attempts'] if str(a['stage']).startswith('m1')]
    return {
        'contract': CONTRACT, 'source_sha256': frozen['source_sha256'],
        'source': source, 'qualification': {'attempts': attempts},
        'evaluation_vector': {
            'delivery_speed': {'state': 'recorded_attempt_durations', 'duration_ms': [a.get('duration_ms') for a in source['attempts']]},
            'qualification_probability': {'state': 'not_estimated_in_frozen_replay'},
            'research_quality': {'state': 'recorded_verifiers', 'verifiers': [a['verifier'] for a in attempts]},
            'judgment_outcome': {'state': 'not_observed'},
            'safety_reliability': {
                'read_only': True,
                'm1_h0_blind': bool(m1_attempts) and all(json.loads(a.get('input_packet_json') or 'null') is not None for a in m1_attempts),
                'inputs_complete': bool(attempts) and all(a['input_reconstructable'] for a in attempts),
            },
        },
    }
