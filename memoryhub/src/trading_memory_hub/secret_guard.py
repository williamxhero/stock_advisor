from __future__ import annotations

import re


class SecretRejected(RuntimeError):
    pass


_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|sk-proj|api[_-]?key)[_-]?[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\b(?:password|passwd|token|secret|cookie)\s*[:=]\s*\S{8,}", re.I),
    re.compile(r"(?:助记词|seed phrase|mnemonic)\s*[:：]\s*\S+", re.I),
)


def assert_safe(value: str) -> None:
    if any(pattern.search(value) for pattern in _PATTERNS):
        raise SecretRejected("secret guard blocked MemoryHub episode")

