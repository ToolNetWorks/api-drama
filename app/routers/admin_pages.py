"""Owner dashboard pages (HTML shell only; all data via session API)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.deps import get_admin_session

router = APIRouter(tags=["admin-pages"], include_in_schema=False)
PAGES = Path(__file__).resolve().parent.parent / "admin_pages"


def _page(name: str) -> str:
    return (PAGES / name).read_text(encoding="utf-8")


@router.get("/admin", response_class=HTMLResponse)
async def admin_root(request: Request):
    if get_admin_session(request) is not None:
        return RedirectResponse(url="/admin/overview", status_code=302)
    return HTMLResponse(_page("login.html"))


@router.get("/admin/overview", response_class=HTMLResponse)
async def admin_overview(request: Request):
    if get_admin_session(request) is None:
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(_page("overview.html"))


@router.get("/admin/providers", response_class=HTMLResponse)
async def admin_providers(request: Request):
    if get_admin_session(request) is None:
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(_page("providers.html"))


@router.get("/admin/keys", response_class=HTMLResponse)
async def admin_keys(request: Request):
    if get_admin_session(request) is None:
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(_page("keys.html"))


@router.get("/admin/cache", response_class=HTMLResponse)
async def admin_cache(request: Request):
    # Static shell only: a Turso outage can never make the dashboard unreachable.
    if get_admin_session(request) is None:
        return RedirectResponse(url="/admin", status_code=302)
    return HTMLResponse(_page("cache.html"))


@router.get("/developers", response_class=HTMLResponse)
async def developers_docs():
    # Public customer documentation. Contains no secrets or key material.
    return HTMLResponse(_page("developers.html"))
