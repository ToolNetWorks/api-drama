"""libSQL (Turso) client: bounded sync connection pool behind a thread executor.

SDK facts (verified against libsql 0.1.11): the official `libsql` package speaks
`libsql://` natively (Hrana) but is SYNCHRONOUS and has no async API or pool.
So: a small bounded pool of connections, every call executed on a dedicated
ThreadPoolExecutor and awaited with a hard timeout, so the event loop never
blocks. Connections are reused across requests, discarded on error, and the URL
is never rewritten to http(s).

Reliability: transient failures retry with exponential backoff + full jitter
(bounded), and a circuit breaker stops hammering a dead backend.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from app.episode_cache.config import CacheConfig

logger = logging.getLogger("toolnet.episode_cache.client")

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")


class CacheBackendError(Exception):
    """Cache backend unavailable/failed. `transient` => retry may help."""

    def __init__(self, message: str, transient: bool = True):
        super().__init__(message)
        self.transient = transient


def scrub(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove the auth token (literal or JWT-shaped) from any message."""
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return _JWT_RE.sub("***", out)[:300]


_PERMANENT_MARKERS = ("401", "403", "unauthorized", "forbidden", "syntax error",
                      "no such table", "no such column", "constraint", "malformed")


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    msg = str(exc).lower()
    if any(m in msg for m in _PERMANENT_MARKERS):
        return False
    # Hrana http/stream errors, 5xx/429, dns, reset, expired stream: transient.
    return True


class TursoClient:
    def __init__(self, cfg: CacheConfig, connect_fn: Optional[Callable[[], Any]] = None):
        self._cfg = cfg
        self._connect_fn = connect_fn or self._default_connect
        self._pool: "queue.Queue[Any]" = queue.Queue()
        self._created = 0
        self._plock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=cfg.pool_size,
                                            thread_name_prefix="episode-cache")
        self._fail_streak = 0
        self._open_until = 0.0
        self._last_error: Optional[str] = None
        self._last_latency_ms: Optional[float] = None
        self._closed = False

    # -- connection management ------------------------------------------------
    def _default_connect(self) -> Any:
        import libsql  # imported lazily: app still boots if the wheel is missing

        return libsql.connect(self._cfg.database_url, auth_token=self._cfg.auth_token,
                              timeout=self._cfg.timeout_s, autocommit=True,
                              _check_same_thread=False)

    def _acquire(self) -> Any:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._plock:
            if self._created < self._cfg.pool_size:
                self._created += 1
                try:
                    return self._connect_fn()
                except BaseException:
                    self._created -= 1
                    raise
        return self._pool.get(timeout=self._cfg.timeout_s)

    def _release(self, conn: Any) -> None:
        self._pool.put(conn)

    def _discard(self, conn: Any) -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        with self._plock:
            self._created = max(0, self._created - 1)

    def _run_sync(self, fn: Callable[[Any], Any]) -> Any:
        conn = self._acquire()
        try:
            result = fn(conn)
        except BaseException:
            self._discard(conn)  # stream state unknown after an error
            raise
        self._release(conn)
        return result

    # -- breaker --------------------------------------------------------------
    @property
    def breaker_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _record_ok(self, latency_ms: float) -> None:
        self._fail_streak = 0
        self._open_until = 0.0
        self._last_latency_ms = latency_ms

    def _record_fail(self, exc: BaseException) -> None:
        self._fail_streak += 1
        self._last_error = type(exc).__name__ + ": " + scrub(str(exc), (self._cfg.auth_token,))
        if self._fail_streak >= self._cfg.breaker_threshold:
            self._open_until = time.monotonic() + self._cfg.breaker_cooldown_s
            logger.warning("episode cache breaker open for %.0fs (%s)",
                           self._cfg.breaker_cooldown_s, type(exc).__name__)

    # -- public ---------------------------------------------------------------
    async def run(self, fn: Callable[[Any], Any], *, retries: Optional[int] = None) -> Any:
        """Run fn(conn) off-loop with timeout + bounded retry. Raises CacheBackendError."""
        if self._closed:
            raise CacheBackendError("client closed", transient=False)
        if self.breaker_open:
            raise CacheBackendError("circuit breaker open", transient=True)
        attempts = 1 + (self._cfg.max_retries if retries is None else max(0, retries))
        loop = asyncio.get_running_loop()
        last: Optional[BaseException] = None
        for n in range(attempts):
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, self._run_sync, fn),
                    timeout=self._cfg.timeout_s)
                self._record_ok((time.monotonic() - t0) * 1000)
                return result
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                last = exc
                transient = _is_transient(exc)
                if not transient or n == attempts - 1:
                    break
                delay = random.uniform(0, self._cfg.retry_base_s * (2 ** n))  # full jitter
                await asyncio.sleep(delay)
        assert last is not None
        self._record_fail(last)
        safe = scrub(str(last), (self._cfg.auth_token,))
        raise CacheBackendError(f"{type(last).__name__}: {safe}",
                                transient=_is_transient(last)) from None

    def status(self) -> dict:
        return {"breaker_open": self.breaker_open, "fail_streak": self._fail_streak,
                "last_error": self._last_error,
                "last_latency_ms": None if self._last_latency_ms is None
                else round(self._last_latency_ms, 1),
                "pool_size": self._cfg.pool_size, "connections": self._created}

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break
            except Exception:  # noqa: BLE001
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)
