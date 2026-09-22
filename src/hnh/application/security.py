from __future__ import annotations

import hashlib
import re
from typing import Any

SECRET_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "opaque_state",
    "password",
    "secret",
    "token",
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)


def scope_fingerprint(scopes: frozenset[str]) -> str:
    return hashlib.sha256("\0".join(sorted(scopes)).encode()).hexdigest()


def redact(value: Any) -> Any:
    """Remove secret values from data headed to context, events, or exports."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            result[str(key)] = (
                "[REDACTED]"
                if any(part in normalized for part in SECRET_KEY_PARTS)
                else redact(item)
            )
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        text_result = value
        for pattern in SECRET_PATTERNS:
            text_result = pattern.sub("[REDACTED]", text_result)
        return text_result
    return value
