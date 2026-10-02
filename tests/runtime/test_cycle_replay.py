import copy
import json

import pytest

from ai_trading_companion.cycle_replay import freeze_cycle, replay_cycle
from ai_trading_companion.engine import CompanionEngine
from ai_trading_companion.store import CompanionStore
from ai_trading_companion.__main__ import _save_safe_stage_fallback
from ai_trading_companion.broker_client import canonical_packet_hash


def test_recovery_fallback_replays_actual_packet_and_preserves_legacy_discrepancy(tmp_path):
    store = CompanionStore(tmp_path / 'cycle.sqlite3')
    cycle = CompanionEngine(store).start_cycle('daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z')
    packet = {'verification_repair': {'frozen_decision': {'direction': 'bullish'}}}
    packet['sha256'] = canonical_packet_hash(packet)
    _, attempt_id = _save_safe_stage_fallback(store, cycle, 'm1_judgment', packet, horizon='当前')
    frozen = freeze_cycle(store, cycle['cycle_id'])
    attempt = frozen['source']['attempts'][0]
    actual_packet = json.loads(attempt['input_packet_json'])
    expected_hash = canonical_packet_hash({k: v for k, v in actual_packet.items() if k != 'sha256'})
    assert attempt['input_sha256'] == actual_packet['sha256'] == expected_hash
    assert replay_cycle(frozen)['qualification']['attempts'][0]['input_integrity'] == 'verified'
    # Emulate the persisted pre-fix fallback, without migrating or rewriting it.
    with store.connection() as connection:
        actual_packet['sha256'] = packet['sha256']
        connection.execute('UPDATE llm_attempt SET input_sha256=?, input_packet_json=? WHERE attempt_id=?',
                           (packet['sha256'], json.dumps(actual_packet), attempt_id))
    legacy = freeze_cycle(store, cycle['cycle_id'])
    original = copy.deepcopy(legacy)
    result = replay_cycle(legacy)
    assert legacy == original
    receipt = result['qualification']['attempts'][0]
    assert receipt['input_integrity'] == 'historical_fallback_hash_mismatch'
    assert receipt['historically_qualified'] is True
    assert receipt['qualified'] is False
    assert result['source']['attempts'][0]['input_sha256'] == packet['sha256']


def test_frozen_cycle_reconstructs_actual_inputs_and_qualification_without_rewriting(tmp_path):
    store = CompanionStore(tmp_path / 'cycle.sqlite3')
    cycle = CompanionEngine(store).start_cycle(
        'daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z',
        schedule_revision=7,
    )
    packet = {'frozen_public_evidence': [], 'business_context': {'positions': []}}
    snapshot = store.create_evidence_snapshot(cycle['cycle_id'], {
        'schema_version': 3, 'as_of': cycle['as_of'], 'spoken_summary': 'Observed facts',
        'sources': [{'evidence_ref': 'ev-1', 'excerpt': 'Original evidence'}],
        'coverage': [], 'critical_gaps': [], 'conflicts': [], 'high_impact_events': [],
    }, as_of=cycle['as_of'], source_watermarks={'market': 'revision-7'})
    attempt = store.begin_attempt(cycle['cycle_id'], 'm1_judgment', cycle['as_of'],
                                  input_packet=packet, model='frozen-model', runner_fingerprint='prompt-v7')
    store.finish_attempt(attempt['attempt_id'], 'succeeded', output={'direction': 'wait'},
                         verifier={'passed': True})
    artifact = store.append_artifact(cycle['cycle_id'], 'm1', 'model', 'Original judgment', cycle['as_of'])
    frozen = freeze_cycle(store, cycle['cycle_id'])
    original = copy.deepcopy(frozen)
    first = replay_cycle(frozen)
    store.append_artifact(cycle['cycle_id'], 'judgment_revision', 'model', 'Later correction', cycle['as_of'])
    second = replay_cycle(copy.deepcopy(frozen))
    assert first == second
    assert frozen == original
    assert json.loads(frozen['source']['attempts'][0]['input_packet_json']) == packet
    assert frozen['source']['attempts'][0]['runner_fingerprint'] == 'prompt-v7'
    assert first['source']['evidence_snapshots'] == [snapshot]
    assert frozen['source']['artifacts'][0]['artifact_id'] == artifact['artifact_id']
    assert frozen['source']['artifacts'][0]['body_markdown'] == 'Original judgment'
    assert first['qualification']['attempts'][0]['qualified'] is True
    assert first['evaluation_vector']['judgment_outcome']['state'] == 'not_observed'
    assert store.latest_artifact(cycle['cycle_id'], 'm1')['body_markdown'] == 'Original judgment'


@pytest.mark.parametrize('status,verifier', [('failed', {}), ('succeeded', {'passed': False})])
def test_failed_or_rejected_attempt_is_not_promoted_by_replay(tmp_path, status, verifier):
    store = CompanionStore(tmp_path / 'cycle.sqlite3')
    cycle = CompanionEngine(store).start_cycle('daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z')
    attempt = store.begin_attempt(cycle['cycle_id'], 'm1_judgment', cycle['as_of'], input_packet={})
    store.finish_attempt(attempt['attempt_id'], status, verifier=verifier)
    frozen = freeze_cycle(store, cycle['cycle_id'])
    assert replay_cycle(frozen)['qualification']['attempts'][0]['qualified'] is False
    corrupted = copy.deepcopy(frozen)
    corrupted['source']['attempts'][0]['status'] = 'succeeded'
    corrupted['source']['attempts'][0]['verifier_json'] = '{"passed": true}'
    with pytest.raises(ValueError, match='integrity'):
        replay_cycle(corrupted)


def test_missing_historical_inputs_remain_missing_and_unqualified(tmp_path):
    store = CompanionStore(tmp_path / 'cycle.sqlite3')
    cycle = CompanionEngine(store).start_cycle('daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z')
    attempt = store.begin_attempt(cycle['cycle_id'], 'm1_judgment', cycle['as_of'])
    store.finish_attempt(attempt['attempt_id'], 'succeeded', verifier={'passed': True})
    result = replay_cycle(freeze_cycle(store, cycle['cycle_id']))
    assert result['qualification']['attempts'][0]['historically_qualified'] is True
    assert result['qualification']['attempts'][0]['qualified'] is False
    assert result['evaluation_vector']['safety_reliability']['inputs_complete'] is False


@pytest.mark.parametrize('problem', ['h0_text', 'hash_conflict', 'unqualified'])
def test_replay_rejects_hidden_h0_and_conflicting_provenance(tmp_path, problem):
    store = CompanionStore(tmp_path / 'cycle.sqlite3')
    cycle = CompanionEngine(store).start_cycle('daily.execution.0945', '2026-09-21T09:45:00+08:00', '2026-09-21T01:45:00Z')
    store.append_artifact(cycle['cycle_id'], 'h0', 'human', 'Human directional claim', cycle['as_of'])
    packet = {'note': 'Human directional claim'} if problem == 'h0_text' else {}
    attempt = store.begin_attempt(cycle['cycle_id'], 'm1_judgment', cycle['as_of'],
                                  input_packet=packet, input_sha256='conflicting-hash' if problem == 'hash_conflict' else None)
    store.finish_attempt(attempt['attempt_id'], 'succeeded', verifier={'passed': True},
                         output={'judgment_qualified': False, 'snapshot': {'qualified': False}})
    frozen = freeze_cycle(store, cycle['cycle_id'])
    if problem == 'unqualified':
        assert replay_cycle(frozen)['qualification']['attempts'][0]['qualified'] is False
    else:
        with pytest.raises(ValueError, match='human content|integrity'):
            replay_cycle(frozen)
