"""Owner provider-connection management API (hidden from public docs).

Connections store RapidAPI keys encrypted (Fernet). Reads return masked
hints only. Mutations require session + CSRF.
"""
from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.crypto import decrypt_key, encrypt_key, mask_key
from app.deps import require_admin_session, require_csrf
from app.providers import pool
from app.store import connect as store_connect, next_quota_reset, utcnow

router = APIRouter(prefix="/admin/api/providers", tags=["admin-providers"],
                   include_in_schema=False)

PROVIDERS = ("reelshort", "dramabox", "netshort", "shortmax")


def _provider_or_400(provider: str) -> str:
    p = (provider or "").lower()
    if p not in PROVIDERS:
        raise _err(400, f"Unknown provider '{provider}'. Expected one of {', '.join(PROVIDERS)}")
    return p


class CreateConnectionBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: str = Field(min_length=8, max_length=512)
    quota_limit: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    quota_window: str = Field(default="monthly", max_length=16)
    priority: int = Field(default=100, ge=1, le=10_000)
    enabled: bool = True


class UpdateConnectionBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    quota_limit: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    quota_window: Optional[str] = Field(default=None, max_length=16)
    priority: Optional[int] = Field(default=None, ge=1, le=10_000)
    enabled: Optional[bool] = None


class RotateKeyBody(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)


class SettingsBody(BaseModel):
    strategy: str = Field(max_length=32)
    rotation_enabled: bool = True
    cooldown_seconds: int = Field(ge=10, le=3600)


def _master() -> str:
    master = os.getenv("TOOLNET_MASTER_KEY", "").strip()
    if not master:
        raise _err(500, "TOOLNET_MASTER_KEY is not set; cannot store upstream keys")
    return master


@router.get("/{provider}/connections")
async def list_connections(provider: str, _a: dict = Depends(require_admin_session)):
    p = _provider_or_400(provider)
    with store_connect() as db:
        cfg = pool.get_settings(db, p)
        rows = db.execute(
            "SELECT * FROM provider_connections WHERE provider=? ORDER BY priority, id", (p,)).fetchall()
    return {"ok": True, "provider": p, "settings": cfg,
            "connections": [pool.public_view(r) for r in rows]}


@router.post("/{provider}/connections", status_code=201)
async def create_connection(provider: str, body: CreateConnectionBody,
                            _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    window = (body.quota_window or "monthly").lower()
    if window not in ("daily", "monthly"):
        return _err(400, "quota_window must be daily or monthly")
    master = _master()
    raw = body.api_key.strip()
    try:
        enc = encrypt_key(raw, master)
    except ValueError as exc:
        return _err(400, str(exc))
    now = utcnow()
    try:
        with store_connect() as db:
            cur = db.execute(
                """INSERT INTO provider_connections
                   (provider, name, key_encrypted, key_hint, enabled, status, priority,
                    quota_limit, quota_window, requests_used, quota_reset_at,
                    consecutive_failures, created_at)
                   VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, 0, ?, 0, ?)""",
                (p, body.name.strip(), enc, mask_key(raw), 1 if body.enabled else 0,
                 body.priority, body.quota_limit, window, next_quota_reset(window), now),
            )
            row = db.execute("SELECT * FROM provider_connections WHERE id=?",
                             (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError:
        return _err(409, f"Connection name '{body.name.strip()}' already exists for {p}")
    return {"ok": True, "connection": pool.public_view(row)}


@router.patch("/{provider}/connections/{conn_id}")
async def update_connection(provider: str, conn_id: int, body: UpdateConnectionBody,
                            _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        updates: dict[str, Any] = {}
        if body.name is not None:
            updates["name"] = body.name.strip()
        if body.quota_limit is not None:
            updates["quota_limit"] = body.quota_limit
        if body.quota_window is not None:
            w = body.quota_window.lower()
            if w not in ("daily", "monthly"):
                return _err(400, "quota_window must be daily or monthly")
            updates["quota_window"] = w
            updates["quota_reset_at"] = next_quota_reset(w)
            updates["requests_used"] = 0
        if body.priority is not None:
            updates["priority"] = body.priority
        if body.enabled is not None:
            updates["enabled"] = 1 if body.enabled else 0
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            try:
                db.execute(f"UPDATE provider_connections SET {sets} WHERE id=?",
                           (*updates.values(), conn_id))
            except sqlite3.IntegrityError:
                return _err(409, "Connection name already exists for this provider")
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
    return {"ok": True, "connection": pool.public_view(row)}


@router.post("/{provider}/connections/{conn_id}/key")
async def rotate_key(provider: str, conn_id: int, body: RotateKeyBody,
                     _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    master = _master()
    raw = body.api_key.strip()
    try:
        enc = encrypt_key(raw, master)
    except ValueError as exc:
        return _err(400, str(exc))
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        db.execute(
            """UPDATE provider_connections SET key_encrypted=?, key_hint=?,
               status='active', cooldown_until=NULL, consecutive_failures=0,
               last_error=NULL, last_check_at=NULL, last_check_ok=NULL WHERE id=?""",
            (enc, mask_key(raw), conn_id),
        )
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
    return {"ok": True, "connection": pool.public_view(row)}


@router.post("/{provider}/connections/{conn_id}/enable")
async def enable_conn(provider: str, conn_id: int, _a: dict = Depends(require_csrf)):
    return _set_enabled(provider, conn_id, True)


@router.post("/{provider}/connections/{conn_id}/disable")
async def disable_conn(provider: str, conn_id: int, _a: dict = Depends(require_csrf)):
    return _set_enabled(provider, conn_id, False)


def _set_enabled(provider: str, conn_id: int, enabled: bool):
    p = _provider_or_400(provider)
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        db.execute("UPDATE provider_connections SET enabled=? WHERE id=?",
                   (1 if enabled else 0, conn_id))
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
    return {"ok": True, "connection": pool.public_view(row)}


@router.post("/{provider}/connections/{conn_id}/clear-error")
async def clear_error(provider: str, conn_id: int, _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        pool.clear_error(db, conn_id)
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
    return {"ok": True, "connection": pool.public_view(row)}


@router.delete("/{provider}/connections/{conn_id}")
async def delete_connection(provider: str, conn_id: int, _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        db.execute("DELETE FROM provider_connections WHERE id=?", (conn_id,))
    return {"ok": True, "deleted": conn_id}


@router.put("/{provider}/settings")
async def update_settings(provider: str, body: SettingsBody, _a: dict = Depends(require_csrf)):
    p = _provider_or_400(provider)
    if body.strategy not in ("round_robin", "fill_first"):
        return _err(400, "strategy must be round_robin or fill_first")
    with store_connect() as db:
        cfg = pool.upsert_settings(db, p, body.strategy, body.rotation_enabled,
                                   body.cooldown_seconds)
    return {"ok": True, "settings": cfg}


@router.post("/{provider}/connections/{conn_id}/test")
async def test_connection(provider: str, conn_id: int, request: Request,
                          _a: dict = Depends(require_csrf)):
    """Test ONE connection (no rotation).

    Mode A — real endpoint (when RAPIDAPI_REELSHORT_EPISODES_PATH + test IDs
    are configured): single GET against the verified GetEpisode path.
    HTTP 200 → endpoint_ok=1, healthy. HTTP 404 → endpoint_ok=0, NOT healthy.
    401/403 → key rejected (auth_error). 429/5xx/timeout → cooling.
    Mode B — key probe (no verified path): unknown path only proves key
    acceptance (401/403 = rejected, else accepted). Never marks healthy.
    HTTP 200 means the check ran; `ok` is true only on verified 2xx.
    Each test consumes upstream quota (counted, reported).
    """
    p = _provider_or_400(provider)
    _ = request
    if p != "reelshort":
        return _err(400, f"Connection test is not implemented for provider '{p}' yet")
    if not settings.rapid_host:
        return _err(500, "RAPIDAPI_REELSHORT_HOST is not configured")
    master = _master()
    with store_connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=? AND provider=?",
                         (conn_id, p)).fetchone()
        if row is None:
            return _err(404, "Connection not found")
        try:
            raw = decrypt_key(row["key_encrypted"], master)
        except ValueError as exc:
            pool.record_auth_failure(db, conn_id, "decrypt failed")
            _stamp_check(db, conn_id, False)
            return _err(500, str(exc))
    if settings.upstream_episodes_path and settings.rapid_test_series_id \
            and settings.rapid_test_episode_id:
        url = (f"https://{settings.rapid_host}"
               + settings.upstream_episodes_path.replace("{id}", settings.rapid_test_series_id)
               .replace("{episode_id}", settings.rapid_test_episode_id))
        mode = "endpoint"
    else:
        url = f"https://{settings.rapid_host}/__toolnet_key_check__"
        mode = "key-probe"
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"X-RapidAPI-Host": settings.rapid_host, "X-RapidAPI-Key": raw,
                         "Accept": "application/json", "User-Agent": "ToolNet-Drama-Gateway/1.0"},
            )
        latency_ms = int((time.monotonic() - start) * 1000)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        with store_connect() as db:
            cfg = pool.get_settings(db, p)
            pool.record_transient_failure(db, conn_id, type(exc).__name__,
                                          int(cfg.get("cooldown_seconds") or 300))
            _stamp_check(db, conn_id, False)
        return {"ok": False, "mode": mode, "reachable": False, "key_valid": None,
                "endpoint_ok": None,
                "latency_ms": int((time.monotonic() - start) * 1000),
                "detail": f"Network error: {type(exc).__name__}. Key validity unknown."}
    finally:
        raw = ""
    with store_connect() as db:
        if resp.status_code in (401, 403):
            pool.record_auth_failure(db, conn_id, f"upstream {resp.status_code}")
            db.execute("UPDATE provider_connections SET key_valid=0 WHERE id=?", (conn_id,))
            _stamp_check(db, conn_id, False)
            row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
            return {"ok": False, "mode": mode, "reachable": True, "key_valid": False,
                    "endpoint_ok": None,
                    "status": resp.status_code, "latency_ms": latency_ms,
                    "detail": "Upstream rejected the key (401/403). NOT marked healthy.",
                    "connection": pool.public_view(row)}
        if mode == "endpoint":
            if resp.status_code == 200:
                pool.record_success(db, conn_id)
                pool.mark_endpoint(db, conn_id, True)
                pool.sync_quota_from_headers(db, conn_id, resp.headers)
                pool.clear_error(db, conn_id)
                db.execute("UPDATE provider_connections SET key_valid=1 WHERE id=?", (conn_id,))
                _stamp_check(db, conn_id, True)
                row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
                return {"ok": True, "mode": mode, "reachable": True, "key_valid": True,
                        "endpoint_ok": 1, "status": 200, "latency_ms": latency_ms,
                        "detail": "Verified endpoint returned HTTP 200. Marked healthy.",
                        "connection": pool.public_view(row)}
            if resp.status_code == 404:
                pool.mark_endpoint(db, conn_id, False)
                pool.sync_quota_from_headers(db, conn_id, resp.headers)
                db.execute(
                    "UPDATE provider_connections SET requests_used=requests_used+1, "
                    "key_valid=1, last_used_at=? WHERE id=?", (utcnow(), conn_id))
                _stamp_check(db, conn_id, False)
                row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
                return {"ok": False, "mode": mode, "reachable": True, "key_valid": True,
                        "endpoint_ok": 0, "status": 404, "latency_ms": latency_ms,
                        "detail": "Endpoint does not exist (404). NOT marked healthy.",
                        "connection": pool.public_view(row)}
            cfg = pool.get_settings(db, p)
            pool.record_transient_failure(db, conn_id, f"upstream {resp.status_code}",
                                          int(cfg.get("cooldown_seconds") or 300))
            _stamp_check(db, conn_id, False)
            row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
            return {"ok": False, "mode": mode, "reachable": True, "key_valid": True,
                    "endpoint_ok": None, "status": resp.status_code, "latency_ms": latency_ms,
                    "detail": f"Upstream status {resp.status_code}; connection cooling. NOT marked healthy.",
                    "connection": pool.public_view(row)}
        # Key-probe mode: accepted key proves NO endpoint.
        pool.record_key_probe(db, conn_id, True)
        pool.sync_quota_from_headers(db, conn_id, resp.headers)
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (conn_id,)).fetchone()
    return {"ok": False, "mode": mode, "reachable": True, "key_valid": True, "endpoint_ok": None,
            "status": resp.status_code, "latency_ms": latency_ms,
            "detail": ("Key accepted by upstream gateway, but no endpoint is verified "
                       "(probe path is unknown by design). Connection NOT marked healthy."),
            "connection": pool.public_view(row)}


@router.get("/{provider}/stats")
async def connection_stats(provider: str, _a: dict = Depends(require_admin_session)):
    p = _provider_or_400(provider)
    with store_connect() as db:
        rows = db.execute("SELECT * FROM provider_connections WHERE provider=?", (p,)).fetchall()
        per_conn = []
        for r in rows:
            v = pool.public_view(r)
            day = db.execute(
                "SELECT COUNT(*) FROM request_logs WHERE connection_id=? AND created_at>=date('now')",
                (r["id"],)).fetchone()
            try:
                day_count = day[0] if day else 0
            except (sqlite3.OperationalError, TypeError, IndexError):
                day_count = 0
            v["requests_today_logged"] = day_count
            per_conn.append(v)
    return {"ok": True, "provider": p, "connections": per_conn}


def _stamp_check(db: sqlite3.Connection, conn_id: int, ok: bool) -> None:
    try:
        db.execute("UPDATE provider_connections SET last_check_at=?, last_check_ok=? WHERE id=?",
                   (utcnow(), 1 if ok else 0, conn_id))
    except sqlite3.OperationalError:
        pass


def _err(status: int, message: str):
    return JSONResponse(status_code=status, content={"ok": False, "error": message})
