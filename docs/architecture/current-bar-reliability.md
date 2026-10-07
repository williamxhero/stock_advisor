# Current-Bar reliability: offline contract and delivery

Scope: #127 / #358 and #128 / #359. These are offline reliability fixes, not live dependency acceptance or a release qualification.

## Retry and terminal delivery

- `ai-trading-tool-failure/v1` carries an allowlisted `error_code` on stderr. Neither exit 75 nor diagnostic prose authorizes a retry.
- `tool-retry-policy/v1` permits at most one same-route retry for typed timeout, connection reset, or temporary DNS failure, within the route's share of the original monotonic deadline.
- Exhausted routes are circuit-broken by cycle, capability, adapter and immutable version. Research repair cannot grant a new route budget.
- Resolution audit and capability needs carry the same per-attempt receipts: route/version, acquired timestamp, exit code, typed error, diagnostic/raw artifact references, and retry policy. Secret-bearing requests and diagnostics are rejected before persistence.
- `stop_reason=current_bar_routes_exhausted` is terminal before generic availability heuristics. A covered requirement name elsewhere in the verifier cannot reopen the exhausted routes. The scheduled pipeline records one failed attempt and one system fault, publishes `research.failed` exactly once through Exchange, and emits neither M0 nor M1. The original scheduled exhaustion regression remains unchanged.
- Research packet finalization includes the derived agent and role contracts before checkpoint lookup and persistence. Re-finalization rebuilds those contracts from the same base rather than recursively hashing their own references. Qualified evidence therefore retains the same frozen packet identity at the acquisition and delivery boundaries.

## Tencent minute-Bar input contract

The generated Tencent adapter supports **`tencent-minute-bar/v1`**, an explicit normalized adapter input contract. This is **not a claim that Tencent's public sampled-price API already exposes this schema**. The response must bind exactly the requested vendor symbol:

```json
{
  "data": {
    "sh600000": {
      "data": {
        "contract": "tencent-minute-bar/v1",
        "date": "20260901",
        "units": {"price": "CNY/share", "volume": "shares", "amount": "CNY"},
        "volume_mode": "cumulative",
        "interval_semantics": "start_labelled_1m",
        "security_status": {
          "is_st": false,
          "is_suspended": false,
          "market_status": "trading",
          "as_of": "2026-09-01T14:30:00+08:00",
          "source_url": "https://provider.example/security-status/sh600000"
        },
        "data": [
          {"time": "1428", "open": 10, "high": 10.1, "low": 9.9, "close": 10,
           "cumulative_volume": 100, "cumulative_amount": 1000, "is_final": true,
           "observed_at": "2026-09-01T14:29:00+08:00", "last_trade_at": "2026-09-01T14:28:50+08:00"},
          {"time": "1429", "open": 10.1, "high": 10.6, "low": 9.8, "close": 10.2,
           "cumulative_volume": 120, "cumulative_amount": 1204, "is_final": true,
           "observed_at": "2026-09-01T14:30:00+08:00", "last_trade_at": "2026-09-01T14:29:50+08:00"}
        ]
      }
    }
  }
}
```

This example and the test fixtures are synthetic, not captured provider evidence.

- The response date must match the frozen Shanghai trading date. Duplicate keys, wrong/missing vendor symbols, duplicate/non-monotonic minutes, decreasing cumulative totals, non-finite/boolean numbers, incomplete OHLC and invalid extrema fail closed.
- `time` explicitly labels the start of a one-minute interval in the A-share trading session. Only completed consecutive minutes at or before the cutoff may be selected; a still-forming current minute cannot displace the completed preceding interval. Missing, premature or insufficient close finality is rejected.
- OHLC comes from the selected interval's explicit fields, never two sampled prices. Volume and amount are differences of consecutive cumulative totals. Volume accepts only `shares` or `lots_100` and is normalized to shares; amount accepts only CNY. The resulting trade amount must be consistent with the interval extrema and the converted volume. A zero-trade interval must carry forward the previous close, but zero volume never proves suspension.
- Observation and last-trade timestamps must be timezone-aware, dated, consistent with the interval and frozen cutoff, and fresh. Security status must include actual boolean ST/suspension fields, a supported market status, a contemporaneous timestamp and a source URL. Status is not guessed from a name, volume or requested finality.
- All frozen holdings must validate before any Bar is returned. Successful Bars carry `provider=tencent_minute`, `source_semantics=derived`, `degraded=true`, normalized units, cumulative derivation operands and status provenance. Source evidence retains the original normalized minute contract in the archived raw tool result, outside the bounded qualification excerpt so a long tape cannot truncate the Bar JSON.
- The existing sampled-price shape remains rejected with typed failure receipts. Its date alone, or an unproven `contract` label without OHLC/units/status evidence, does not make it qualifying data.

The shared technical validator also rejects mismatched symbol/exchange identities, non-finite/boolean OHLCVA, wrong interval duration/date, completed future intervals, stale observations despite a claimed zero freshness, missing boolean status metadata, false close finality, incomplete/duplicate frozen coverage and an inconsistent top-level fact timestamp. MarketHub must supply actual boolean ST/suspension metadata rather than relying on coercion.

## Offline acceptance and remaining external evidence

Offline regression coverage includes restoration of the existing MarketHub-failure/Tencent-success capability test with an explicit contract-valid input, rejection of undated or sampled-only tapes, all three frozen holdings, both supported volume units, ST and suspension provenance, zero-volume non-suspension, typed invalid-contract receipts, and a successful formal 14:30 scheduled qualification through the real EvidenceContract and Exchange seams. The positive scheduled replay uses controlled non-Bar observations and a synthetic compose response; it is not a live model/provider acceptance.

The following remain user-owned and are not claimed complete:

1. Captured Tencent trading-window responses and provider documentation establishing the actual date, interval OHLC, interval-label semantics, cumulative volume/amount units, completion timestamps, and contemporaneous ST/suspension/status source. The public sampled-price endpoint is not automatically promoted to this normalized schema; a verified provider-to-contract mapping still requires those captures.
2. Captured same-symbol, same-minute MarketHub/Tencent differential evidence, including the three frozen holdings and trading-status edge cases. Synthetic fixtures do not establish provider parity.
3. Live formal 14:30 qualification and Exchange delivery using the verified provider mapping, plus release/install/activation and rollback acceptance. No live requests, real credentials or production service changes were performed.

## Verification

- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_tooling.py tests/runtime/test_local_research.py tests/runtime/test_current_bar_reliability.py -q`: **230 passed** on the final implementation.
- `env -u PYTHONPATH py -3.13 -m pytest tests/runtime/test_companion_exchange.py -q`: **15 passed**, run after the acceptance suite.
- Both operator-reported failures were reproduced before editing (**2 failed**) and pass in the final acceptance run. An intermediate broader run exposed an incorrect holding-order assumption in the new positive fixture (**1 failed, 229 passed**); the assertion now checks the actual frozen contract order, not the portfolio input order.
- Pytest emits the existing unset `asyncio_default_fixture_loop_scope` deprecation warning; no tests were skipped, disabled or xfailed.

`engine.py`, `store.py`, `packet_builder.py`, schedules and historical judgments are unchanged.
