# Local Runtime Gateway

The Python runtime owns `trading-companion.sqlite3` and is the sole business writer. The desktop client reaches it only through the authenticated, loopback-only `companion-gateway/v1` contract.

- `GET /v1/health` reports protocol compatibility and runtime health.
- `GET /v1/snapshots/{history,today,cycle,portfolio,schedules}` returns read-only projections.
- `POST /v1/commands` accepts an idempotent command identified by `command_id`.
- `GET /v1/events?after=<sequence>` is an SSE replay stream over the durable client event log.
- `GET /v1/provider-quality` returns paged, filtered technical quality rows.
- `GET /v1/provider-quality/comparison` compares model families within the selected stage/window.
- `GET /v1/provider-quality/errors` returns grouped error categories and sample sizes.
- `GET /v1/provider-quality/export?format={csv|json}` exports the same redacted metadata view.

The runtime writes a short-lived loopback descriptor to `runtime/gateway.json`; it contains loopback host, ephemeral port, protocol version and the process-local bearer token required by the desktop client. It is not a Provider credential and is regenerated for every Gateway process. Desktop command retries reuse the same `command_id`; a command-id conflict is rejected instead of being reinterpreted. File exchange remains a one-version migration fallback and is not the source for desktop history.

History is grouped by the Shanghai date of `scheduled_for`, never by file arrival time. A task/time with retry or recovery copies resolves to one richest current projection; all user-visible states remain visible.

Provider-quality endpoints are operator-only diagnostics. They expose no prompt, private message, evidence body, generated result, credential target, API key, request header, or cookie. All filter, ordering, and pagination inputs are allowlisted before statistics are evaluated.
