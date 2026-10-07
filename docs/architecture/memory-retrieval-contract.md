# MemoryRetrievalSpec/v1

Runtime's read-only retrieval projection for #102, implemented at `MemoryPort` in
`ai_trading_companion.memory_retrieval` and `memory_port`. MemoryHub remains the
only ledger owner. The HTTP endpoints, Exchange, CLI and ledger format do not
change. Existing formal packet construction and adaptive chat research consume
ranked bundles without a second memory store or an LLM-owned write path.

## Input and provenance

`build_input` / `validate_input` define `MemoryRetrievalSpec/v1`, version 1,
`memory-retrieval-policy/v1`: the immutable MemoryHub snapshot (space, stage,
cycle, as-of, watermark and protocol/policy versions), query, result limit,
instruments, optional market state, and empty write permissions. Six-digit
instrument codes can be taken deterministically from a query; callers can
instead supply `retrieve_bundle(..., context={"instruments": [...],
"market_state": "..."})`. Unknown market state is not guessed. The existing
formal caller does not supply a market-state context; adding that evidence-bound
input there requires the separately owned `packet_builder.py` seam.

An optional authoritative episode metadata `memory_retrieval` profile uses
`MemoryRetrievalProfile/v1`: reliability (`unverified`, `verified`, `conflicted`,
`rejected`), outcome support (`unknown`, `supported`, `contradicted`), lesson state
(`candidate`, `verified`, `error`), instrument/market scopes and parent, evidence
and outcome episode references. Verified reliability requires evidence refs;
supported/contradicted outcomes require outcome refs; verified lessons require
both. References must resolve in the same snapshot and have the appropriate
semantic type. Retrieval never creates these declarations or promotes a lesson.
Legacy records remain readable, with conservative unknown support/reliability;
MemoryTypeSpec identity is checked when present. Private user facts/preferences
retain user authority, not authority over market conclusions.

Each result carries the contract/policy version, semantic type, six independent
ranking dimensions, half-life, ranking score and original episode/hash/source/
known-at references. The bundle also retains the original service bundle/audit
IDs and retriever/index/extractor/policy/protocol versions, candidate IDs,
exclusion reasons, input hash and accepted original hashes. Blind receipts expose
only qualified candidate IDs and no rejected IDs/counts or saturation signal:
query-dependent rejected hits can themselves reveal H0 direction. Raw candidate
information belongs only in the separate offline archive, not blind model context.

## Ranking and decay

The bounded candidate window is the existing MemoryHub lexical provider's first
100 matches. Runtime expands original records and applies typed ranking **before**
the requested result limit, so lexical repetition cannot determine the final
order. This is not a full-ledger recall guarantee; saturation is explicitly
reported. No new embeddings, provider dependencies or index are introduced.

Relevance is the fraction of distinct query terms present in original text.
Instrument/market scope matches are 1, unknown scope 0.5 and mismatched scope
0.25. Reliability weights are 1 / 0.5 / 0.2 / 0 for verified / unverified /
conflicted / rejected; rejected records are excluded. Outcome support is 1 /
0.5 / 0.2 for supported / unknown / contradicted. Error lessons cannot have
support weight above 0.2. These are retrieval weights, not measured probabilities.

Decay is `2 ** (-age_days / half_life_days)` from occurrence time to frozen as-of:
observations 2 days; preferences 3650; user facts 30; judgments 14; outcomes 90;
candidate lessons 30; verified lessons 365; error lessons 7; rules 365; messages
7; evidence 7; corrections 30; operational records 1.

Ranking score is `(0.7 * relevance + 0.3 * instrument_match) * reliability *
decay * outcome_support * market_state_match`. Ties use episode identity. This
retrieval score is **not** an evaluation vector or a maturity/promotion verdict.

## Isolation, failures and recovery

All reads remain bound to MemoryHub space, as-of and watermark. Occurred, known
and submitted times cannot be future context. Parent/evidence/outcome,
`derived_from_episode_ids`, related and correction lineage is traversed
transitively. Missing, cross-space, future and cyclic lineage cannot qualify.
M1 search, retrieve, expand and related reads reject H0, propositions/actions,
conversation/premarket/M2 provenance and their descendants, including across
cycles. Legacy cognition records with unresolved `source_message_id` are not
blind-safe. Unknown legacy non-factual lineage is excluded from M1 rather than
assuming that a summary or a personal-fact label removes directional influence.
Derived summaries do not replace original ledger text in ranked results.

Outside blind stages, bad candidates produce explicit `degraded` receipts; no
matches produce `empty`. Blind receipt status depends only on qualified results,
so rejection metadata cannot become a directional side channel.
Transport/source-integrity outages raise `MemoryUnavailable`, never a local
memory fallback or a partial successful bundle. Reusing a snapshot after retry
or adapter restart keeps the same watermark/policy boundary. The HTTP adapter
rejects a changed snapshot or query identity.

## Frozen evidence and verification

`HttpMemoryAdapter.freeze_retrieval` captures an actual service bundle, original
records and denied reads in a separate `MemoryRetrievalReplay/v1` archive.
`frozen_replay` verifies its hash, recomputes the qualification/ranking offline
and compares the original output. Archives can contain rejected originals and
must **not** be passed to models or used as a live visibility bypass. No archive
is written automatically and no historical judgment is changed.

Replay evidence separately records speed and qualification probability as
unmeasured/not estimated, research quality as deterministic retrieval-only,
judgment outcome as unmeasured, and safety as reproduced snapshot-bound,
read-only qualification. It does not claim Trading-window evidence, research
maturity, live non-inferiority or installed acceptance.

Run `scripts/test.ps1 -Select MemoryRetrieval` for local synthetic HTTP and
adapter regression plus desktop tests. The same retrieval regression is included
in `-ProjectRegression`. Tests replay both chat and blind M1 inputs twice and
cover failures, recovery, provenance and immutability. No qualification, publish
or install script is changed; formal clean installation, source-unavailable
installed smoke, revision verification and activation/rollback remain user-owned.
