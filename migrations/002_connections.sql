-- 002: multi-connection pool for upstream providers.
-- Safe: IF NOT EXISTS + guarded columns. Preserves series, api_keys, request_logs.
CREATE TABLE IF NOT EXISTS provider_connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'reelshort',
    name TEXT NOT NULL,
    key_encrypted TEXT NOT NULL,
    key_hint TEXT NOT NULL DEFAULT '****',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 100,
    quota_limit INTEGER,
    quota_window TEXT NOT NULL DEFAULT 'monthly',
    requests_used INTEGER NOT NULL DEFAULT 0,
    quota_reset_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    last_error TEXT,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(provider, name)
);
CREATE INDEX IF NOT EXISTS idx_conn_provider ON provider_connections(provider, enabled, priority, id);

CREATE TABLE IF NOT EXISTS provider_settings (
    provider TEXT PRIMARY KEY,
    strategy TEXT NOT NULL DEFAULT 'round_robin',
    rotation_enabled INTEGER NOT NULL DEFAULT 1,
    cooldown_seconds INTEGER NOT NULL DEFAULT 300,
    updated_at TEXT NOT NULL
);
