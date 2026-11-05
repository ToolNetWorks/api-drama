"""Owner customer-key management API (hidden from public docs).

Full key material is returned exactly once at creation. List/detail views
expose only prefix + metadata. Mutations require session + CSRF.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.security import generate_raw_key, hash_key, key_prefix
from app.deps import require_admin_session, require_csrf
from app.store import connect as store_connect, utcnow

router = APIRouter(prefix="/admin/api/keys", tags=["admin-keys"], include_in_schema=False)

PROVIDERS = ("reelshort", "dramabox", "netshort", "shortmax")


def _validate_providers(raw: Any) -> Optional[str]:
    """Validate provider list, return JSON to store (None keeps existing)."""
    import json as _json

    if raw is None:
        return None
    cleaned = sorted({str(p).strip().lower() for p in raw if str(p).strip().lower() in PROVIDERS})
    if not cleaned:
        return _err(400, f"allowed_providers must be a non-empty subset of {', '.join(PROVIDERS)}")
    return _json.dumps(cleaned)


def _parse_providers(row: Any) -> Optional[list]:
    import json as _json

    # Handle both sqlite3.Row (key access) and dict/RealDictRow (get method)
    try:
        raw = row["allowed_providers"]
    except (KeyError, TypeError):
        raw = row.get("allowed_providers") if hasattr(row, "get") else None
    if raw is None:
        return None  # legacy key: all providers
    try:
        parsed = _json.loads(raw)
        return parsed if isinstance(parsed, list) else None
    except (ValueError, TypeError):
        return None


class CreateKeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    rpm: int = Field(default=60, ge=1, le=10_000)
    daily_quota: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    monthly_quota: Optional[int] = Field(default=None, ge=1, le=100_000_000)
    expires_at: Optional[str] = Field(default=None, max_length=64)
    allowed_providers: Optional[list] = Field(default=None)


class UpdateKeyBody(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    rpm: Optional[int] = Field(default=None, ge=1, le=10_000)
    daily_quota: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    monthly_quota: Optional[int] = Field(default=None, ge=1, le=100_000_000)
    expires_at: Optional[str] = Field(default=None, max_length=64)
    is_active: Optional[bool] = None
    allowed_providers: Optional[list] = Field(default=None)


def _validate_expiry(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return _err(400, "expires_at must be ISO-8601 (e.g. 2027-01-01T00:00:00+00:00)")
    return dt.isoformat()


def _public(row: Any) -> dict[str, Any]:
    d = dict(row)
    d.pop("key_hash", None)
    d["is_active"] = bool(d.get("is_active"))
    d["revoked"] = bool(d.get("revoked"))
    d["allowed_providers"] = _parse_providers(row)
    return d


@router.get("")
async def list_keys(_a: dict = Depends(require_admin_session)):
    with store_connect() as db:
        try:
            rows = db.execute(
                "SELECT * FROM api_keys ORDER BY id DESC").fetchall()
        except Exception:
            return {"ok": True, "keys": []}
    out = []
    for r in rows:
        d = _public(r)
        d.update(_usage(r["id"]))
        out.append(d)
    return {"ok": True, "keys": out}


@router.post("", status_code=201)
async def create_key(body: CreateKeyBody, _a: dict = Depends(require_csrf)):
    expires = _validate_expiry(body.expires_at)
    if isinstance(expires, JSONResponse):
        return expires
    raw = generate_raw_key()
    now = utcnow()
    providers_json = _validate_providers(body.allowed_providers)
    if isinstance(providers_json, JSONResponse):
        return providers_json
    with store_connect() as db:
        if settings.use_postgres:
            cur = db.execute(
                """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked,
                   rpm, daily_quota, monthly_quota, created_at, last_used_at, expires_at,
                   allowed_providers)
                   VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, NULL, ?,
                   COALESCE(?, '["reelshort"]'))
                   RETURNING id""",
                (body.name.strip(), key_prefix(raw), hash_key(raw), body.rpm,
                 body.daily_quota, body.monthly_quota, now, expires, providers_json),
            )
            new_id = cur.fetchone()["id"]
        else:
            cur = db.execute(
                """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked,
                   rpm, daily_quota, monthly_quota, created_at, last_used_at, expires_at,
                   allowed_providers)
                   VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?, NULL, ?,
                   COALESCE(?, '["reelshort"]'))""",
                (body.name.strip(), key_prefix(raw), hash_key(raw), body.rpm,
                 body.daily_quota, body.monthly_quota, now, expires, providers_json),
            )
            new_id = cur.lastrowid
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (new_id,)).fetchone()
    return {"ok": True, "api_key": raw, "warning": "Copy now — the full key is never shown again.",
            "key": _public(row)}


@router.get("/{key_id}")
async def get_key(key_id: int, _a: dict = Depends(require_admin_session)):
    with store_connect() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            return _err(404, "Key not found")
        d = _public(row)
        d.update(_usage(key_id))
    return {"ok": True, "key": d}


@router.patch("/{key_id}")
async def update_key(key_id: int, body: UpdateKeyBody, _a: dict = Depends(require_csrf)):
    expires = _validate_expiry(body.expires_at)
    if isinstance(expires, JSONResponse):
        return expires
    with store_connect() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            return _err(404, "Key not found")
        updates: dict[str, Any] = {}
        if body.name is not None:
            updates["name"] = body.name.strip()
        if body.rpm is not None:
            updates["rpm"] = body.rpm
        if body.daily_quota is not None:
            updates["daily_quota"] = body.daily_quota
        if body.monthly_quota is not None:
            updates["monthly_quota"] = body.monthly_quota
        if body.expires_at is not None:
            updates["expires_at"] = expires
        if body.is_active is not None:
            updates["is_active"] = 1 if body.is_active else 0
        if body.allowed_providers is not None:
            providers_json = _validate_providers(body.allowed_providers)
            if isinstance(providers_json, JSONResponse):
                return providers_json
            updates["allowed_providers"] = providers_json
        if updates:
            sets = ", ".join(f"{k}=?" for k in updates)
            db.execute(f"UPDATE api_keys SET {sets} WHERE id=?", (*updates.values(), key_id))
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
    return {"ok": True, "key": _public(row)}


@router.post("/{key_id}/revoke")
async def revoke_key(key_id: int, _a: dict = Depends(require_csrf)):
    with store_connect() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            return _err(404, "Key not found")
        db.execute("UPDATE api_keys SET revoked=1, is_active=0 WHERE id=?", (key_id,))
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
    return {"ok": True, "key": _public(row)}


@router.post("/{key_id}/enable")
async def enable_key(key_id: int, _a: dict = Depends(require_csrf)):
    return _set_active(key_id, True)


@router.post("/{key_id}/disable")
async def disable_key(key_id: int, _a: dict = Depends(require_csrf)):
    return _set_active(key_id, False)


def _set_active(key_id: int, active: bool):
    with store_connect() as db:
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            return _err(404, "Key not found")
        db.execute("UPDATE api_keys SET is_active=? WHERE id=?", (1 if active else 0, key_id))
        row = db.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
    return {"ok": True, "key": _public(row)}


def _usage(key_id: int) -> dict[str, Any]:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    try:
        with store_connect() as db:
            total = db.execute("SELECT COUNT(*) FROM request_logs WHERE key_id=?", (key_id,)).fetchone()[0]
            today = db.execute("SELECT COUNT(*) FROM request_logs WHERE key_id=? AND created_at>=?",
                               (key_id, day_start)).fetchone()[0]
            month = db.execute("SELECT COUNT(*) FROM request_logs WHERE key_id=? AND created_at>=?",
                               (key_id, month_start)).fetchone()[0]
    except Exception:
        total = today = month = 0
    _ = settings  # keep import used (rate defaults documented in dashboard)
    return {"usage_total": total, "usage_today": today, "usage_month": month}


def _err(status: int, message: str):
    return JSONResponse(status_code=status, content={"ok": False, "error": message})