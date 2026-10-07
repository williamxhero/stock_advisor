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

No Trading-window observations, real credentials, installation, activation, rollback, or production service changes were performed.
