"""Owner gateway overview stats (hidden from public docs)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.deps import require_admin_session
from app.providers import pool
from app.store import connect as store_connect
from app.core.config import settings

router = APIRouter(prefix="/admin/api/stats", tags=["admin-stats"], include_in_schema=False)


@router.get("/overview")
async def overview(_a: dict = Depends(require_admin_session)):
    with store_connect() as db:
        try:
            # PostgreSQL compatible: use CASE WHEN instead of boolean SUM
            if settings.use_postgres:
                bool_ok = "SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END)"
                bool_err = "SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END)"
                date_func = "substring(created_at FROM 1 FOR 10)"
            else:
                bool_ok = "COALESCE(SUM(status < 400), 0)"
                bool_err = "COALESCE(SUM(status >= 400), 0)"
                date_func = "substr(created_at, 1, 10)"

            totals = db.execute(
                f"""SELECT COUNT(*) AS total,
                          {bool_ok} AS ok,
                          {bool_err} AS errors
                     FROM request_logs""").fetchone()
            cache = db.execute(
                """SELECT COUNT(*) AS n FROM request_logs
                   WHERE provider IS NOT NULL AND connection_id IS NULL AND status = 200"""
            ).fetchone()[0]
            upstream = db.execute(
                "SELECT COUNT(*) FROM request_logs WHERE connection_id IS NOT NULL"
            ).fetchone()[0]
            days = db.execute(
                f"""SELECT {date_func} AS day, COUNT(*) AS total,
                          {bool_ok} AS ok,
                          {bool_err} AS errors
                     FROM request_logs GROUP BY day ORDER BY day DESC LIMIT 14""").fetchall()
            top_keys = db.execute(
                """SELECT k.name, k.key_prefix, COUNT(l.id) AS requests
                   FROM api_keys k LEFT JOIN request_logs l ON l.key_id = k.id
                   GROUP BY k.id ORDER BY requests DESC LIMIT 10""").fetchall()
            conns = db.execute("SELECT * FROM provider_connections ORDER BY provider, priority, id"
                               ).fetchall()
        except Exception:
            return {"ok": True, "totals": {"total": 0, "ok": 0, "errors": 0},
                    "cache_served": 0, "upstream_calls": 0, "days": [],
                    "top_keys": [], "connections": []}
    return {
        "ok": True,
        "totals": {"total": totals["total"], "ok": totals["ok"], "errors": totals["errors"]},
        "cache_served": cache,
        "upstream_calls": upstream,
        "days": [dict(d) for d in days],
        "top_keys": [dict(k) for k in top_keys],
        "connections": [
            {"id": c["id"], "provider": c["provider"], "name": c["name"],
             "state": pool.public_view(c)["state"],
             "requests_used": c["requests_used"],
             "quota_text": pool.public_view(c)["quota_text"]} for c in conns
        ],
    }