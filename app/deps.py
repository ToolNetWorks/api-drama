"""Auth dependencies: customer X-API-Key + owner session (no customer dashboard).

Customer errors use detail prefix "CODE: message" so the app exception
handler can render the standard envelope {success, data, error, request_id}.
Admin area uses opaque session cookies (HttpOnly, SameSite) + CSRF header.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import Header, HTTPException, Request

from app.core.admin_auth import SESSION_COOKIE, hash_token
from app.core.config import settings
from app.core.rate_limit import limiter
from app.store import connect as store_connect, utcnow


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _day_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _month_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


def _expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        dt = datetime.fromisoformat(expires_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= dt
    except ValueError:
        return False


async def require_customer(request: Request,
                           x_api_key: Optional[str] = Header(default=None)) -> dict[str, Any]:
    request_id = getattr(request.state, "request_id", new_request_id())
    if not x_api_key or not x_api_key.strip():
        raise HTTPException(status_code=401, detail="KEY_MISSING: Missing X-API-Key")
    digest = _hash(x_api_key.strip())
    try:
        with store_connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE key_hash=?", (digest,)).fetchone()
    except sqlite3.OperationalError:
        raise HTTPException(status_code=503, detail="UPSTREAM_ERROR: Service storage unavailable")
    if row is None or not row["is_active"] or row["revoked"]:
        raise HTTPException(status_code=401, detail="KEY_INVALID: Invalid or revoked API key")
    key = dict(row)
    if _expired(key.get("expires_at")):
        raise HTTPException(status_code=401, detail="KEY_EXPIRED: API key has expired")
    rpm = int(key.get("rpm") or settings.rate_limit_default_rpm)
    allowed, retry_after = limiter.check_and_hit(int(key["id"]), rpm)
    if not allowed:
        raise HTTPException(status_code=429,
                            detail=f"RATE_LIMITED: Rate limit exceeded; retry in {retry_after}s")
    try:
        with store_connect() as db:
            if key.get("daily_quota") is not None:
                n = db.execute(
                    "SELECT COUNT(*) FROM request_logs WHERE key_id=? AND created_at>=?",
                    (key["id"], _day_start_iso())).fetchone()[0]
                if n >= int(key["daily_quota"]):
                    raise HTTPException(status_code=429, detail="QUOTA_EXCEEDED: Daily quota exceeded")
            if key.get("monthly_quota") is not None:
                n = db.execute(
                    "SELECT COUNT(*) FROM request_logs WHERE key_id=? AND created_at>=?",
                    (key["id"], _month_start_iso())).fetchone()[0]
                if n >= int(key["monthly_quota"]):
                    raise HTTPException(status_code=429, detail="QUOTA_EXCEEDED: Monthly quota exceeded")
    except HTTPException:
        raise
    except sqlite3.OperationalError:
        pass
    try:
        with store_connect() as db:
            db.execute("UPDATE api_keys SET last_used_at=? WHERE id=?", (utcnow(), key["id"]))
    except sqlite3.OperationalError:
        pass
    key["request_id"] = request_id
    return key


def ensure_provider(customer: dict[str, Any], provider: str) -> None:
    """Enforce per-customer provider rights (403 PROVIDER_FORBIDDEN).

    allowed_providers NULL (legacy keys) means all providers.
    """
    import json as _json

    raw = customer.get("allowed_providers")
    if raw is None:
        return
    try:
        allowed = _json.loads(raw) if isinstance(raw, str) else list(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(allowed, list) or provider not in [str(p).lower() for p in allowed]:
        raise HTTPException(status_code=403,
                            detail=f"PROVIDER_FORBIDDEN: Key has no access to provider '{provider}'")


def get_admin_session(request: Request) -> Optional[dict[str, Any]]:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    try:
        with store_connect() as db:
            row = db.execute(
                """SELECT s.*, u.username FROM admin_sessions s
                   JOIN admin_users u ON u.id = s.admin_id
                   WHERE s.token_hash=?""", (hash_token(token),)).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= exp:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return dict(row)


async def require_admin_session(request: Request) -> dict[str, Any]:
    session = get_admin_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="ADMIN_AUTH: Admin login required")
    return session


async def require_csrf(request: Request,
                       x_csrf_token: Optional[str] = Header(default=None)) -> dict[str, Any]:
    session = await require_admin_session(request)
    csrf = (x_csrf_token or "").strip()
    if not csrf or csrf != session.get("csrf_token"):
        raise HTTPException(status_code=403, detail="ADMIN_CSRF: Invalid CSRF token")
    return session


def log_request(request_id: str, key_id: Optional[int], method: str, path: str,
                status: int, latency_ms: int = 0,
                connection_id: Optional[int] = None, provider: Optional[str] = None,
                upstream_status: Optional[int] = None) -> None:
    try:
        with store_connect() as db:
            db.execute(
                """INSERT INTO request_logs
                   (request_id, key_id, method, path, status, upstream_status,
                    latency_ms, created_at, connection_id, provider)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (request_id, key_id, method, path[:300], status, upstream_status,
                 latency_ms, utcnow(), connection_id, provider),
            )
    except sqlite3.OperationalError:
        pass  # logging must never break the request
