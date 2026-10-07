"""Versioned, fail-closed retry policy shared by tool resolution and repair."""
from __future__ import annotations

import json

RETRY_POLICY = "tool-retry-policy/v1"
FAILURE_CONTRACT = "ai-trading-tool-failure/v1"
TRANSIENT_ERRORS = frozenset({
    "tool_network_timeout", "tool_network_connection_reset", "tool_network_dns_temporary",
})
TYPED_ERRORS = TRANSIENT_ERRORS | frozenset({
    "tool_access_restricted", "tool_process_configuration", "tool_network_unavailable",
    "tool_http_client_error", "tool_http_server_error", "tool_response_contract_invalid",
    "tool_current_bar_result_invalid", "tool_current_bar_symbol_mismatch",
    "tool_current_bar_identity_invalid", "tool_current_bar_time_invalid",
    "tool_current_bar_values_invalid", "tool_current_bar_stale",
    "tool_current_bar_status_invalid", "tool_current_bar_finality_invalid",
    "tool_current_bar_source_invalid",
})


def process_error(stderr: bytes, exit_code: int) -> str:
    """Only an explicit versioned error permits retry, never exit 75 or prose."""
    try:
        failure = json.loads(stderr.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        failure = None
    if isinstance(failure, dict) and failure.get("contract") == FAILURE_CONTRACT:
        code = failure.get("error_code")
        if isinstance(code, str) and code in TYPED_ERRORS:
            return code
    return (
        "tool_access_restricted" if exit_code == 64
        else "tool_browser_unavailable" if exit_code == 69
        else "tool_process_failed"
    )
