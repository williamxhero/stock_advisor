"""A fail-closed boundary for text that may leave this workstation.

The companion deliberately keeps useful non-secret history available to its
research calls.  That makes a small, deterministic secret boundary more
important than a vague "be careful" prompt.  This module is used before text
is persisted as a memory candidate and again before a packet is handed to a
cloud-capable runner.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class SecretFinding:
    category: str
    sample: str


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----", re.I)),
    ("api_key", re.compile(r"\b(?:sk|sk-proj|api[_-]?key)[_-]?[A-Za-z0-9_-]{16,}\b", re.I)),
    ("authorization", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}\b", re.I)),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("credential_assignment", re.compile(r"\b(?:password|passwd|token|secret|cookie)\s*[:=]\s*\S{8,}", re.I)),
    ("mnemonic", re.compile(r"(?:助记词|seed phrase|mnemonic)\s*[:：]\s*\S+", re.I)),
)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    frequencies = {character: value.count(character) / len(value) for character in set(value)}
    return -sum(probability * math.log2(probability) for probability in frequencies.values())


def find_secrets(text: str) -> list[SecretFinding]:
    findings: list[SecretFinding] = []
    for category, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            findings.append(SecretFinding(category, "[redacted]"))
    # High-entropy values are only suspicious when explicitly assigned.  This
    # avoids treating stock research prose or a URL as a secret.
    for match in re.finditer(r"\b(?:key|token|secret)\s*[:=]\s*([A-Za-z0-9+/=_-]{24,})", text, re.I):
        if _entropy(match.group(1)) >= 3.5:
            findings.append(SecretFinding("high_entropy_credential", "[redacted]"))
            break
    return findings


def assert_safe(text: str, *, boundary: str) -> None:
    findings = find_secrets(text)
    if findings:
        categories = ", ".join(sorted({item.category for item in findings}))
        raise ValueError(f"secret guard blocked {boundary}: {categories}")
