"""Turso schema + idempotent, versioned migrations for the episode cache.

Only touches Turso. The production SQLite (keys/admin/connections/quota) is
never read or written here. Timestamps are INTEGER unix epoch seconds (UTC).
Every statement is IF NOT EXISTS / INSERT OR IGNORE, so concurrent workers or
re-runs are safe and never drop data. The version row is written last.
"""
from __future__ import annotations

import time
from typing import Any

SCHEMA_TABLE = "episode_cache_schema"

# (version, name, [statements])
MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (1, "episode_cache_v1", [
        """CREATE TABLE IF NOT EXISTS episode_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            series_id TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            episode_number INTEGER,
            video_url TEXT,
            duration INTEGER,
            next_episode_id TEXT,
            next_episode_number INTEGER,
            extra_json TEXT,
            fetched_at INTEGER NOT NULL,
            metadata_expires_at INTEGER NOT NULL,
            video_url_expires_at INTEGER,
            last_accessed_at INTEGER,
            hit_count INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE (provider, series_id, episode_id)
        )""",
        # The UNIQUE constraint's index serves lookups by (provider, series_id,
        # episode_id) and, via its prefix, filters by (provider, series_id).
        "CREATE INDEX IF NOT EXISTS idx_ec_meta_exp ON episode_cache (metadata_expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_ec_video_exp ON episode_cache (video_url_expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_ec_fetched ON episode_cache (fetched_at)",
        """CREATE TABLE IF NOT EXISTS episode_cache_counters (
            name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)""",
        "INSERT OR IGNORE INTO episode_cache_counters (name, value) VALUES ('total_rows', 0)",
        # Exact row count without COUNT(*) scans: maintained atomically by triggers.
        """CREATE TRIGGER IF NOT EXISTS trg_ec_count_ins AFTER INSERT ON episode_cache BEGIN
            UPDATE episode_cache_counters SET value = value + 1 WHERE name = 'total_rows'; END""",
        """CREATE TRIGGER IF NOT EXISTS trg_ec_count_del AFTER DELETE ON episode_cache BEGIN
            UPDATE episode_cache_counters SET value = value - 1 WHERE name = 'total_rows'; END""",
        """CREATE TABLE IF NOT EXISTS episode_cache_stats (
            day TEXT NOT NULL,
            provider TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0,
            misses INTEGER NOT NULL DEFAULT 0,
            expired INTEGER NOT NULL DEFAULT 0,
            coalesced INTEGER NOT NULL DEFAULT 0,
            upstream_calls INTEGER NOT NULL DEFAULT 0,
            upstream_errors INTEGER NOT NULL DEFAULT 0,
            backend_errors INTEGER NOT NULL DEFAULT 0,
            not_cacheable INTEGER NOT NULL DEFAULT 0,
            lock_timeouts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, provider)
        )""",
        """CREATE TABLE IF NOT EXISTS episode_cache_locks (
            lock_key TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            lease_expires_at INTEGER NOT NULL
        )""",
    ]),
    (2, "series_cache_v1", [
        # One row per (provider, series_id). Nullable metadata: filled by
        # GetSeries sync when RAPIDAPI_REELSHORT_DETAIL_PATH is verified;
        # episode fetches only create the minimal link row (no invented titles).
        """CREATE TABLE IF NOT EXISTS series_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            series_id TEXT NOT NULL,
            series_title TEXT,
            series_pic TEXT,
            episode_count INTEGER,
            description TEXT,
            language TEXT,
            fetched_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            metadata_expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE (provider, series_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sc_fetched ON series_cache (fetched_at)",
        # Ordered episode listing per series without scanning.
        "CREATE INDEX IF NOT EXISTS idx_ec_series_epnum ON "
        "episode_cache (provider, series_id, episode_number)",
    ]),
]

LATEST = MIGRATIONS[-1][0]


def current_version(conn: Any) -> int:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)")
    row = conn.execute(f"SELECT COALESCE(MAX(version), 0) FROM {SCHEMA_TABLE}").fetchone()
    return int(row[0] or 0)


def migrate(conn: Any) -> list[int]:
    """Apply pending migrations. Idempotent; returns versions applied by this call."""
    applied: list[int] = []
    cur = current_version(conn)
    for version, name, statements in MIGRATIONS:
        if version <= cur:
            continue
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(f"INSERT OR IGNORE INTO {SCHEMA_TABLE} (version, name, applied_at) "
                     "VALUES (?, ?, ?)", (version, name, int(time.time())))
        applied.append(version)
    return applied
