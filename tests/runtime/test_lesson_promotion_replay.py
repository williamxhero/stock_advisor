from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ai_trading_companion.memory_type import sha256

from ai_trading_companion.lesson_promotion import LessonPromotion
from ai_trading_companion.memory_port import InMemoryMemoryAdapter
from ai_trading_companion.memory_write import write_memory
from test_lesson_promotion import memory_port

AT = "2026-09-21T08:10:00Z"
LATER = "2026-09-22T08:10:00Z"
SPACE = "lesson-replay-test"


def proposed(memory):
    evidence = write_memory(memory, "evidence", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": "evidence",
        "episode_type": "external_evidence", "authority": "immutable_source_reference",
        "body": "Frozen market mechanism and counterexample", "content_hash": "auto",
        "protocol_version": "memoryhub/v1", "occurred_at": AT, "known_at": AT, "submitted_at": AT,
    }, semantic_type="evidence")["episode_id"]
    service = LessonPromotion(memory, SPACE)
    candidate = service.propose("candidate", "Use only in tested states", market_states=["range"],
                                evidence_episode_ids=[evidence], counterevidence_episode_ids=[evidence], as_of=AT)
    return service, candidate


def test_frozen_replay_preserves_actual_memoryhub_episodes_and_candidate_maturity(memory_port):
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    memory = memory_port
    service, candidate = proposed(memory)
    before = copy.deepcopy(memory.export_space(SPACE))
    frozen = freeze_lessons(memory, SPACE, as_of=AT)
    original = copy.deepcopy(frozen)

    first = replay_lessons(frozen)
    second = replay_lessons(copy.deepcopy(frozen))

    assert first == second
    assert frozen == original
    assert memory.export_space(SPACE) == before
    assert frozen["source"]["episodes"] == before["episodes"]
    assert first["qualification"]["candidates"][0]["assessment"] == service.assess(candidate["episode_id"], as_of=AT)
    assert first["trace"][0]["recorded_state"] == "candidate"
    assert set(first["evaluation_vector"]) == {
        "delivery_speed", "qualification_probability", "research_quality", "judgment_outcome", "safety_reliability",
    }


@pytest.mark.parametrize("damage", ["episode_hash", "body", "envelope", "lesson_version", "policy_version", "future", "duplicate", "cross_space"])
def test_replay_validates_each_original_episode_even_when_outer_hash_is_recomputed(damage):
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    memory = InMemoryMemoryAdapter()
    proposed(memory)
    frozen = freeze_lessons(memory, SPACE, as_of=AT)
    source = frozen["source"]
    episode = source["episodes"][0]
    if damage == "episode_hash":
        source["episode_sha256"][episode["episode_id"]] = "0" * 64
    elif damage == "body":
        episode["body"] = "changed body without original content hash"
    elif damage == "envelope":
        episode["metadata"]["memory_type"]["authority"] = "user_private_fact"
    elif damage == "lesson_version":
        source["versions"]["lesson"] = "LessonPromotionSpec/v999"
    elif damage == "policy_version":
        source["versions"]["policy"] = "lesson-maturity/v999"
    elif damage == "future":
        source["snapshot"]["as_of"] = "2026-09-20T08:10:00Z"
    elif damage == "duplicate":
        source["episodes"].append(copy.deepcopy(episode))
    elif damage == "cross_space":
        episode["memory_space_id"] = "another-space"
    if damage != "episode_hash":
        source["episode_sha256"] = {row["episode_id"]: sha256(row) for row in source["episodes"]}
    frozen["source_sha256"] = sha256(source)
    with pytest.raises(ValueError):
        replay_lessons(frozen)


def test_replay_refuses_missing_original_evidence_instead_of_repairing_history():
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    memory = InMemoryMemoryAdapter()
    proposed(memory)
    frozen = freeze_lessons(memory, SPACE, as_of=AT)
    removed = frozen["source"]["episodes"].pop(0)
    del frozen["source"]["episode_sha256"][removed["episode_id"]]
    frozen["source_sha256"] = sha256(frozen["source"])
    with pytest.raises(ValueError, match="lineage"):
        replay_lessons(frozen)


def observation(memory, event, *, status="correct"):
    from ai_trading_companion.memory_write import canonical_json
    result = {"verification_status": status, "observations": [{"subject": "600519", "mae": 0.0, "excess_return": 99.0}]}
    return write_memory(memory, "learning", {
        "memory_space_id": SPACE, "source_system": "stock-advisor", "source_event_id": event,
        "episode_type": "outcome", "authority": "runtime_learning", "body": canonical_json(result),
        "content_hash": "auto", "protocol_version": "memoryhub/v1", "occurred_at": LATER, "known_at": LATER, "submitted_at": LATER,
        "metadata": {"outcome_result": result, "cycle_id": "cycle", "horizon": "T+1", "market_state": "range"},
    }, semantic_type="outcome")["episode_id"]


def test_replay_recomputes_attempts_from_outcome_episodes_not_stored_model_support():
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    from ai_trading_companion.memory_write import canonical_json
    memory = InMemoryMemoryAdapter()
    service, candidate = proposed(memory)
    good = observation(memory, "good")
    baseline = observation(memory, "baseline", status="incorrect")
    service.observe("one", candidate["episode_id"], good, baseline, subject="600519", market_state="range", as_of=LATER)
    frozen = freeze_lessons(memory, SPACE, as_of=LATER)
    event_episode = frozen["source"]["episodes"][-1]
    event_episode["metadata"]["lesson_promotion"]["payload"]["trial"].update(
        {"support": 1, "baseline_support": 0, "quality_passed": True, "safety_passed": True})
    event_episode["body"] = canonical_json(event_episode["metadata"]["lesson_promotion"])
    import hashlib
    event_episode["content_hash"] = "sha256:" + hashlib.sha256(event_episode["body"].encode()).hexdigest()
    frozen["source"]["episode_sha256"][event_episode["episode_id"]] = sha256(event_episode)
    frozen["source_sha256"] = sha256(frozen["source"])

    replay = replay_lessons(frozen)

    assert replay["qualification"]["candidates"][0]["assessment"] == service.assess(candidate["episode_id"], as_of=LATER)
    assert replay["trace"][-1]["recomputed_decision"]["payload"]["trial"]["support"] == 0
    assert replay["trace"][-1]["recorded_decision"]["payload"]["trial"]["support"] == 1
    assert replay["trace"][-1]["matches_recorded"] is False


def test_later_append_only_rollback_does_not_change_an_earlier_frozen_replay():
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    memory = InMemoryMemoryAdapter()
    service, candidate = proposed(memory)
    early = freeze_lessons(memory, SPACE, as_of=AT)
    early_receipt = replay_lessons(early)
    rollback = service.rollback("withdraw", candidate["episode_id"], reason="counterexample invalidates scope", as_of=LATER)
    history = copy.deepcopy(memory.export_space(SPACE))
    later = replay_lessons(freeze_lessons(memory, SPACE, as_of=LATER))

    assert replay_lessons(early) == early_receipt
    assert early_receipt["qualification"]["candidates"][0]["assessment"]["state"] == "inconclusive"
    assert later["qualification"]["candidates"][0]["assessment"]["state"] == "rolled_back"
    assert later["trace"][-1]["recomputed_decision"] == rollback["decision"]
    assert later["trace"][-1]["matches_recorded"] is True
    assert memory.export_space(SPACE) == history


def test_replay_reconstructs_factual_frozen_pair_without_trusting_recorded_trial_scores():
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    from test_lesson_frozen_evidence import END, SPACE as FROZEN_SPACE, frozen_pair, proposal
    memory = InMemoryMemoryAdapter()
    service, candidate = proposal(memory)
    pair = frozen_pair(memory, lesson_candidate_id=candidate["decision"]["candidate_id"])
    # The MemoryHub watermark is ledger-wide, while an export is space-scoped.
    write_memory(memory, "evidence", {
        "memory_space_id": "unrelated-space", "source_system": "stock-advisor", "source_event_id": "interleaved",
        "episode_type": "external_evidence", "authority": "immutable_source_reference", "body": "unrelated evidence",
        "content_hash": "auto", "protocol_version": "memoryhub/v1", "occurred_at": END, "known_at": END, "submitted_at": END,
    }, semantic_type="evidence")
    original_attempt = service.observe_frozen_pair("offline-one", candidate["episode_id"], pair, as_of=END)
    frozen = freeze_lessons(memory, FROZEN_SPACE, as_of=END)
    original = copy.deepcopy(frozen)
    history = copy.deepcopy(memory.export_space(FROZEN_SPACE))

    replay = replay_lessons(frozen)

    assert replay == replay_lessons(copy.deepcopy(frozen))
    assert replay["trace"][-1]["recomputed_decision"] == original_attempt["decision"]
    assert replay["trace"][-1]["matches_recorded"] is True
    assert replay["trace"][-1]["recomputed_decision"]["payload"]["trial"]["support"] == 1
    assert replay["qualification"]["candidates"][0]["assessment"] == service.assess(candidate["episode_id"], as_of=END)
    assert frozen == original
    assert memory.export_space(FROZEN_SPACE) == history


@pytest.mark.slow
# Exercise a real uncertainty threshold with independent raw windows; a few
# caller-asserted wins must never stand in for a statistically mature revision.
def test_statistically_qualified_offline_revision_replays_then_rolls_back_without_rewriting():
    from ai_trading_companion.lesson_promotion_replay import freeze_lessons, replay_lessons
    from test_lesson_frozen_evidence import START, SPACE as FROZEN_SPACE, evidence, frozen_pair
    memory = InMemoryMemoryAdapter()
    supporting = evidence(memory, "support", {"mechanism": "momentum"})
    contrary = evidence(memory, "contrary", {"counterexample": "momentum can fail"})
    service = LessonPromotion(memory, FROZEN_SPACE)
    candidate = service.propose("qualified-replay", "Momentum only in validated expansion", market_states=["trend_expansion"],
                                evidence_episode_ids=[supporting], counterevidence_episode_ids=[contrary], as_of=START)
    as_of = "2031-01-01T00:00:00Z"
    for index in range(256):
        pair = frozen_pair(memory, f"independent-{index}", index=index, lesson_candidate_id=candidate["decision"]["candidate_id"])
        promoted = service.observe_frozen_pair(f"trial-{index}", candidate["episode_id"], pair, as_of=as_of)
    assert promoted["decision"]["state"] == "promoted"
    frozen = freeze_lessons(memory, FROZEN_SPACE, as_of=as_of)
    before = copy.deepcopy(memory.export_space(FROZEN_SPACE))

    replay = replay_lessons(frozen)

    assert replay["qualification"]["candidates"][0]["assessment"]["state"] == "promoted"
    assert replay["trace"][-1]["recomputed_decision"] == promoted["decision"]
    assert replay["trace"][-1]["matches_recorded"] is True
    assert replay["qualification"]["production_strategy_approved"] is False
    assert memory.export_space(FROZEN_SPACE) == before
    service.rollback("withdraw-qualified", promoted["episode_id"], reason="new adverse evidence", as_of=as_of)
    later = replay_lessons(freeze_lessons(memory, FROZEN_SPACE, as_of=as_of))
    assert later["qualification"]["candidates"][0]["assessment"]["state"] == "rolled_back"
    assert replay_lessons(frozen) == replay


def test_source_isolated_installed_cli_qualifies_twice_without_live_claims(tmp_path):
    root = Path(__file__).resolve().parents[2]
    install = tmp_path / "app"
    shutil.copytree(root / "src/runtime/ai_trading_companion", install / "runtime/ai_trading_companion",
                    ignore=shutil.ignore_patterns("__pycache__"))
    home = tmp_path / "isolated-home"
    home.mkdir()
    environment = {**os.environ, "PYTHONPATH": str(install / "runtime"),
                   "AI_TRADING_COMPANION_HOME": str(home), "AI_TRADING_COMPANION_INSTALL_ROOT": str(install)}
    command = [sys.executable, "-m", "ai_trading_companion.lesson_promotion_replay"]
    first = subprocess.run(command, cwd=home, env=environment, capture_output=True, check=True)
    second = subprocess.run(command, cwd=home, env=environment, capture_output=True, check=True)
    assert first.stdout == second.stdout
    result = json.loads(first.stdout)
    assert result["contract"] == "LessonPromotionInstallQualification/v1"
    assert result["qualified"] is True
    assert all(result["checks"].values())
    assert result["checks"]["factual_pair_reconstructed"] is True
    assert result["scope"] == "offline_frozen_replay_only"
    assert result["production_strategy_approved"] is False
    for axis in ("delivery_speed", "qualification_probability", "research_quality", "judgment_outcome"):
        assert result["evaluation_vector"][axis]["status"] == "not_measured"
        assert result["evaluation_vector"][axis]["measurements"]["measured"] is False
        assert result["evaluation_vector"][axis]["reason"]
    assert result["evaluation_vector"]["safety_reliability"]["status"] == "pass"
    assert list(home.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 native stdout decoding seam")
def test_formal_verifier_roundtrips_unicode_and_restores_native_stdout_encoding():
    verifier = (Path(__file__).resolve().parents[2] / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    save = ""
    configure = ""
    restore = ""
    if "$previousPythonIOEncoding =" in verifier:
        save = verifier.split("$previousPythonIOEncoding =", 1)[1].split("New-Item", 1)[0]
        save = "$previousPythonIOEncoding =" + save
        configure = verifier.split("$env:PYTHONIOENCODING = 'utf-8'", 1)[1].split("$env:AI_TRADING_COMPANION_HOME = $healthHome", 1)[0]
        configure = "$env:PYTHONIOENCODING = 'utf-8'" + configure
        restore = verifier.rsplit("finally {", 1)[1].split("if ($null -eq $previousHome)", 1)[0]
    command = (
        "$ErrorActionPreference = 'Stop'; [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936); "
        "$env:PYTHONIOENCODING = 'utf-8'; " + save + "; try { " + configure +
        "; $raw = (& '" + sys.executable.replace("'", "''") + "' -c "
        '"import json; print(json.dumps({\'text\': chr(0x51bb) + chr(0x7ed3)}, ensure_ascii=False))"); '
        "$correct = (($raw | ConvertFrom-Json).text -eq ([string][char]0x51bb + [char]0x7ed3)); "
        "} finally { " + restore + " }; "
        "$restored = ([Console]::OutputEncoding.CodePage -eq 936 -and $env:PYTHONIOENCODING -eq 'utf-8'); "
        "[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); "
        "if (-not $correct -or -not $restored) { throw 'Verifier Unicode roundtrip or encoding restoration failed' }; 'verified'"
    )
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout.strip() == b"verified"


def test_formal_scripts_include_lesson_regression_replay_and_installed_artifacts():
    root = Path(__file__).resolve().parents[2]
    tests = (root / "scripts/test.ps1").read_text(encoding="utf-8")
    verifier = (root / "scripts/verify-install.ps1").read_text(encoding="utf-8")
    assert "'LessonPromotion'" in tests
    assert "$Select -eq 'LessonPromotion'" in tests
    project_regression = tests.split("if ($ProjectRegression) {")[1].split("exit 0")[0]
    for name in ("test_lesson_promotion.py", "test_lesson_frozen_evidence.py", "test_lesson_promotion_replay.py"):
        assert name in project_regression
        assert name in tests.split("$Select -eq 'LessonPromotion'")[1].split("elseif")[0]
    for artifact in ("resources\\contracts\\lesson-promotion-spec-v1.schema.json",
                     "resources\\contracts\\companion-outcome-result-v2.schema.json",
                     "resources\\contracts\\lesson-frozen-pair-v1.schema.json",
                     "resources\\contracts\\lesson-frozen-window-v1.schema.json",
                     "runtime\\ai_trading_companion\\lesson_promotion.py",
                     "runtime\\ai_trading_companion\\lesson_promotion_replay.py"):
        assert artifact in verifier
    assert verifier.count("-m ai_trading_companion.lesson_promotion_replay") == 2
    assert "LessonPromotionInstallQualification/v1" in verifier
    assert "$lessonReplayOne -ne $lessonReplayTwo" in verifier
    assert "'factual_pair_reconstructed'" in verifier
