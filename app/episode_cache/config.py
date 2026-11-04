"""Episode cache configuration (env-driven). Secrets are never repr'd or logged.

The cache is OFF unless EPISODE_CACHE_ENABLED=true AND backend=turso AND both
TURSO_DATABASE_URL / TURSO_AUTH_TOKEN are present. Missing credentials never
produce fake defaults; the gateway simply keeps its pre-cache behaviour.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import app.core.config  # noqa: F401  (loads .env before we read os.environ)


def _s(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _i(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(_s(name, str(default)))
    except ValueError:
        v = default
    return max(lo, min(v, hi))


def _f(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(_s(name, str(default)))
    except ValueError:
        v = default
    return max(lo, min(v, hi))


def _b(name: str, default: bool) -> bool:
    raw = _s(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _prov(provider: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", provider.upper())


@dataclass(frozen=True)
class CacheConfig:
    enabled: bool = False
    backend: str = "turso"
    database_url: str = ""
    auth_token: str = field(default="", repr=False)
    metadata_ttl_s: int = 2592000
    video_url_ttl_s: int = 86400
    timeout_s: float = 5.0
    max_retries: int = 2
    retry_base_s: float = 0.15
    pool_size: int = 3
    breaker_cooldown_s: float = 30.0
    breaker_threshold: int = 3
    fallback_to_upstream: bool = True
    # Singleflight over Turso lease rows.
    lock_lease_s: int = 60
    lock_wait_s: float = 20.0
    # Batched usage metrics (hit_count / last_accessed_at / stats).
    flush_interval_s: float = 30.0
    flush_max_keys: int = 200
    # auto = store video URLs in Turso only while ALLOW_VIDEO_URLS=true.
    store_video_url: str = "auto"
    # Operator-set safety margin subtracted from explicit URL expiry.
    video_expiry_margin_s: int = 60
    max_extra_bytes: int = 8192
    respect_cache_control: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.database_url and self.auth_token)

    @property
    def active(self) -> bool:
        return self.enabled and self.backend == "turso" and self.configured

    def missing(self) -> list[str]:
        out = []
        if not self.database_url:
            out.append("TURSO_DATABASE_URL")
        if not self.auth_token:
            out.append("TURSO_AUTH_TOKEN")
        return out

    def metadata_ttl(self, provider: str) -> int:
        return _i(f"EPISODE_METADATA_TTL_SECONDS_{_prov(provider)}", self.metadata_ttl_s, 60, 365 * 86400)

    def video_ttl(self, provider: str) -> int:
        return _i(f"EPISODE_VIDEO_URL_TTL_SECONDS_{_prov(provider)}", self.video_url_ttl_s, 30, 30 * 86400)


def load_cache_config() -> CacheConfig:
    return CacheConfig(
        enabled=_b("EPISODE_CACHE_ENABLED", False),
        backend=_s("EPISODE_CACHE_BACKEND", "turso").lower(),
        database_url=_s("TURSO_DATABASE_URL"),
        auth_token=_s("TURSO_AUTH_TOKEN"),
        metadata_ttl_s=_i("EPISODE_METADATA_TTL_SECONDS", 2592000, 60, 365 * 86400),
        video_url_ttl_s=_i("EPISODE_VIDEO_URL_TTL_SECONDS", 86400, 30, 30 * 86400),
        timeout_s=_f("EPISODE_CACHE_TIMEOUT_SECONDS", 5.0, 0.5, 30.0),
        max_retries=_i("EPISODE_CACHE_MAX_RETRIES", 2, 0, 4),
        pool_size=_i("EPISODE_CACHE_POOL_SIZE", 3, 1, 8),
        breaker_cooldown_s=_f("EPISODE_CACHE_BREAKER_COOLDOWN_SECONDS", 30.0, 1.0, 600.0),
        fallback_to_upstream=_b("EPISODE_CACHE_FALLBACK_TO_UPSTREAM", True),
        lock_lease_s=_i("EPISODE_CACHE_LOCK_LEASE_SECONDS", 60, 5, 300),
        lock_wait_s=_f("EPISODE_CACHE_LOCK_WAIT_SECONDS", 20.0, 1.0, 120.0),
        flush_interval_s=_f("EPISODE_CACHE_FLUSH_INTERVAL_SECONDS", 30.0, 1.0, 600.0),
        store_video_url=_s("EPISODE_CACHE_STORE_VIDEO_URL", "auto").lower(),
        respect_cache_control=_b("EPISODE_CACHE_RESPECT_CACHE_CONTROL", True),
    )
