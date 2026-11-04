"""Owner admin auth API (hidden from public OpenAPI docs)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from app.core.admin_auth import (
    SESSION_COOKIE,
    hash_password,
    hash_token,
    login_limiter,
    new_csrf_token,
    new_session_token,
    utcnow_iso,
    verify_password,
)
from app.core.config import settings
from app.deps import require_admin_session
from app.store import connect as store_connect

router = APIRouter(prefix="/admin/api", tags=["admin-auth"], include_in_schema=False)


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateAdminBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = _client_ip(request)
    ok, retry_after = login_limiter.allowed(ip, settings.login_max_attempts, settings.login_window_s)
    if not ok:
        return _err(429, "Too many login attempts; retry later", retry_after=retry_after)
    with store_connect() as db:
        row = db.execute("SELECT * FROM admin_users WHERE username=?",
                         (body.username.strip(),)).fetchone()
        user = dict(row) if row else None
    if user is None or not verify_password(user["pw_hash"], body.password):
        login_limiter.hit(ip)
        return _err(401, "Invalid username or password")
    token = new_session_token()
    csrf = new_csrf_token()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=settings.admin_session_ttl_s)
    with store_connect() as db:
        db.execute(
            """INSERT INTO admin_sessions (admin_id, token_hash, csrf_token,
               created_at, expires_at, ip) VALUES (?, ?, ?, ?, ?, ?)""",
            (user["id"], hash_token(token), csrf, now.isoformat(), exp.isoformat(), ip),
        )
        # Opportunistic cleanup of expired sessions.
        db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now.isoformat(),))
    response.set_cookie(
        SESSION_COOKIE, token, max_age=settings.admin_session_ttl_s,
        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
    )
    return {"ok": True, "username": user["username"], "csrf_token": csrf}


@router.post("/logout")
async def logout(request: Request, response: Response,
                 _admin: dict = Depends(require_admin_session)):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with store_connect() as db:
            db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (hash_token(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(admin: dict = Depends(require_admin_session)):
    return {"ok": True, "username": admin["username"], "csrf_token": admin["csrf_token"]}


@router.get("/admins")
async def list_admins(_admin: dict = Depends(require_admin_session)):
    with store_connect() as db:
        rows = db.execute("SELECT id, username, created_at FROM admin_users ORDER BY id").fetchall()
    return {"ok": True, "admins": [dict(r) for r in rows]}


@router.post("/admins", status_code=201)
async def create_admin(body: CreateAdminBody, request: Request,
                       _admin: dict = Depends(require_admin_session)):
    csrf = request.headers.get("x-csrf-token", "")
    if not csrf or csrf != _admin.get("csrf_token"):
        return _err(403, "Invalid CSRF token")
    try:
        pw_hash = hash_password(body.password)
    except ValueError as exc:
        return _err(400, str(exc))
    try:
        with store_connect() as db:
            cur = db.execute(
                "INSERT INTO admin_users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                (body.username.strip(), pw_hash, utcnow_iso()),
            )
            new_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return _err(409, "Username already exists")
    return {"ok": True, "id": new_id, "username": body.username.strip()}


def _err(status: int, message: str, retry_after: int | None = None):
    from fastapi.responses import JSONResponse
    headers = {"Retry-After": str(retry_after)} if retry_after else None
    return JSONResponse(status_code=status, content={"ok": False, "error": message}, headers=headers)
