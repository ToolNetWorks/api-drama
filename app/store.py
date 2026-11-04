"""SQLite store + file-based migration runner (Alembic-equivalent for SQLite).

Keeps the legacy `series` table intact; adds gateway tables idempotently.
Migration 002 seeds provider_connections from legacy env keys (KEY_1/KEY_2)
so existing deployments keep working without re-entering secrets.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def _connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(settings.db_path), timeout=15)
    db.row_factory = sqlite3.Row
    return db


def connect() -> sqlite3.Connection:
    return _connect()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def next_quota_reset(window: str, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    window = (window or "monthly").lower()
    if window == "daily":
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return nxt.isoformat()
    # monthly: first day of next month 00:00 UTC
    year, month = now.year, now.month
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return nxt.isoformat()


def ensure_migrations_table(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )


def applied_versions(db: sqlite3.Connection) -> set[str]:
    ensure_migrations_table(db)
    return {r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()}


def _ensure_api_keys_columns(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "expires_at" not in cols:
        db.execute("ALTER TABLE api_keys ADD COLUMN expires_at TEXT")
    if "allowed_providers" not in cols:
        db.execute("ALTER TABLE api_keys ADD COLUMN allowed_providers TEXT")


def _ensure_request_logs_columns(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(request_logs)").fetchall()}
    if "connection_id" not in cols:
        db.execute("ALTER TABLE request_logs ADD COLUMN connection_id INTEGER")
    if "provider" not in cols:
        db.execute("ALTER TABLE request_logs ADD COLUMN provider TEXT")


def _ensure_connection_health_columns(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(provider_connections)").fetchall()}
    if "last_check_at" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN last_check_at TEXT")
    if "last_check_ok" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN last_check_ok INTEGER")
    if "key_valid" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN key_valid INTEGER")
    if "endpoint_ok" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN endpoint_ok INTEGER")
    if "quota_remaining" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN quota_remaining INTEGER")
    if "quota_synced_at" not in cols:
        db.execute("ALTER TABLE provider_connections ADD COLUMN quota_synced_at TEXT")


def _seed_connections_from_env(db: sqlite3.Connection) -> None:
    """One-time seed: legacy RAPIDAPI_REELSHORT_KEY_1/_KEY_2 -> connections table."""
    has_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_connections'"
    ).fetchone()
    if not has_table:
        return
    count = db.execute(
        "SELECT COUNT(*) FROM provider_connections WHERE provider='reelshort'"
    ).fetchone()[0]
    if count > 0:
        return
    from app.core.crypto import encrypt_key, mask_key  # local import: avoids cycle

    import os

    master = os.getenv("TOOLNET_MASTER_KEY", "").strip()
    if not master:
        # Cannot encrypt without master key: leave seeding to operator via dashboard.
        return
    seeds: list[tuple[str, str]] = []
    if settings.rapid_key_1:
        seeds.append(("rapidapi-1", settings.rapid_key_1))
    if settings.rapid_key_2 and settings.rapid_key_2 != settings.rapid_key_1:
        seeds.append(("rapidapi-2", settings.rapid_key_2))
    now = utcnow()
    for idx, (name, raw) in enumerate(seeds):
        try:
            enc = encrypt_key(raw, master)
        except ValueError:
            continue
        db.execute(
            """INSERT OR IGNORE INTO provider_connections
               (provider, name, key_encrypted, key_hint, enabled, status, priority,
                quota_limit, quota_window, requests_used, quota_reset_at,
                consecutive_failures, created_at)
               VALUES ('reelshort', ?, ?, ?, 1, 'active', ?, NULL, 'monthly', 0, ?, 0, ?)""",
            (name, enc, mask_key(raw), 10 * (idx + 1), next_quota_reset("monthly"), now),
        )
    # Default provider settings row.
    db.execute(
        """INSERT OR IGNORE INTO provider_settings
           (provider, strategy, rotation_enabled, cooldown_seconds, updated_at)
           VALUES ('reelshort', ?, ?, ?, ?)""",
        (settings.pool_strategy, 1 if settings.pool_rotation_enabled else 0,
         settings.pool_cooldown_s, now),
    )


def run_migrations() -> list[str]:
    """Apply pending *.sql migrations in lexical order. Returns applied versions."""
    applied: list[str] = []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with _connect() as db:
        done = applied_versions(db)
        for path in files:
            version = path.stem  # e.g. 001_gateway
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            db.executescript(sql)
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utcnow()),
            )
            applied.append(version)
        # Ensure legacy series table exists even on fresh DBs (same DDL as provider).
        db.execute(
            """CREATE TABLE IF NOT EXISTS series (
                book_id TEXT PRIMARY KEY, t_book_id TEXT, title TEXT NOT NULL,
                poster TEXT, description TEXT, chapter_count INTEGER,
                paid_start INTEGER, tags TEXT NOT NULL, shelves TEXT NOT NULL,
                first_chapter_id TEXT, read_count INTEGER, updated_at TEXT NOT NULL
            )"""
        )
        # Guarded columns for tables created by older migrations.
        for helper in (_ensure_request_logs_columns, _ensure_api_keys_columns,
                       _ensure_connection_health_columns):
            try:
                helper(db)
            except sqlite3.OperationalError:
                pass
        _seed_connections_from_env(db)
    return applied


def ensure_bootstrap_admin() -> str | None:
    """Create the first owner account from env (ADMIN_USERNAME/ADMIN_PASSWORD).

    Returns the username created, or None if nothing to do. Only runs when
    admin_users is empty. Operators should remove ADMIN_PASSWORD from .env
    after the first login.
    """
    import logging
    import os

    from app.core.admin_auth import hash_password

    logger = logging.getLogger("api-drama")
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    with _connect() as db:
        try:
            count = db.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0]
        except sqlite3.OperationalError:
            return None
        if count > 0:
            return None
        if not username or not password:
            logger.warning("no admin users exist; set ADMIN_USERNAME/ADMIN_PASSWORD or use tools/create_admin.py")
            return None
        try:
            db.execute(
                "INSERT INTO admin_users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                (username, hash_password(password), utcnow()),
            )
        except ValueError as exc:
            logger.error("bootstrap admin failed: %s", exc)
            return None
        logger.warning("bootstrap admin '%s' created; remove ADMIN_PASSWORD from .env now", username)
        return username
