# Local Runtime Gateway

The Python runtime owns `trading-companion.sqlite3` and is the sole business writer. The desktop client reaches it only through the authenticated, loopback-only `companion-gateway/v1` contract.

- `GET /v1/health` reports protocol compatibility and runtime health.
- `GET /v1/snapshots/{history,today,cycle,portfolio,schedules}` returns read-only projections.
- `POST /v1/commands` accepts an idempotent command identified by `command_id`.
- `GET /v1/events?after=<sequence>` is an SSE replay stream over the durable client event log.

The runtime writes a short-lived loopback descriptor to `runtime/gateway.json`; it contains loopback host, ephemeral port, protocol version and the process-local bearer token required by the desktop client. It is not a Provider credential and is regenerated for every Gateway process. Desktop command retries reuse the same `command_id`; a command-id conflict is rejected instead of being reinterpreted. File exchange remains a one-version migration fallback and is not the source for desktop history.

History is grouped by the Shanghai date of `scheduled_for`, never by file arrival time. A task/time with retry or recovery copies resolves to one richest current projection; all user-visible states remain visible.

LLM Provider 的管理和质量诊断不属于本地 Gateway；应用通过固定的小电脑 Provider Broker 调用 LLM，运营管理使用 Broker 管理台。
