# Companion decision cycle replay

`cycle_replay.freeze_cycle(store, cycle_id)` captures one SQLite read transaction without initializing missing historical stages. The receipt retains the cycle contract, frozen private facts, attempts (input, output, model and runner versions), evidence snapshots, original artifacts, judgment snapshots, checkpoints and append-only events. It is an internal audit API, not an Exchange command or permission to publish or modify history.

`replay_cycle(receipt)` verifies receipt integrity, evidence snapshot hashes, M1 forbidden keys and recorded human text, available input hashes and checkpoint bindings. It reconstructs recorded verifier decisions and preserves explicit unqualified conclusions. Missing historical inputs stay missing; they cannot receive replay qualification. Historical verifier acceptance is recorded separately. A replay does not call a model, require identical generated wording, verify external market truth, or replace MemoryHub ownership.

Evaluation dimensions are independent: recorded attempt durations; qualification probability (not estimated offline); recorded research verifiers; judgment outcome (not observed offline); and read-only, M1 blindness and input completeness. No aggregate score is produced. This receipt cannot authorize strategy promotion or replace paired live evaluation.

## Ticket 02 verification, 2026-10-02

- `scripts/test.ps1 -ProjectRegression`: 158 passed. Includes cycle identity, retry/recovery, immutable revisions, frozen replay, missing/conflicting evidence, temporal integrity, publication and internal role permissions.
- Full `scripts/test.ps1`: MemoryHub 28 passed; Runtime 780 passed, 5 skipped, 1 failed. The existing active-research evaluation fixture schedules October 1 before the current candidate registration and is rejected by the future-task gate. The test script stops before desktop tests on that failure.
- Public schema, Exchange, CLI, plugin and installation contracts are unchanged in this ticket; conditional clean-install/source-unavailable/build-info qualification is not triggered.
- Separate `dotnet test AITradingCompanion.sln --nologo`: 102 passed after NuGet restore.
- Frozen replay correctness is verified; live delivery speed, probability, research effectiveness and investment outcomes are not established by these deterministic regressions.

The regression gate keeps the five acceptance dimensions separate in the replay
receipt: delivery speed, qualification probability, research quality, judgment
outcome, and safety/reliability. Offline replay records unavailable dimensions
as `not_estimated` or `not_observed`; it never folds them into an aggregate
score or upgrades a historically accepted attempt whose inputs cannot be
reconstructed.
