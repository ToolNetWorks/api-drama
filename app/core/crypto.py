"""Encrypted storage for upstream provider keys (Fernet, stdlib + cryptography).

The master key lives only in backend env TOOLNET_MASTER_KEY. Frontend and logs
only ever see masked hints (last 4 chars). Raw keys are decrypted in-memory
for the single outbound request and never returned, logged, or cached.
"""
from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


def _derive_fernet(master: str) -> Fernet:
    # Accept a Fernet urlsafe key directly, else derive one from arbitrary text.
    text = (master or "").strip()
    if text:
        try:
            Fernet(text.encode())
            return Fernet(text.encode())
        except (ValueError, TypeError):
            pass
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_master_key() -> str:
    return Fernet.generate_key().decode()


def encrypt_key(raw: str, master: str) -> str:
    if not raw:
        raise ValueError("empty api key")
    return _derive_fernet(master).encrypt(raw.encode("utf-8")).decode()


def decrypt_key(token: str, master: str) -> str:
    try:
        return _derive_fernet(master).decrypt(token.encode("utf-8")).decode()
    except InvalidToken as exc:
        raise ValueError("cannot decrypt connection key (wrong TOOLNET_MASTER_KEY?)") from exc


def mask_key(raw_or_hint: str) -> str:
    """Return a non-secret hint like ****a1b2 (last 4 chars only)."""
    s = (raw_or_hint or "").strip()
    if len(s) <= 4:
        return "****"
    return "****" + s[-4:]


def ensure_master_key() -> str:
    """Return configured master key or a process-local ephemeral one.

    Operators should set TOOLNET_MASTER_KEY persistently; without it, stored
    connections cannot be decrypted after restart (fail-closed with clear error).
    """
    master = os.getenv("TOOLNET_MASTER_KEY", "").strip()
    return master
