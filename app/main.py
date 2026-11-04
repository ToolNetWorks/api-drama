"""ToolNet Drama API: legacy ReelShort endpoints + /v1 gateway + owner /admin."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.providers.reelshort import get_series, list_series, scan_reelshort
from app.store import ensure_bootstrap_admin, run_migrations

logger = logging.getLogger("api-drama")

# Also scrubs JWT-shaped strings (Turso auth tokens are JWTs).
_SECRET_RE = re.compile(r"(tn_live_[A-Za-z0-9_\-]+|[A-Za-z0-9]{20,}msh[A-Za-z0-9]+|"
                        r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,})")


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _SECRET_RE.sub("***", str(record.getMessage()))
            record.args = ()
        except (TypeError, ValueError):
            pass
        return True


def _configure_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        handler.addFilter(_RedactingFilter())
    try:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(settings.log_file), maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        file_handler.addFilter(_RedactingFilter())
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(file_handler)
    except (OSError, PermissionError) as exc:
        logger.warning("file logging disabled: %s", type(exc).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    try:
        applied = run_migrations()
        if applied:
            logger.info("migrations applied: %s", ",".join(applied))
    except Exception as exc:
        logger.error("migration failed: %s", type(exc).__name__)
    try:
        ensure_bootstrap_admin()
    except Exception as exc:  # noqa: BLE001 — bootstrap must never block startup
        logger.error("admin bootstrap failed: %s", type(exc).__name__)
    # Episode cache (Turso): startup must never block or fail the gateway.
    cache_boot: asyncio.Task | None = None
    try:
        from app import episode_cache as _ec
        cache_boot = asyncio.create_task(_ec.get_service().start())
    except Exception as exc:  # noqa: BLE001
        logger.error("episode cache init failed: %s", type(exc).__name__)
    yield
    try:
        from app import episode_cache as _ec
        if cache_boot is not None and not cache_boot.done():
            cache_boot.cancel()
        await _ec.get_service().stop()
    except Exception as exc:  # noqa: BLE001
        logger.error("episode cache shutdown failed: %s", type(exc).__name__)


app = FastAPI(title="ToolNet Drama API", version="2.0.0", lifespan=lifespan)

# Shared admin design system (CSS/JS). Public by design: no secrets, only assets.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.middleware("http")
async def _request_id_middleware(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex[:12]
    start = time.monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Response-Time-Ms"] = str(int((time.monotonic() - start) * 1000))
    return response


def _code_and_message(detail: str) -> tuple[str, str]:
    if isinstance(detail, str) and ": " in detail:
        code, _, msg = detail.partition(": ")
        if code and code == code.upper().replace(" ", "_"):
            return code, msg
    return "ERROR", str(detail)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    # Standard envelope for public gateway errors; legacy /api/* keeps old shape.
    if request.url.path.startswith("/v1/"):
        code, message = _code_and_message(exc.detail)
        rid = getattr(request.state, "request_id", uuid.uuid4().hex[:12])
        return JSONResponse(status_code=exc.status_code,
                            content={"success": False, "data": None,
                                     "error": {"code": code, "message": message,
                                               "status": exc.status_code},
                                     "request_id": rid})
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# -- legacy endpoints (unchanged behaviour) ---------------------------------

@app.get("/health")
def health():
    return {"ok": True, "service": "api-drama", "version": "2.0.0"}


@app.get("/v1/health")
def v1_health():
    """Public gateway health: DB + pool summary. No secrets."""
    info: dict = {"ok": True, "service": "api-drama-gateway"}
    try:
        from app.providers import pool as _pool
        from app.store import connect as _connect
        with _connect() as db:
            cfg = _pool.get_settings(db, "reelshort")
            rows = db.execute(
                "SELECT status, enabled, cooldown_until, quota_limit, requests_used "
                "FROM provider_connections WHERE provider='reelshort'").fetchall()
        healthy = 0
        for r in rows:
            d = dict(r)
            if d.get("enabled") and (d.get("status") or "active") != "auth_error":
                lim = d.get("quota_limit")
                if lim is None or (d.get("requests_used") or 0) < lim:
                    healthy += 1
        info["upstream"] = {"provider": "reelshort",
                            "strategy": cfg.get("strategy"),
                            "connections": len(rows), "healthy": healthy}
    except Exception as e:
        info["upstream"] = {"provider": "reelshort", "connections": "unknown", "error": str(e)}
    return info


@app.get("/v1/debug/db")
def v1_debug_db():
    """Debug database connection."""
    from app.store import connect as _connect
    from app.core.config import settings
    try:
        db = _connect()
        info = {
            "use_postgres": settings.use_postgres,
            "use_libsql": settings.use_libsql,
            "database_url_set": bool(settings.database_url),
            "database_url_prefix": settings.database_url[:50] if settings.database_url else None,
        }
        # Test query
        rows = db.execute("SELECT 1 as test").fetchall()
        info["query_test"] = "OK" if rows else "FAIL"
        # Test provider_connections
        rows = db.execute("SELECT COUNT(*) as c FROM provider_connections WHERE provider='reelshort'").fetchall()
        info["provider_connections_count"] = rows[0]["c"] if rows else 0
        return {"ok": True, "info": info}
    except Exception as e:
        import traceback
        return {"ok": False, "error": str(e), "traceback": traceback.format_exc()}
    # Status word only (no host/secrets). Turso trouble never flips the overall `ok`.
    try:
        from app import episode_cache as _ec
        svc = _ec.get_service()
        status = "disabled" if not svc.cfg.enabled else (
            "unconfigured" if not svc.active else
            ("degraded" if svc.client and svc.client.breaker_open else "enabled"))
        info["episode_cache"] = {"status": status}
    except Exception:  # noqa: BLE001
        info["episode_cache"] = {"status": "unknown"}
    return info


@app.post("/api/reelshort/scan")
async def scan():
    try:
        return await scan_reelshort()
    except (ValueError, Exception) as exc:
        logger.exception("ReelShort scan failed")
        raise HTTPException(502, "ReelShort scan failed; check server logs") from exc


@app.get("/api/reelshort/series")
def series(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return list_series(limit, offset)


@app.get("/api/reelshort/series/{book_id}")
def detail(book_id: str):
    movie = get_series(book_id)
    if movie is None:
        raise HTTPException(404, "Series not found")
    return movie


# -- gateway + owner admin (admin routers hide themselves from /docs) --------

from app.routers import admin_auth as _admin_auth  # noqa: E402
from app.routers import admin_cache as _admin_cache  # noqa: E402
from app.routers import admin_keys as _admin_keys  # noqa: E402
from app.routers import admin_pages as _admin_pages  # noqa: E402
from app.routers import admin_providers as _admin_providers  # noqa: E402
from app.routers import admin_stats as _admin_stats  # noqa: E402
from app.routers import v1_reelshort as _v1  # noqa: E402

app.include_router(_v1.router)
app.include_router(_admin_auth.router)
app.include_router(_admin_keys.router)
app.include_router(_admin_providers.router)
app.include_router(_admin_stats.router)
app.include_router(_admin_cache.router)
app.include_router(_admin_pages.router)

Path(__file__).resolve().parents[1].joinpath("logs").mkdir(parents=True, exist_ok=True)
