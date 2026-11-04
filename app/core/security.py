"""ToolNet API key issuance + verification. Only hashes are stored."""
from __future__ import annotations

import hashlib
import secrets

KEY_PREFIX = "tn_live_"


def generate_raw_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def key_prefix(raw: str, length: int = 12) -> str:
    # Store a short non-secret prefix for operator lookup / log correlation.
    return raw[:length]


def redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return value[:4] + "***" + value[-2:]
