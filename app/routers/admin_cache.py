"""Owner Episode Cache admin API (hidden from public docs).

Reads need an admin session; every mutation needs session + CSRF. Turso trouble is
reported in the JSON body and never breaks other admin pages (the HTML shell is static).
Secrets (Turso token, RapidAPI keys, signed video URL query strings) are never returned.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.deps import require_admin_session, require_csrf
from app.episode_cache import service as ec
from app.episode_cache.client import CacheBackendError
from app.providers.base import ProviderError

logger = logging.getLogger("toolnet.episode_cache.admin")

router = APIRouter(prefix="/admin/api/cache", tags=["admin-cache"], include_in_schema=False)

_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")


def _err(status: int, message: str, **extra):
    return JSONResponse(status_code=status, content={"ok": False, "error": message, **extra})


def _svc_or_503():
    svc = ec.get_service()
    if not svc.active:
        return svc, _err(503, "Episode cache is not active (disabled or Turso not configured)",
                         missing=svc.cfg.missing() if svc.cfg.enabled else None)
    return svc, None


def _backend_down(exc: Exception):
    return _err(503, f"Turso unavailable: {str(exc)[:200]}")


@router.get("/status")
async def status(_a: dict = Depends(require_admin_session)):
    svc = ec.get_service()
    out: dict = {"ok": True, "health": await svc.health(), "providers": sorted(ec.HOOKS),
                 "config": {"enabled": svc.cfg.enabled, "backend": svc.cfg.backend,
                            "metadata_ttl_s": svc.cfg.metadata_ttl_s,
                            "video_url_ttl_s": svc.cfg.video_url_ttl_s,
                            "fallback_to_upstream": svc.cfg.fallback_to_upstream,
                            "turso_url_set": bool(svc.cfg.database_url),
                            "turso_token_set": bool(svc.cfg.auth_token)},
                 "stats": None}
    if svc.active:
        try:
            out["stats"] = await svc.stats()
        except CacheBackendError as exc:
            out["stats_error"] = str(exc)[:200]
    return out


@router.get("/series")
async def series_list(provider: str = Query("", max_length=32), q: str = Query("", max_length=128),
                      limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000),
                      _a: dict = Depends(require_admin_session)):
    """Series catalog: one row per (provider, series_id) with episode counters."""
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        data = await svc.list_series_catalog(provider=provider.strip().lower(), q=q.strip(),
                                             limit=limit, offset=offset)
    except CacheBackendError as exc:
        return _backend_down(exc)
    return {"ok": True, **data, "now": int(time.time())}


@router.get("/series/{provider}/{series_id}")
async def series_detail(provider: str, series_id: str,
                        _a: dict = Depends(require_admin_session)):
    """One series + ordered episode roster (no video URLs in the list)."""
    if not _ID_RE.match(provider) or not _ID_RE.match(series_id):
        return _err(400, "Invalid provider or series_id")
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        series = await svc.get_series_catalog(provider.strip().lower(), series_id.strip())
        if series is None:
            return _err(404, "Series not found in cache")
        roster = await svc.list_series_episodes(provider.strip().lower(), series_id.strip(),
                                                limit=500, offset=0)
    except CacheBackendError as exc:
        return _backend_down(exc)
    return {"ok": True, "series": series, "episodes": roster["items"],
            "has_more_episodes": roster["has_more"], "now": int(time.time())}


@router.get("/entries")
async def entries(provider: str = Query("", max_length=32), q: str = Query("", max_length=128),
                  state: str = Query("", pattern="^(|fresh|expired)$"),
                  limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=1_000_000),
                  _a: dict = Depends(require_admin_session)):
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        data = await svc.list_rows(provider=provider.strip().lower(), q=q.strip(), state=state,
                                   limit=limit, offset=offset)
    except CacheBackendError as exc:
        return _backend_down(exc)
    return {"ok": True, **data, "now": int(time.time())}


@router.get("/entries/{row_id}")
async def entry_detail(row_id: int, _a: dict = Depends(require_admin_session)):
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        row = await svc.detail(row_id)
    except CacheBackendError as exc:
        return _backend_down(exc)
    if row is None:
        return _err(404, "Cache entry not found")
    return {"ok": True, "entry": row, "now": int(time.time())}


@router.post("/entries/{row_id}/refresh")
async def entry_refresh(row_id: int, _a: dict = Depends(require_csrf)):
    """Force one upstream fetch (consumes RapidAPI quota) and UPSERT the result."""
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        row = await svc.detail(row_id)
        if row is None:
            return _err(404, "Cache entry not found")
        if row["provider"] not in ec.HOOKS:
            return _err(400, f"No refresh hook for provider '{row['provider']}'")
        res = await svc.refresh(row["provider"], row["series_id"], row["episode_id"])
    except CacheBackendError as exc:
        return _backend_down(exc)
    except ProviderError as exc:
        return _err(exc.status if 400 <= exc.status < 600 else 502, f"{exc.code}: {exc.message}")
    logger.info("admin refreshed cache entry id=%s provider=%s admin=%s", row_id, row["provider"],
                _a.get("username"))
    return {"ok": True, "refreshed": True, "persisted": res.persisted, "source": res.source}


@router.delete("/entries/{row_id}")
async def entry_delete(row_id: int, _a: dict = Depends(require_csrf)):
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        n = await svc.delete(row_id)
    except CacheBackendError as exc:
        return _backend_down(exc)
    if not n:
        return _err(404, "Cache entry not found")
    logger.info("admin deleted cache entry id=%s admin=%s", row_id, _a.get("username"))
    return {"ok": True, "deleted": n}


class InvalidateSeriesBody(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    series_id: str = Field(min_length=1, max_length=128)


@router.post("/series/invalidate")
async def series_invalidate(body: InvalidateSeriesBody, _a: dict = Depends(require_csrf)):
    """Soft-invalidate every cached episode of a series (next request refreshes)."""
    provider = body.provider.strip().lower()
    series_id = body.series_id.strip()
    if not _ID_RE.match(series_id):
        return _err(400, "Invalid series_id")
    svc, bad = _svc_or_503()
    if bad:
        return bad
    try:
        n = await svc.invalidate_series(provider, series_id)
    except CacheBackendError as exc:
        return _backend_down(exc)
    logger.info("admin invalidated series provider=%s rows=%d admin=%s", provider, n,
                _a.get("username"))
    return {"ok": True, "invalidated": n}
