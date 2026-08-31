from __future__ import annotations

from pathlib import Path

from ai_trading_companion.store import CompanionStore
from ai_trading_companion.tool_manager import ToolManagerRuntime


def test_tool_manager_projects_exchange_and_applies_idempotent_pause(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "runtime" / "companion.sqlite3")
    need = store.submit_capability_need({"contract": "ai-trading-capability-need/v1", "capability": "quote", "output_contract": {"v": 1}, "examples": [], "failure_traces": [], "source_hints": []})
    runtime = ToolManagerRuntime(store, tmp_path / "tools", tmp_path / "exchange")
    command = {"contract": "ai-trading-tool-manager-command/v1", "command_id": "pause-1", "type": "pause", "need_id": need["need_id"]}

    first = runtime.command(command)
    second = runtime.command(command)
    projection = runtime.publish_projection()

    assert first == second
    assert first["need"]["state"] == "paused"
    assert projection.exists()


def test_disabled_tool_is_not_resolvable(tmp_path: Path) -> None:
    from ai_trading_companion.builtin_tools import ensure_builtin_tools
    from ai_trading_companion.tooling import ToolCatalog, ToolLookupError
    tools = tmp_path / "tools"; ensure_builtin_tools(tools)
    runtime = ToolManagerRuntime(CompanionStore(tmp_path / "companion.sqlite3"), tools, tmp_path / "exchange")
    runtime.command({"contract": "ai-trading-tool-manager-command/v1", "command_id": "disable-1", "type": "disable", "capability": "generic_web_read"})
    try:
        ToolCatalog(tools).resolve("generic_web_read")
    except ToolLookupError as error:
        assert error.code == "tool_disabled"
    else:
        raise AssertionError("disabled tool resolved")
