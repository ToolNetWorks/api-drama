"""Customer gateway: /v1/reelshort/* (ToolNet API key, envelope, logging)."""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from app.core.envelope import fail, ok
from app.deps import ensure_provider, log_request, require_customer
from app.providers.base import UpstreamError
from app.providers.rapidapi_reelshort import get_reelshort_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/reelshort", tags=["reelshort-v1"])


def _req_id(request: Request, customer: dict) -> str:
    return getattr(request.state, "request_id", customer.get("request_id", "n/a"))


def _conn(data: Any) -> int | None:
    cid = data.get("connection_id") if isinstance(data, dict) else None
    return cid if isinstance(cid, int) else None


@router.get("/search")
async def search(request: Request, q: str = Query("", max_length=200),
                 limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0),
                 customer: dict[str, Any] = Depends(require_customer)):
    ensure_provider(customer, "reelshort")
    rid = _req_id(request, customer)
    start = time.monotonic()
    try:
        data = await get_reelshort_provider().search(q, limit, offset)
        log_request(rid, customer["id"], "GET", "/v1/reelshort/search", 200,
                    int((time.monotonic() - start) * 1000),
                    connection_id=_conn(data), provider="reelshort")
        return JSONResponse(status_code=200, content=ok(data, rid))
    except UpstreamError as exc:
        log_request(rid, customer["id"], "GET", "/v1/reelshort/search", exc.status,
                    int((time.monotonic() - start) * 1000), provider="reelshort")
        return JSONResponse(status_code=exc.status,
                            content=fail(exc.code, exc.message, rid, exc.status))
    except Exception as exc:
        logger.exception("Failed in search: %s", exc)
        return JSONResponse(status_code=500,
                            content=fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", rid, 500))


@router.get("/series/{series_id}")
async def series_detail(request: Request, series_id: str,
                        customer: dict[str, Any] = Depends(require_customer)):
    ensure_provider(customer, "reelshort")
    rid = _req_id(request, customer)
    start = time.monotonic()
    try:
        data = await get_reelshort_provider().get_series(series_id)
        if data is None:
            log_request(rid, customer["id"], "GET", f"/v1/reelshort/series/{series_id[:60]}",
                        404, int((time.monotonic() - start) * 1000), provider="reelshort")
            return JSONResponse(status_code=404,
                                content=fail("NOT_FOUND", "Series not found", rid, 404))
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id", 200,
                    int((time.monotonic() - start) * 1000),
                    connection_id=_conn(data), provider="reelshort")
        return JSONResponse(status_code=200, content=ok(data, rid))
    except UpstreamError as exc:
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id", exc.status,
                    int((time.monotonic() - start) * 1000), provider="reelshort")
        return JSONResponse(status_code=exc.status,
                            content=fail(exc.code, exc.message, rid, exc.status))
    except Exception as exc:
        logger.exception("Failed in series_detail: %s", exc)
        return JSONResponse(status_code=500,
                            content=fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", rid, 500))


@router.get("/series/{series_id}/episodes")
async def episode_list(request: Request, series_id: str,
                       limit: int = Query(200, ge=1, le=500),
                       offset: int = Query(0, ge=0),
                       customer: dict[str, Any] = Depends(require_customer)):
    """Ordered episode roster from Turso. No video URLs, no upstream fan-out.

    404 when the series is unknown to the cache (no verified upstream list
    endpoint to fall back to, and no episode IDs are invented).
    """
    ensure_provider(customer, "reelshort")
    rid = _req_id(request, customer)
    start = time.monotonic()
    try:
        data = await get_reelshort_provider().list_series_episodes(
            series_id, limit=limit, offset=offset)
        if data is None:
            log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes",
                        404, int((time.monotonic() - start) * 1000), provider="reelshort")
            return JSONResponse(status_code=404,
                                content=fail("NOT_FOUND", "Series not found in episode cache", rid, 404))
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes", 200,
                    int((time.monotonic() - start) * 1000),
                    connection_id=None, provider="reelshort")
        return JSONResponse(status_code=200, content=ok(data, rid))
    except UpstreamError as exc:
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes",
                    exc.status, int((time.monotonic() - start) * 1000), provider="reelshort")
        return JSONResponse(status_code=exc.status,
                            content=fail(exc.code, exc.message, rid, exc.status))
    except Exception as exc:
        logger.exception("Failed in episode_list: %s", exc)
        return JSONResponse(status_code=500,
                            content=fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", rid, 500))


@router.get("/series/{series_id}/episodes/{episode_id}")
async def episode_detail(request: Request, series_id: str, episode_id: str,
                         customer: dict[str, Any] = Depends(require_customer)):
    ensure_provider(customer, "reelshort")
    rid = _req_id(request, customer)
    start = time.monotonic()
    try:
        data = await get_reelshort_provider().get_episode(series_id, episode_id)
        if data is None:
            log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes/:eid",
                        404, int((time.monotonic() - start) * 1000), provider="reelshort")
            return JSONResponse(status_code=404,
                                content=fail("NOT_FOUND", "Series or episode not found", rid, 404))
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes/:eid", 200,
                    int((time.monotonic() - start) * 1000),
                    connection_id=_conn(data), provider="reelshort")
        return JSONResponse(status_code=200, content=ok(data, rid))
    except UpstreamError as exc:
        log_request(rid, customer["id"], "GET", "/v1/reelshort/series/:id/episodes/:eid",
                    exc.status, int((time.monotonic() - start) * 1000), provider="reelshort")
        return JSONResponse(status_code=exc.status,
                            content=fail(exc.code, exc.message, rid, exc.status))
    except Exception as exc:
        logger.exception("Failed getting episode %s/%s: %s", series_id, episode_id, exc)
        return JSONResponse(status_code=500,
                            content=fail("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}", rid, 500))
