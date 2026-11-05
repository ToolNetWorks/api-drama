"""Central gateway configuration. Reads .env, provides typed settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (ValueError, AttributeError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # Upstream RapidAPI ReelShort — exact env names required by spec.
    rapid_host: str
    rapid_key_1: str
    rapid_key_2: str
    rapid_enabled: bool
    # Verified upstream paths. Empty string = NOT verified / not configured.
    upstream_search_path: str
    upstream_detail_path: str
    upstream_episodes_path: str
    # Owner-provided validation fixture for Connection Test against the real
    # episode endpoint (single request per test run). Empty = key-probe only.
    rapid_test_series_id: str
    rapid_test_episode_id: str
    upstream_timeout_s: float
    upstream_max_retries: int
    # Gateway secrets / behaviour
    master_key: str
    allow_video_urls: bool
    require_distributable_flag: bool
    rate_limit_default_rpm: int
    cache_ttl_search_s: int
    cache_ttl_detail_s: int
    cache_ttl_episode_s: int
    # Multi-connection pool
    pool_strategy: str  # round_robin | fill_first
    pool_rotation_enabled: bool
    pool_cooldown_s: int
    # Admin session auth (owner-only dashboard; no customer dashboard)
    admin_session_ttl_s: int
    cookie_secure: bool
    login_max_attempts: int
    login_window_s: int
    # Database: PostgreSQL (Layerbase), libSQL (Turso/Layerbase), or local SQLite
    database_url: str
    auth_token: str
    use_postgres: bool
    use_libsql: bool
    db_path: Path
    log_file: Path
    gateway_port: int


def load_settings() -> Settings:
    timeout_raw = os.getenv("UPSTREAM_TIMEOUT_S", "8").strip()
    try:
        timeout_s = float(timeout_raw)
    except ValueError:
        timeout_s = 8.0
    timeout_s = min(max(timeout_s, 1.0), 30.0)
    strategy = os.getenv("POOL_STRATEGY", "round_robin").strip().lower()
    if strategy not in ("round_robin", "fill_first"):
        strategy = "round_robin"
    
    # Database config: PostgreSQL (Layerbase), libSQL (Turso/Layerbase), or local SQLite
    database_url = _get("DATABASE_URL", "").strip()
    auth_token = _get("DATABASE_AUTH_TOKEN", "").strip()
    use_postgres = bool(database_url and database_url.startswith("postgresql://"))
    use_libsql = bool(database_url and database_url.startswith("libsql://"))
    
    # Legacy SQLite path (used when not using postgres/libsql)
    db_path = Path(_get("DB_PATH", str(ROOT / "data" / "reelshort.sqlite3")))
    
    return Settings(
        rapid_host=_get("RAPIDAPI_REELSHORT_HOST", ""),
        rapid_key_1=_get("RAPIDAPI_REELSHORT_KEY_1", ""),
        rapid_key_2=_get("RAPIDAPI_REELSHORT_KEY_2", ""),
        rapid_enabled=_get_bool("RAPIDAPI_REELSHORT_ENABLED", True),
        upstream_search_path=_get("RAPIDAPI_REELSHORT_SEARCH_PATH", ""),
        upstream_detail_path=_get("RAPIDAPI_REELSHORT_DETAIL_PATH", ""),
        upstream_episodes_path=_get("RAPIDAPI_REELSHORT_EPISODES_PATH", "/reelshort/series/{id}/episodes/{episode_id}"),
        rapid_test_series_id=_get("RAPIDAPI_REELSHORT_TEST_SERIES_ID", ""),
        rapid_test_episode_id=_get("RAPIDAPI_REELSHORT_TEST_EPISODE_ID", ""),
        upstream_timeout_s=timeout_s,
        upstream_max_retries=max(0, min(_get_int("UPSTREAM_MAX_RETRIES", 1), 2)),
        master_key=_get("TOOLNET_MASTER_KEY", ""),
        allow_video_urls=_get_bool("ALLOW_VIDEO_URLS", True),
        require_distributable_flag=_get_bool("REQUIRE_DISTRIBUTABLE_FLAG", False),
        rate_limit_default_rpm=max(1, _get_int("RATE_LIMIT_DEFAULT_RPM", 60)),
        cache_ttl_search_s=max(0, _get_int("CACHE_TTL_SEARCH_S", 300)),
        cache_ttl_detail_s=max(0, _get_int("CACHE_TTL_DETAIL_S", 3600)),
        cache_ttl_episode_s=max(0, _get_int("CACHE_TTL_EPISODE_S", 600)),
        pool_strategy=strategy,
        pool_rotation_enabled=_get_bool("POOL_ROTATION_ENABLED", True),
        pool_cooldown_s=max(10, min(_get_int("POOL_COOLDOWN_S", 300), 3600)),
        admin_session_ttl_s=max(600, min(_get_int("ADMIN_SESSION_TTL_H", 12) * 3600, 72 * 3600)),
        cookie_secure=_get_bool("COOKIE_SECURE", False),
        login_max_attempts=max(3, min(_get_int("LOGIN_MAX_ATTEMPTS", 10), 100)),
        login_window_s=max(60, min(_get_int("LOGIN_WINDOW_S", 600), 3600)),
        database_url=database_url,
        auth_token=auth_token,
        use_postgres=use_postgres,
        use_libsql=use_libsql,
        db_path=db_path,
        log_file=Path(_get("LOG_FILE", str(ROOT / "logs" / "api-drama.log"))),
        gateway_port=_get_int("GATEWAY_PORT", 8090),
    )


settings = load_settings()
