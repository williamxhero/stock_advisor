from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from ai_trading_companion.paths import RuntimePaths


def test_installed_runtime_defaults_to_d_app() -> None:
    with mock.patch.dict(os.environ, {}, clear=True):
        paths = RuntimePaths.discover()

    assert paths.home == Path("D:/APP/AITradingCompanion").resolve()


def test_runtime_home_override_remains_available_for_isolated_runs(tmp_path: Path) -> None:
    with mock.patch.dict(os.environ, {"AI_TRADING_COMPANION_HOME": str(tmp_path)}):
        paths = RuntimePaths.discover()

    assert paths.home == tmp_path.resolve()
