# Current-Bar reliability: incomplete delivery

Scope: #127 / #358, followed by #128 / #359. These changes are offline defensive fixes, not live dependency acceptance or a release qualification.

## Retry and evidence contract

- `ai-trading-tool-failure/v1` carries an allowlisted `error_code` on stderr. Neither exit 75 nor diagnostic prose authorizes a retry.
- `tool-retry-policy/v1` permits at most one same-route retry for typed timeout, connection reset, or temporary DNS failure, within the route's share of the original monotonic deadline.
- Exhausted routes are circuit-broken by cycle, capability, adapter and immutable version. Research repair cannot grant a new route budget.
- Resolution audit and capability needs carry the same per-attempt receipts: route/version, acquired timestamp, exit code, typed error, diagnostic/raw artifact references, and retry policy. Secret-bearing requests and diagnostics are rejected before persistence.

## Unresolved #127 ownership blocker

`__main__._m0_failure_is_retryable` searches the entire verifier for prose markers, including `portfolio_market_state`. That key also occurs in successfully covered gap states. Consequently the actual scheduled all-source-failure replay returns `m0_retry_wait` instead of publishing the required terminal fault, despite `stop_reason=current_bar_routes_exhausted`.

The owner of `__main__.py` must consume the terminal exhaustion/typed retry state before generic availability heuristics. This branch deliberately does not edit that exclusive file or falsify verifier content. `engine.py`, `store.py`, `packet_builder.py`, qualification/publish/install scripts, schedules and historical judgments are also unchanged.

The retained scheduled regression is intentionally not skipped, xfailed, or weakened. #127 / #358 are **not complete** until it passes through the actual scheduled pipeline and Exchange publication.

## Verification of the #127 pass

- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_tooling.py tests/runtime/test_local_research.py tests/runtime/test_current_bar_reliability.py -q`: **165 passed, 1 failed**. The only failure is the scheduled terminal-fault acceptance above.
- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_current_bar_reliability.py::test_current_bar_public_fault_names_only_attempted_routes -q`: **1 passed** (added after the broader run).

## #128 fail-closed guard, not a qualified Tencent fallback

The previous Tencent converter recursively collected rows from arbitrary symbols, assigned the requested date to undated rows, derived interval extrema from only two sampled prices, assumed ST was false, inferred suspension from zero volume, and inferred finality from the requested mode. Those claims are not proven by a cumulative minute tape.

The current converter binds the exact requested vendor symbol and the response's trading date, rejects duplicate JSON keys and malformed/non-finite/non-monotonic rows, checks consecutive completed minutes and the frozen cutoff, and validates every requested holding before rejecting the tape's missing provenance. It **does not produce a qualifying Bar** from the currently supported sampled-price shape. The independent route is still attempted immediately after MarketHub failure; a well-formed tape reports typed `tool_current_bar_source_invalid` rather than synthesizing facts. This intentionally removes the unsafe apparent success path.

The shared technical validator additionally rejects mismatched symbol/exchange identities, non-finite and boolean OHLCVA values, wrong interval duration/date, completed future intervals, stale observations despite a claimed zero freshness, missing boolean status metadata, false close finality, incomplete/duplicate frozen coverage, and an inconsistent top-level fact timestamp. MarketHub must supply actual boolean ST/suspension metadata rather than relying on coercion.

#128 / #359 remain **not complete**. Required follow-up is captured provider evidence establishing interval OHLC, cumulative volume/amount units and security-status provenance; then a deterministic converter, captured same-minute MarketHub/Tencent differential tests, and the successful formal 14:30 scheduled qualification/Exchange replay. The existing successful Tencent test currently supplies an undated sampled-price fixture, not a captured OHLCVA contract; it is retained to expose the unresolved positive fallback, not rewritten to claim success. No captured-provider parity or successful Tencent scheduled delivery is claimed.

## Verification of the #128 safety pass

- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_tooling.py tests/runtime/test_local_research.py tests/runtime/test_current_bar_reliability.py -q`: **198 passed, 3 failed**. Failures were stale-error precedence, the unresolved positive Tencent fallback fixture, and the scheduled terminal-fault acceptance.
- The stale-error precedence regression was then fixed without changing its test: stale evidence remains classified as stale before checking missing status metadata.
- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_tooling.py::ToolRunnerTests::test_current_equity_bar_rejects_a_stale_observation tests/runtime/test_current_bar_reliability.py::test_current_bar_rejects_invalid_values_identity_time_and_metadata -q`: **13 passed** after that fix. The broader set was not rerun after this localized correction; the two acceptance gaps remain unresolved.

No Trading-window observations, real credentials, installation, activation, rollback, or production service changes were performed.
