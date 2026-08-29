"""Stable locations for the installed product and its mutable local workspace."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PRODUCT = "AITradingCompanion"


def _local_app_data() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise RuntimeError("LOCALAPPDATA is required")
    return Path(value)


@dataclass(frozen=True)
class RuntimePaths:
    install_root: Path
    home: Path

    @classmethod
    def discover(cls) -> "RuntimePaths":
        package_root = Path(__file__).resolve().parent
        default_install = next(
            (candidate for candidate in package_root.parents if (candidate / "resources").is_dir()),
            package_root.parents[2],
        )
        install_root = Path(os.environ.get("AI_TRADING_COMPANION_INSTALL_ROOT", default_install)).resolve()
        home = Path(os.environ.get("AI_TRADING_COMPANION_HOME", _local_app_data() / PRODUCT)).resolve()
        return cls(install_root=install_root, home=home)

    @property
    def resources(self) -> Path:
        return self.install_root / "resources"

    @property
    def contracts(self) -> Path:
        return self.resources / "contracts"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def runtime(self) -> Path:
        return self.home / "runtime"

    @property
    def database(self) -> Path:
        return self.home / "data" / "trading-companion.sqlite3"

    @property
    def exchange(self) -> Path:
        return self.home / "exchange"

    @property
    def settings(self) -> Path:
        return self.home / "config" / "settings.local.json"

    @property
    def evidence(self) -> Path:
        return self.home / "evidence"

    def ensure(self) -> None:
        for directory in (
            self.home / "data", self.workspace / "portfolio", self.workspace / "state",
            self.workspace / "logs", self.workspace / "reports", self.runtime / "runs",
            self.runtime / "logs", self.runtime / "backups", self.exchange / "to-runtime" / "pending",
            self.exchange / "to-client" / "pending", self.home / "ui", self.home / "cache",
            self.home / "migration", self.home / "config",
            self.evidence, self.runtime / "downloads" / "quarantine",
        ):
            directory.mkdir(parents=True, exist_ok=True)
