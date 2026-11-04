"""Multi-connection pool: Round Robin / Fill First over independent quotas.

Rules (load-bearing):
- Only connections with independent quota and operator-allowed rotation take
  part in selection. Unlimited connections are supported (no code cap).
- A connection in cooldown (transient 429/5xx/timeout) is excluded until
  cooldown_until passes.
- A connection with status='auth_error' (upstream 401/403) is excluded until
  an admin re-enables it. The caller MUST NOT automatically try another
  account on 401/403 — it returns the error immediately (fail-fast).
- Quota exhaustion skips the connection until quota_reset_at; reset rolls
  automatically (daily/monthly).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from app.store import next_quota_reset, utcnow


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def get_settings(db: sqlite3.Connection, provider: str) -> dict[str, Any]:
    row = db.execute(
        "SELECT * FROM provider_settings WHERE provider=?", (provider,)
    ).fetchone()
    if row is None:
        return {"provider": provider, "strategy": "round_robin",
                "rotation_enabled": 1, "cooldown_seconds": 300}
    return dict(row)


def upsert_settings(db: sqlite3.Connection, provider: str, strategy: str,
                    rotation_enabled: bool, cooldown_seconds: int) -> dict[str, Any]:
    if strategy not in ("round_robin", "fill_first"):
        raise ValueError("strategy must be round_robin or fill_first")
    cooldown_seconds = max(10, min(int(cooldown_seconds), 3600))
    now = utcnow()
    db.execute(
        """INSERT INTO provider_settings (provider, strategy, rotation_enabled, cooldown_seconds, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(provider) DO UPDATE SET strategy=excluded.strategy,
             rotation_enabled=excluded.rotation_enabled,
             cooldown_seconds=excluded.cooldown_seconds, updated_at=excluded.updated_at""",
        (provider, strategy, 1 if rotation_enabled else 0, cooldown_seconds, now),
    )
    return get_settings(db, provider)


def _maybe_reset_quota(db: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """Reset requests_used if quota window elapsed. Returns refreshed row."""
    reset_at = _parse_dt(row["quota_reset_at"])
    if reset_at is not None and _now() >= reset_at:
        window = row["quota_window"] or "monthly"
        db.execute(
            "UPDATE provider_connections SET requests_used=0, quota_reset_at=? WHERE id=?",
            (next_quota_reset(window), row["id"]),
        )
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (row["id"],)).fetchone()
    elif reset_at is None:
        window = row["quota_window"] or "monthly"
        db.execute(
            "UPDATE provider_connections SET quota_reset_at=? WHERE id=?",
            (next_quota_reset(window), row["id"]),
        )
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (row["id"],)).fetchone()
    return row


def _col(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    try:
        return row[name] if name in row.keys() else default
    except (IndexError, KeyError):
        return default


def sync_quota_from_headers(db: sqlite3.Connection, conn_id: int, headers: Any) -> None:
    """Adopt real upstream quota signals (RapidAPI x-ratelimit-* headers).

    - quota_remaining / reset / synced_at always refresh when headers present.
    - quota_limit is adopted ONLY when the owner left it NULL (manual cap wins).
    - Never raises: quota sync must not break the request.
    """
    try:
        h = {str(k).lower(): v for k, v in dict(headers).items()}
        remaining = _parse_int(h.get("x-ratelimit-requests-remaining"), 0, 1_000_000_000)
        limit = _parse_int(h.get("x-ratelimit-requests-limit"), 1, 10_000_000)
        reset_s = _parse_int(h.get("x-ratelimit-requests-reset"), 60, 366 * 86400)
        if remaining is None or limit is None or reset_s is None:
            return
        from datetime import timedelta
        reset_at = (_now() + timedelta(seconds=reset_s)).isoformat()
        now = utcnow()
        row = db.execute("SELECT quota_limit FROM provider_connections WHERE id=?",
                         (conn_id,)).fetchone()
        if row is None:
            return
        if row["quota_limit"] is None:
            db.execute(
                """UPDATE provider_connections SET quota_limit=?, quota_remaining=?,
                   quota_reset_at=?, quota_synced_at=? WHERE id=?""",
                (limit, remaining, reset_at, now, conn_id),
            )
        else:
            db.execute(
                """UPDATE provider_connections SET quota_remaining=?,
                   quota_reset_at=?, quota_synced_at=? WHERE id=?""",
                (remaining, reset_at, now, conn_id),
            )
    except (sqlite3.OperationalError, ValueError, TypeError, AttributeError):
        pass


def _parse_int(raw: Any, lo: int, hi: int) -> int | None:
    try:
        value = int(str(raw).strip())
    except (ValueError, TypeError, AttributeError):
        return None
    if lo <= value <= hi:
        return value
    return None


def _is_usable(row: sqlite3.Row) -> bool:
    if not row["enabled"]:
        return False
    if (row["status"] or "active") == "auth_error":
        return False
    cd = _parse_dt(row["cooldown_until"])
    if cd is not None and _now() < cd:
        return False
    limit = row["quota_limit"]
    if limit is not None and (row["requests_used"] or 0) >= limit:
        return False
    remaining = _col(row, "quota_remaining")
    if remaining is not None and remaining <= 0:
        return False
    return True


def pick_connection(db: sqlite3.Connection, provider: str) -> Optional[sqlite3.Row]:
    """Select next eligible connection per provider strategy. Returns None if empty."""
    cfg = get_settings(db, provider)
    strategy = cfg.get("strategy") or "round_robin"
    rotation = bool(cfg.get("rotation_enabled", 1))
    rows = db.execute(
        "SELECT * FROM provider_connections WHERE provider=? AND enabled=1 ORDER BY id",
        (provider,),
    ).fetchall()
    usable: list[sqlite3.Row] = []
    for row in rows:
        row = _maybe_reset_quota(db, row)
        if _is_usable(row):
            usable.append(row)
    if not usable:
        return None
    if not rotation or strategy == "fill_first":
        usable.sort(key=lambda r: (r["priority"] or 100, r["id"]))
        return usable[0]
    # round_robin: least-recently-used first (NULLS first), tie-break by priority/id.
    def _sort_key(r: sqlite3.Row) -> tuple:
        lu = _parse_dt(r["last_used_at"])
        return (lu is not None, lu or _now(), r["priority"] or 100, r["id"])
    usable.sort(key=_sort_key)
    return usable[0]


def public_view(row: sqlite3.Row) -> dict[str, Any]:
    """Non-secret dashboard/API view. NEVER includes key material."""
    cd = _parse_dt(row["cooldown_until"])
    cooling = bool(cd is not None and _now() < cd)
    limit = row["quota_limit"]
    used = row["requests_used"] or 0
    remaining = _col(row, "quota_remaining")
    synced_at = _col(row, "quota_synced_at")
    if limit is None:
        quota_text = f"{used} / unlimited" if remaining is None else f"{used} used · {remaining} left"
        pct = 0
    else:
        quota_text = f"{used} / {limit}" if remaining is None else f"{used} / {limit} · {remaining} left"
        pct = min(100, int(used * 100 / limit) if limit else 100)
    if (row["status"] or "active") == "auth_error":
        state = "auth_error"
    elif not row["enabled"]:
        state = "disabled"
    elif cooling:
        state = "cooldown"
    elif (limit is not None and used >= limit) or (remaining is not None and remaining <= 0):
        state = "exhausted"
    else:
        state = "active"
    return {
        "id": row["id"],
        "provider": row["provider"],
        "name": row["name"],
        "key_hint": row["key_hint"],
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "state": state,
        "priority": row["priority"],
        "requests_used": used,
        "quota_limit": limit,
        "quota_remaining": remaining,
        "quota_synced_at": synced_at,
        "quota_window": row["quota_window"],
        "quota_text": quota_text,
        "quota_pct": pct,
        "quota_reset_at": row["quota_reset_at"],
        "cooldown_until": row["cooldown_until"],
        "cooling": cooling,
        "consecutive_failures": row["consecutive_failures"] or 0,
        "last_error": row["last_error"],
        "last_used_at": row["last_used_at"],
        "last_check_at": row["last_check_at"] if "last_check_at" in row.keys() else None,
        "last_check_ok": row["last_check_ok"] if "last_check_ok" in row.keys() else None,
        # Four-state verification (req: stored vs key-valid vs endpoint-valid vs working).
        # key_valid: None=unchecked, 1=gateway accepted key, 0=rejected (401/403).
        # endpoint_ok: None=unchecked, 1=configured path returned 2xx, 0=failed.
        "key_valid": row["key_valid"] if "key_valid" in row.keys() else None,
        "endpoint_ok": row["endpoint_ok"] if "endpoint_ok" in row.keys() else None,
        "created_at": row["created_at"],
    }


def record_success(db: sqlite3.Connection, conn_id: int) -> None:
    now = utcnow()
    db.execute(
        """UPDATE provider_connections SET requests_used=requests_used+1,
           consecutive_failures=0, last_used_at=?, cooldown_until=NULL WHERE id=?""",
        (now, conn_id),
    )


def record_transient_failure(db: sqlite3.Connection, conn_id: int, error: str, cooldown_s: int) -> None:
    """429/5xx/timeout: exclude connection for cooldown. Caller may try next."""
    from datetime import timedelta
    until = (_now() + timedelta(seconds=max(10, min(cooldown_s, 3600)))).isoformat()
    safe = (error or "upstream error")[:300]
    db.execute(
        """UPDATE provider_connections SET consecutive_failures=consecutive_failures+1,
           cooldown_until=?, last_error=?, last_used_at=? WHERE id=?""",
        (until, safe, utcnow(), conn_id),
    )


def record_auth_failure(db: sqlite3.Connection, conn_id: int, error: str) -> None:
    """401/403: mark auth_error. Caller MUST NOT try another connection."""
    safe = (error or "upstream access denied")[:300]
    db.execute(
        """UPDATE provider_connections SET status='auth_error', last_error=?,
           consecutive_failures=consecutive_failures+1, last_used_at=? WHERE id=?""",
        (safe, utcnow(), conn_id),
    )


def record_key_probe(db: sqlite3.Connection, conn_id: int, key_valid: bool) -> None:
    """Record a key-validity probe outcome WITHOUT health side effects.

    The probe consumes upstream quota (counted in requests_used) but never
    marks the connection healthy, verified, or failed: a 404 on the unknown
    probe path proves nothing about endpoints, and only 401/403 (handled by
    record_auth_failure) proves anything about the key beyond acceptance.
    """
    db.execute(
        """UPDATE provider_connections SET requests_used=requests_used+1,
           key_valid=?, last_check_at=?, last_used_at=? WHERE id=?""",
        (1 if key_valid else 0, utcnow(), utcnow(), conn_id),
    )


def mark_endpoint(db: sqlite3.Connection, conn_id: int, ok: bool) -> None:
    """Record a *verified-path* endpoint outcome (2xx only counts as ok)."""
    db.execute(
        """UPDATE provider_connections SET endpoint_ok=?, last_check_at=?,
           last_check_ok=? WHERE id=?""",
        (1 if ok else 0, utcnow(), 1 if ok else 0, conn_id),
    )


def clear_error(db: sqlite3.Connection, conn_id: int) -> None:
    db.execute(
        """UPDATE provider_connections SET status='active', cooldown_until=NULL,
           consecutive_failures=0, last_error=NULL WHERE id=?""",
        (conn_id,),
    )
