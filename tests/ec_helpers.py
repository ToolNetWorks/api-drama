"""Shared helpers for episode-cache tests.

The Turso backend is replaced by a LOCAL libSQL file opened through the same `libsql`
SDK and the same TursoClient code path (pool, thread executor, timeout, retry, breaker).
Only the transport differs (file vs libsql://). Nothing here ever touches a real Turso DB.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import libsql

from app.episode_cache.client import TursoClient
from app.episode_cache.config import CacheConfig
from app.episode_cache.service import (
    EpisodeCacheService, FetchResult, ProviderHooks, register_hooks,
)
from app.providers.base import UpstreamError

TEST_TOKEN = "test-token-NOT-A-REAL-SECRET-0123456789"
EP_PAYLOAD = {
    "episode": 14, "episode_id": "ep14",
    "video_url": "https://cdn.example.com/v/ep14.m3u8", "duration": 103,
    "next_episode": {"episode": 15, "episode_id": "5h6g34m5y5"},
}


def make_cfg(**kw: Any) -> CacheConfig:
    base: dict[str, Any] = dict(
        enabled=True, backend="turso", database_url="libsql://unit-test.invalid",
        auth_token=TEST_TOKEN, timeout_s=3.0, max_retries=0, retry_base_s=0.01, pool_size=3,
        breaker_cooldown_s=30.0, breaker_threshold=3, lock_lease_s=5, lock_wait_s=8.0,
        flush_interval_s=3600.0)
    base.update(kw)
    return CacheConfig(**base)


def local_connect_fn(path: str):
    def _c():
        return libsql.connect(str(path), autocommit=True, _check_same_thread=False, timeout=5)
    return _c


def make_service(path: str, **kw: Any) -> EpisodeCacheService:
    cfg = make_cfg(**kw)
    return EpisodeCacheService(cfg, TursoClient(cfg, connect_fn=local_connect_fn(path)))


def broken_service(error: Optional[BaseException] = None, **kw: Any) -> EpisodeCacheService:
    cfg = make_cfg(**kw)

    def _boom():
        raise error or ValueError(f"Hrana: http error: connection refused (token={TEST_TOKEN})")
    return EpisodeCacheService(cfg, TursoClient(cfg, connect_fn=_boom))


def sql(path: str, query: str, args: tuple = ()) -> list:
    conn = libsql.connect(str(path), autocommit=True, _check_same_thread=False)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()


class Upstream:
    """Counts upstream calls; optionally slow or failing."""

    def __init__(self, payload: Optional[dict] = None, delay: float = 0.0,
                 error: Optional[BaseException] = None, via: str = "rapidapi-1",
                 cache_control: Optional[str] = None):
        self.payload = payload
        self.delay = delay
        self.error = error
        self.via = via
        self.cache_control = cache_control
        self.calls = 0

    async def fetch(self, series_id: str, episode_id: str) -> FetchResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        data = self.payload if self.payload is not None else dict(EP_PAYLOAD, episode_id=episode_id)
        if isinstance(data, dict):
            data = dict(data)
        return FetchResult(data=data, connection_id=7, via=self.via,
                           cache_control=self.cache_control)


def register(up: Upstream, provider: str = "tp", allow_video: bool = False) -> None:
    register_hooks(provider, ProviderHooks(
        fetch=up.fetch,
        wants_video=lambda p: allow_video and isinstance(p, dict)
        and (p.get("distributable") or p.get("allow_distribution") or p.get("licenced")) is True,
        store_video=lambda: allow_video))


def upstream_error(status: int) -> UpstreamError:
    return UpstreamError(f"UPSTREAM_{status}", f"upstream status {status}", 502)
