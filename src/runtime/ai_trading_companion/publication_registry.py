from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def publication_registry() -> dict[str, Any]:
    package = Path(__file__).resolve()
    candidates = (
        package.parents[3] / "resources" / "contracts" / "companion-message-publication-registry-v2.json",
        package.parents[2] / "resources" / "contracts" / "companion-message-publication-registry-v2.json",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        raise RuntimeError("companion message publication registry is missing")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if value.get("contract") != "companion-message-publication-registry/v2":
        raise RuntimeError("companion message publication registry contract is invalid")
    return value


def published_event_types() -> frozenset[str]:
    return frozenset(str(value) for value in publication_registry()["published_event_types"])
