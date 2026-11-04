"""PostgreSQL/libSQL/SQLite store + file-based migration runner.

Supports PostgreSQL (Layerbase), libSQL (Turso/Layerbase), and local SQLite.
Keeps the legacy `series` table intact; adds gateway tables idempotently.
Migration 002 seeds provider_connections from legacy env keys (KEY_1/KEY_2)
so existing deployments keep working without re-entering secrets.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.core.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Thread-local storage for connections (not thread-safe)
_libsql_local = threading.local()
_postgres_local = threading.local()


class _DBWrapper:
    """Wrapper to provide consistent interface across SQLite, libSQL, and PostgreSQL."""
    
    def __init__(self, conn: Any):
        self._conn = conn
        self._is_pg = settings.use_postgres
        self._is_libsql = settings.use_libsql
    
    def execute(self, query: str, params: tuple = ()) -> Any:
        """Execute query and return cursor."""
        if self._is_pg:
            # Convert ? placeholders to %s for PostgreSQL
            query = query.replace("?", "%s")
            import psycopg2.extras
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(query, params)
            return cur
        else:
            # SQLite and libSQL have execute method on connection
            return self._conn.execute(query, params)
    
    def commit(self) -> None:
        self._conn.commit()
    
    def close(self) -> None:
        if not self._is_pg and not self._is_libsql:
            self._conn.close()
    
    @property
    def closed(self) -> bool:
        if self._is_pg:
            return self._conn.closed
        return False
    
    # Context manager support for tests
    def __enter__(self) -> '_DBWrapper':
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            # Commit on successful exit
            self.commit()
        if not self._is_pg and not self._is_libsql:
            self.close()


def _get_postgres_connection() -> _DBWrapper:
    """Get or create a PostgreSQL connection for the current thread."""
    import psycopg2
    import psycopg2.extras
    from urllib.parse import urlparse, parse_qs

    if not hasattr(_postgres_local, "conn") or _postgres_local.conn is None or _postgres_local.conn.closed:
        # Parse and clean DSN (remove unsupported params like autocommit)
        parsed = urlparse(settings.database_url)
        query = parse_qs(parsed.query)
        for k in list(query.keys()):
            if k not in ("sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout", "application_name", "ApplicationName"):
                del query[k]
        clean_query = "&".join(f"{k}={v[0]}" for k, v in query.items())
        clean_dsn = f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
        if clean_query:
            clean_dsn += f"?{clean_query}"
        
        _postgres_local.conn = psycopg2.connect(clean_dsn)
        _postgres_local.conn.autocommit = True
        _postgres_local.conn.cursor_factory = psycopg2.extras.RealDictCursor
    return _DBWrapper(_postgres_local.conn)


def _get_libsql_connection() -> _DBWrapper:
    """Get or create a libSQL connection for the current thread."""
    import libsql

    if not hasattr(_libsql_local, "conn") or _libsql_local.conn is None:
        _libsql_local.conn = libsql.connect(
            settings.database_url,
            auth_token=settings.auth_token or None,
            timeout=15,
            autocommit=True,
            _check_same_thread=False,
        )
    return _DBWrapper(_libsql_local.conn)


def _get_sqlite_connection() -> _DBWrapper:
    """Get a local SQLite connection."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(settings.db_path), timeout=15)
    db.row_factory = sqlite3.Row
    return _DBWrapper(db)


def _connect() -> _DBWrapper:
    """Return a database connection wrapper (PostgreSQL, libSQL, or SQLite)."""
    if settings.use_postgres:
        return _get_postgres_connection()
    if settings.use_libsql:
        return _get_libsql_connection()
    return _get_sqlite_connection()


def connect() -> _DBWrapper:
    """Public connection getter."""
    return _connect()


def _execute_migration_sql(db: _DBWrapper, sql: str) -> None:
    """Execute migration SQL, handling all backends."""
    for stmt in sql.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                db.execute(stmt)
            except Exception as e:
                # Ignore "already exists" errors for idempotent migrations
                err_str = str(e).lower()
                if "already exists" in err_str or "duplicate" in err_str or "wrong number of parameters" in err_str:
                    continue
                raise


def _fetchall(db: _DBWrapper, query: str, params: tuple = ()) -> list:
    """Fetch all rows, returning list of dicts for all backends."""
    cursor = db.execute(query, params)
    if settings.use_postgres:
        return [dict(row) for row in cursor.fetchall()]
    elif settings.use_libsql:
        rows = cursor.fetchall()
        if rows and hasattr(cursor, "description") and cursor.description:
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]
        return rows
    else:
        return [dict(row) for row in cursor.fetchall()]


def _fetchone(db: _DBWrapper, query: str, params: tuple = ()) -> dict | None:
    """Fetch one row as dict."""
    cursor = db.execute(query, params)
    if settings.use_postgres:
        row = cursor.fetchone()
        return dict(row) if row else None
    elif settings.use_libsql:
        row = cursor.fetchone()
        if row and hasattr(cursor, "description") and cursor.description:
            cols = [d[0] for d in cursor.description]
            return dict(zip(cols, row))
        return row
    else:
        row = cursor.fetchone()
        return dict(row) if row else None


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
    year, month = now.year, now.month
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return nxt.isoformat()


def ensure_migrations_table(db: _DBWrapper) -> None:
    if settings.use_postgres:
        exists = _fetchone(db, "SELECT 1 FROM information_schema.tables WHERE table_name='schema_migrations'")
    else:
        exists = _fetchone(db, "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
    if not exists:
        db.execute(
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )


def applied_versions(db: _DBWrapper) -> set[str]:
    ensure_migrations_table(db)
    rows = _fetchall(db, "SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


def _ensure_api_keys_columns(db: _DBWrapper) -> None:
    if settings.use_postgres:
        cols = {r["name"] for r in _fetchall(db, "SELECT * FROM information_schema.columns WHERE table_name='api_keys'")}
    else:
        cols = {r["name"] for r in _fetchall(db, "PRAGMA table_info(api_keys)")}
    if "expires_at" not in cols:
        db.execute("ALTER TABLE api_keys ADD COLUMN expires_at TEXT")
    if "allowed_providers" not in cols:
        db.execute("ALTER TABLE api_keys ADD COLUMN allowed_providers TEXT")


def _ensure_request_logs_columns(db: _DBWrapper) -> None:
    if settings.use_postgres:
        cols = {r["name"] for r in _fetchall(db, "SELECT * FROM information_schema.columns WHERE table_name='request_logs'")}
    else:
        cols = {r["name"] for r in _fetchall(db, "PRAGMA table_info(request_logs)")}
    if "connection_id" not in cols:
        db.execute("ALTER TABLE request_logs ADD COLUMN connection_id INTEGER")
    if "provider" not in cols:
        db.execute("ALTER TABLE request_logs ADD COLUMN provider TEXT")


def _ensure_connection_health_columns(db: _DBWrapper) -> None:
    if settings.use_postgres:
        cols = {r["name"] for r in _fetchall(db, "SELECT * FROM information_schema.columns WHERE table_name='provider_connections'")}
    else:
        cols = {r["name"] for r in _fetchall(db, "PRAGMA table_info(provider_connections)")}
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


def _seed_connections_from_env(db: _DBWrapper) -> None:
    """One-time seed: legacy RAPIDAPI_REELSHORT_KEY_1/_KEY_2 -> connections table."""
    if settings.use_postgres:
        has_table = _fetchone(db, "SELECT 1 FROM information_schema.tables WHERE table_name='provider_connections'")
    else:
        has_table = _fetchone(db, "SELECT name FROM sqlite_master WHERE type='table' AND name='provider_connections'")
    if not has_table:
        return
    count_row = _fetchone(db, "SELECT COUNT(*) as cnt FROM provider_connections WHERE provider='reelshort'")
    if count_row and count_row.get("cnt", 0) > 0:
        return
    from app.core.crypto import encrypt_key, mask_key  # local import: avoids cycle

    master = os.getenv("TOOLNET_MASTER_KEY", "").strip()
    if not master:
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
            """INSERT INTO provider_connections
               (provider, name, key_encrypted, key_hint, enabled, status, priority,
                quota_limit, quota_window, requests_used, quota_reset_at,
                consecutive_failures, created_at)
               VALUES ('reelshort', ?, ?, ?, 1, 'active', ?, NULL, 'monthly', 0, ?, 0, ?)
               ON CONFLICT DO NOTHING""",
            (name, enc, mask_key(raw), 10 * (idx + 1), next_quota_reset("monthly"), now),
        )
    db.execute(
        """INSERT INTO provider_settings
           (provider, strategy, rotation_enabled, cooldown_seconds, updated_at)
           VALUES ('reelshort', ?, ?, ?, ?)
           ON CONFLICT DO NOTHING""",
        (settings.pool_strategy, 1 if settings.pool_rotation_enabled else 0,
         settings.pool_cooldown_s, now),
    )


def run_migrations() -> list[str]:
    """Apply pending *.sql migrations in lexical order. Returns applied versions."""
    applied: list[str] = []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    db = _connect()
    try:
        done = applied_versions(db)
        for path in files:
            version = path.stem
            if version in done:
                continue
            sql = path.read_text(encoding="utf-8")
            _execute_migration_sql(db, sql)
            placeholder = "%s" if settings.use_postgres else "?"
            db.execute(
                f"INSERT INTO schema_migrations(version, applied_at) VALUES ({placeholder}, {placeholder}) ON CONFLICT DO NOTHING",
                (version, utcnow()),
            )
            applied.append(version)
        # Ensure legacy series table exists even on fresh DBs.
        _execute_migration_sql(db, """CREATE TABLE IF NOT EXISTS series (
                book_id TEXT PRIMARY KEY, t_book_id TEXT, title TEXT NOT NULL,
                poster TEXT, description TEXT, chapter_count INTEGER,
                paid_start INTEGER, tags TEXT NOT NULL, shelves TEXT NOT NULL,
                first_chapter_id TEXT, read_count INTEGER, updated_at TEXT NOT NULL
            )""")
        for helper in (_ensure_request_logs_columns, _ensure_api_keys_columns,
                       _ensure_connection_health_columns):
            try:
                helper(db)
            except Exception:
                pass
        _seed_connections_from_env(db)
        db.commit()
    finally:
        if not settings.use_postgres and not settings.use_libsql:
            db.close()
    return applied


def ensure_bootstrap_admin() -> str | None:
    """Create the first owner account from env (ADMIN_USERNAME/ADMIN_PASSWORD)."""
    import logging

    from app.core.admin_auth import hash_password

    logger = logging.getLogger("api-drama")
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    db = _connect()
    try:
        try:
            count_row = _fetchone(db, "SELECT COUNT(*) as cnt FROM admin_users")
        except Exception:
            return None
        if count_row and count_row.get("cnt", 0) > 0:
            return None
        if not username or not password:
            logger.warning("no admin users exist; set ADMIN_USERNAME/ADMIN_PASSWORD or use tools/create_admin.py")
            return None
        try:
            db.execute(
                "INSERT INTO admin_users (username, pw_hash, created_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
                (username, hash_password(password), utcnow()),
            )
            db.commit()
        except ValueError as exc:
            logger.error("bootstrap admin failed: %s", exc)
            return None
        logger.warning("bootstrap admin '%s' created; remove ADMIN_PASSWORD from .env now", username)
        return username
    finally:
        if not settings.use_postgres and not settings.use_libsql:
            db.close()