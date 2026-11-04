"""Owner-only admin auth: Argon2id passwords, opaque session tokens, CSRF.

- Passwords: Argon2id PHC hashes (argon2-cffi PasswordHasher defaults).
- Sessions: 32-byte random token, SHA-256 hash stored in DB, cookie is
  HttpOnly + SameSite=Lax (+ Secure in production behind HTTPS).
- CSRF: per-session token, required as X-CSRF-Token header on mutations.
- Login rate limiting: in-memory per-IP sliding window (fail-closed 429).
No admin token / customer login exists by design.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerifyMismatchError

_ph = PasswordHasher()  # Argon2id by default

SESSION_COOKIE = "tn_admin_session"


def hash_password(plain: str) -> str:
    if not plain or len(plain) < 8:
        raise ValueError("password must be at least 8 characters")
    return _ph.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    try:
        return _ph.verify(stored_hash, plain or "")
    except (VerifyMismatchError, InvalidHash, AttributeError, TypeError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _ph.check_needs_rehash(stored_hash)
    except (InvalidHash, AttributeError):
        return True


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LoginRateLimiter:
    """Per-IP sliding window for /admin/api/login."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, ip: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._hits[ip].append(now)

    def allowed(self, ip: str, max_attempts: int, window_s: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[ip]
            while dq and now - dq[0] > window_s:
                dq.popleft()
            if len(dq) >= max(1, max_attempts):
                retry_after = int(window_s - (now - dq[0])) + 1 if dq else 1
                return False, max(1, retry_after)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter = LoginRateLimiter()
