"""Episode cache service: cache-first lookup, singleflight, batched metrics, health.

Singleflight (two layers, neither alone is the guarantee):
  1. in-process: one asyncio Task per cache key, followers await it;
  2. cross-worker/container: a lease row in Turso acquired with an atomic
     conditional UPSERT (`episode_cache_locks`, server clock, lease timeout).
     A dead holder's lease simply expires; waiters then take over.
This is best-effort deduplication, NOT an exactly-once guarantee: if Turso is
unreachable, or an upstream call outlives the lease/wait budget, a bounded
duplicate upstream call can happen (counted as `lock_timeouts` /
`backend_errors`). See docs/EPISODE_CACHE.md.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit

from app.episode_cache import policy, repo, schema
from app.episode_cache.client import CacheBackendError, TursoClient
from app.episode_cache.config import CacheConfig, load_cache_config
from app.providers.base import UpstreamError

logger = logging.getLogger("toolnet.episode_cache")


@dataclass
class FetchResult:
    data: Any
    connection_id: Optional[int]
    via: str
    cache_control: Optional[str] = None


@dataclass
class ProviderHooks:
    fetch: Callable[[str, str], Awaitable[FetchResult]]
    wants_video: Callable[[dict], bool]
    store_video: Callable[[], bool]
    series_meta: Optional[Callable[[str, Any], Optional[dict]]] = None


@dataclass
class CacheResult:
    payload: Any
    hit: bool
    source: str
    connection_id: Optional[int] = None
    persisted: bool = False


HOOKS: dict[str, ProviderHooks] = {}


def register_hooks(provider: str, hooks: ProviderHooks) -> None:
    HOOKS[provider] = hooks


class EpisodeCacheService:
    def __init__(self, cfg: CacheConfig, client: Optional[TursoClient] = None):
        self.cfg = cfg
        self.client: Optional[TursoClient] = client if client is not None else (
            TursoClient(cfg) if cfg.active else None)
        self._ready = False
        self._ready_lock: Optional[asyncio.Lock] = None
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._pending_hits: dict[tuple, list] = {}
        self._flush_task: Optional[asyncio.Task] = None
        self._stats_cache: tuple[float, dict] = (0.0, {})
        self._last_flush_ok: Optional[bool] = None
        self._last_housekeeping = 0.0

    # -- state ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        # client is only built for an enabled+configured turso config (or injected by tests).
        return self.client is not None and self.cfg.enabled and self.cfg.backend == "turso"

    def _count(self, provider: str, name: str, n: int = 1) -> None:
        self._counters[provider][name] += n

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        if self._ready_lock is None:
            self._ready_lock = asyncio.Lock()
        async with self._ready_lock:
            if self._ready:
                return
            applied = await self.client.run(schema.migrate)  # type: ignore[union-attr]
            if applied:
                logger.info("episode cache migrations applied: %s", applied)
            self._ready = True

    # -- lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        if not self.active:
            return
        try:
            await self.ensure_ready()
        except CacheBackendError as exc:
            logger.warning("episode cache not ready at startup (will retry lazily): %s", exc)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._flush_task = None
        if self.active:
            await self.flush()
        if self.client is not None:
            self.client.close()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.flush_interval_s)
            try:
                await self.flush()
            except Exception as exc:  # noqa: BLE001 — metrics must never kill the loop
                logger.warning("episode cache flush error: %s", type(exc).__name__)

    # -- freshness --------------------------------------------------------------
    def _evaluate(self, row: dict, hooks: ProviderHooks, now: int) -> tuple[str, Optional[dict]]:
        payload = policy.rebuild_payload(row, include_video=policy.video_fresh(row, now))
        if payload is None:
            return "unusable", None
        if not policy.metadata_fresh(row, now):
            return "expired_meta", None
        if hooks.wants_video(payload) and not policy.video_fresh(row, now):
            return "expired_video", None
        return "hit", payload

    def _hit(self, provider: str, key: tuple, payload: dict, coalesced: bool = False) -> CacheResult:
        self._count(provider, "coalesced" if coalesced else "hits")
        ent = self._pending_hits.setdefault(key, [0, 0])
        ent[0] += 1
        ent[1] = int(time.time())
        return CacheResult(payload=payload, hit=True, source="cache:turso", persisted=True)

    # -- main entry -------------------------------------------------------------
    async def get_episode(self, provider: str, series_id: str, episode_id: str) -> CacheResult:
        hooks = HOOKS[provider]
        key = (provider, series_id, episode_id)
        backend_ok = True
        row: Optional[dict] = None
        try:
            await self.ensure_ready()
            row = await self.client.run(lambda c: repo.get_row(c, *key))  # type: ignore[union-attr]
        except CacheBackendError as exc:
            backend_ok = False
            self._count(provider, "backend_errors")
            logger.warning("episode cache read failed: %s", exc)
        now = int(time.time())
        kind: Optional[str] = None  # counted only when this request actually triggers a fetch
        if row is not None:
            verdict, payload = self._evaluate(row, hooks, now)
            if verdict == "hit" and payload is not None:
                return self._hit(provider, key, payload)
            kind = "expired"
        elif backend_ok:
            kind = "misses"
        if not backend_ok and not self.cfg.fallback_to_upstream:
            raise UpstreamError("CACHE_UNAVAILABLE", "Episode cache backend unavailable", 503)

        task = self._inflight.get(key)
        leader = task is None
        if task is None:
            task = asyncio.create_task(self._load(key, hooks, backend_ok, kind=kind))
            self._inflight[key] = task

            def _done(t: asyncio.Task, k: tuple = key) -> None:
                self._inflight.pop(k, None)
                if not t.cancelled():
                    t.exception()  # mark retrieved; followers/leader re-raise it themselves
            task.add_done_callback(_done)
        res: CacheResult = await asyncio.shield(task)
        if not leader:
            # Follower of an in-process flight: served from the leader's single upstream call.
            self._count(provider, "coalesced")
            if not res.hit:
                res = replace(res, hit=True, connection_id=None,
                              source="cache:turso" if res.persisted else "cache:inflight")
        return res

    async def refresh(self, provider: str, series_id: str, episode_id: str) -> CacheResult:
        """Admin: force an upstream refresh (still singleflight-coordinated)."""
        hooks = HOOKS[provider]
        return await self._load((provider, series_id, episode_id), hooks, True, force=True)

    # -- load (leader) ----------------------------------------------------------
    async def _load(self, key: tuple, hooks: ProviderHooks, backend_ok: bool,
                    force: bool = False, kind: Optional[str] = None) -> CacheResult:
        provider, series_id, episode_id = key
        owner = uuid.uuid4().hex
        lock_key = f"{provider}:{series_id}:{episode_id}"
        have_lock = False
        deadline = time.monotonic() + self.cfg.lock_wait_s
        delay = 0.2
        c = self.client
        try:
            while backend_ok:
                try:
                    got = await c.run(lambda cn: repo.try_acquire_lock(  # type: ignore[union-attr]
                        cn, lock_key, owner, self.cfg.lock_lease_s), retries=0)
                except CacheBackendError as exc:
                    backend_ok = False
                    self._count(provider, "backend_errors")
                    logger.warning("episode cache lock failed: %s", exc)
                    break
                if got:
                    have_lock = True
                    break
                if time.monotonic() >= deadline:
                    self._count(provider, "lock_timeouts")
                    logger.warning("singleflight wait exceeded; proceeding uncoordinated (bounded)")
                    break
                await asyncio.sleep(random.uniform(delay * 0.5, delay))
                delay = min(delay * 1.6, 1.0)
                try:
                    row = await c.run(lambda cn: repo.get_row(cn, *key), retries=0)  # type: ignore[union-attr]
                except CacheBackendError:
                    backend_ok = False
                    break
                if row is not None and not force:
                    verdict, payload = self._evaluate(row, hooks, int(time.time()))
                    if verdict == "hit" and payload is not None:
                        return self._hit(provider, key, payload, coalesced=True)
            if have_lock and not force:
                try:
                    row = await c.run(lambda cn: repo.get_row(cn, *key))  # type: ignore[union-attr]
                    if row is not None:
                        verdict, payload = self._evaluate(row, hooks, int(time.time()))
                        if verdict == "hit" and payload is not None:
                            return self._hit(provider, key, payload, coalesced=True)
                except CacheBackendError:
                    pass
            return await self._fetch_and_store(key, hooks, kind)
        finally:
            if have_lock:
                try:
                    await asyncio.shield(c.run(  # type: ignore[union-attr]
                        lambda cn: repo.release_lock(cn, lock_key, owner), retries=1))
                except (CacheBackendError, asyncio.CancelledError):
                    pass  # lease expiry recovers it

    async def _fetch_and_store(self, key: tuple, hooks: ProviderHooks,
                               kind: Optional[str] = None) -> CacheResult:
        provider, series_id, episode_id = key
        self._count(provider, "upstream_calls")
        if kind:
            self._count(provider, kind)
        try:
            fr = await hooks.fetch(series_id, episode_id)
        except BaseException:
            self._count(provider, "upstream_errors")
            raise  # errors are never cached; existing rows are left untouched
        persisted = False
        if self.active:
            mode = self.cfg.store_video_url
            store_video = hooks.store_video() if mode == "auto" else mode == "true"
            rec, reason = policy.normalize(
                self.cfg, provider, series_id, episode_id, fr.data, int(time.time()),
                store_video=store_video, cache_control=fr.cache_control,
                respect_cache_control=self.cfg.respect_cache_control)
            if rec is None:
                self._count(provider, "not_cacheable")
                logger.info("episode not cached provider=%s reason=%s", provider, reason)
            else:
                try:
                    smeta = hooks.series_meta(series_id, fr.data) if hooks.series_meta else None
                    ttl_s = self.cfg.metadata_ttl(provider)
                    srec = None
                    if isinstance(smeta, dict):
                        srec, _ = policy.normalize_series(
                            provider, series_id, rec.fetched_at, ttl_s,
                            title=smeta.get("title"), pic=smeta.get("pic"),
                            episode_count=smeta.get("episode_count"),
                            description=smeta.get("description"),
                            language=smeta.get("language"))

                    def _store(conn: Any) -> None:
                        repo.upsert(conn, rec)
                        if srec is not None:
                            repo.upsert_series(conn, srec)
                        else:
                            repo.ensure_series_minimal(
                                conn, provider, series_id, rec.fetched_at, ttl_s)
                    await self.client.run(_store)  # type: ignore[arg-type]
                    persisted = True
                except CacheBackendError as exc:
                    self._count(provider, "backend_errors")
                    logger.warning("episode cache upsert failed: %s", exc)
        return CacheResult(payload=fr.data, hit=False, source=f"upstream:{fr.via}",
                           connection_id=fr.connection_id, persisted=persisted)

    # -- series catalog ---------------------------------------------------------
    async def resolve_episode_id(self, provider: str, series_id: str,
                                 episode_number: int) -> Optional[str]:
        """Resolve an episode number (1, 2, 3...) to its episode_id from Turso."""
        if not self.active:
            return None
        try:
            await self.ensure_ready()
            return await self.client.run(  # type: ignore[return-value]
                lambda c: repo.resolve_episode_id_by_number(c, provider, series_id, episode_number))
        except CacheBackendError as exc:
            logger.warning("episode number resolve failed: %s", exc)
            return None

    async def find_preceding_episode(self, provider: str, series_id: str,
                                     target_number: int) -> Optional[tuple[int, str, Optional[int], Optional[str]]]:
        """Find the closest cached episode < target_number in Turso."""
        if not self.active:
            return None
        try:
            await self.ensure_ready()
            return await self.client.run(  # type: ignore[return-value]
                lambda c: repo.find_closest_preceding_episode(c, provider, series_id, target_number))
        except CacheBackendError as exc:
            logger.warning("preceding episode lookup failed: %s", exc)
            return None

    async def sync_series_list(self, provider: str, series_id: str, *,
                               title: Any = None, pic: Any = None,
                               episode_count: Any = None, description: Any = None,
                               language: Any = None,
                               episodes: Optional[list] = None,
                               metadata_ttl_s: Optional[int] = None) -> dict:
        """Upsert a series row + episode skeletons (INSERT OR IGNORE).

        Used by future GetSeries integration (when DETAIL path is verified) and
        by backfill with already-verified IDs. Never overwrites a fully fetched
        episode row with NULLs; skips episode 0 (trailer); never fetches upstream.
        """
        await self.ensure_ready()
        now = int(time.time())
        ttl = metadata_ttl_s if metadata_ttl_s and metadata_ttl_s > 0 else self.cfg.metadata_ttl(provider)
        rec, reason = policy.normalize_series(
            provider, series_id, now, ttl, title=title, pic=pic,
            episode_count=episode_count, description=description, language=language)
        if rec is None:
            raise ValueError(f"invalid series payload: {reason}")

        def _work(conn: Any) -> dict:
            repo.upsert_series(conn, rec)
            created = skipped = 0
            for item in episodes or []:
                try:
                    num, eid = item[0], item[1]
                except (TypeError, IndexError, KeyError):
                    skipped += 1
                    continue
                if not policy.validate_skeleton(num, eid):
                    skipped += 1
                    continue
                if repo.insert_episode_skeleton(conn, provider, series_id.strip(),
                                               int(num), str(eid).strip(), now, ttl):
                    created += 1
            return {"series_upserted": 1, "skeletons_created": created,
                    "skeletons_skipped": skipped}
        return await self.client.run(_work)  # type: ignore[return-value]

    async def get_series_catalog(self, provider: str, series_id: str) -> Optional[dict]:
        await self.ensure_ready()
        return await self.client.run(  # type: ignore[return-value]
            lambda c: self._series_with_counts(c, provider, series_id))

    @staticmethod
    def _series_with_counts(conn: Any, provider: str, series_id: str) -> Optional[dict]:
        row = repo.get_series_row(conn, provider, series_id)
        if row is None:
            # Legacy episodes may predate series_cache: still report counts so the
            # series is visible as soon as one episode exists.
            counts = repo.count_series_episodes(conn, provider, series_id)
            if not counts["episodes_tracked"]:
                return None
            row = {"provider": provider, "series_id": series_id, "series_title": None,
                   "series_pic": None, "episode_count": None, "description": None,
                   "language": None, "fetched_at": None, "updated_at": None,
                   "metadata_expires_at": None}
        else:
            counts = repo.count_series_episodes(conn, provider, series_id)
        return {**row, **counts}

    async def list_series_catalog(self, *, provider: str = "", q: str = "",
                                  limit: int = 25, offset: int = 0) -> dict:
        await self.ensure_ready()
        page = await self.client.run(  # type: ignore[return-value]
            lambda c: repo.list_series(c, provider=provider, q=q, limit=limit, offset=offset))

        def _with_counts(conn: Any) -> list:
            out = []
            for item in page["items"]:
                counts = repo.count_series_episodes(conn, item["provider"], item["series_id"])
                out.append({**item, **counts})
            return out
        page["items"] = await self.client.run(_with_counts)  # type: ignore[assignment]
        return page

    async def list_series_episodes(self, provider: str, series_id: str, *,
                                   limit: int = 200, offset: int = 0) -> dict:
        await self.ensure_ready()
        return await self.client.run(  # type: ignore[return-value]
            lambda c: repo.list_episodes(c, provider, series_id, limit=limit, offset=offset))

    # -- batched metrics --------------------------------------------------------
    async def flush(self) -> None:
        if not self.active or not self._ready:
            return
        keys = list(self._pending_hits)[: self.cfg.flush_max_keys]
        hits = [(self._pending_hits[k][0], self._pending_hits[k][1], *k) for k in keys]
        for k in keys:
            self._pending_hits.pop(k, None)
        counters = {p: dict(c) for p, c in self._counters.items() if any(c.values())}
        self._counters = defaultdict(lambda: defaultdict(int))
        if not hits and not counters:
            return
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        do_housekeeping = time.monotonic() - self._last_housekeeping > 3600

        def work(conn: Any) -> None:
            if hits:
                repo.flush_hits(conn, hits)
            if counters:
                repo.flush_stats(conn, day, counters)
            if do_housekeeping:
                repo.housekeeping(conn)
        try:
            await self.client.run(work, retries=0)  # type: ignore[union-attr]
            self._last_flush_ok = True
            if do_housekeeping:
                self._last_housekeeping = time.monotonic()
        except CacheBackendError as exc:
            self._last_flush_ok = False
            logger.warning("episode cache metrics flush failed (re-queued): %s", exc)
            for (n, ts, *k) in hits:
                ent = self._pending_hits.setdefault(tuple(k), [0, 0])
                ent[0] += n
                ent[1] = max(ent[1], ts)
            for p, c in counters.items():
                for name, n in c.items():
                    self._counters[p][name] += n
            if len(self._pending_hits) > self.cfg.flush_max_keys * 5:
                for k in list(self._pending_hits)[self.cfg.flush_max_keys * 5:]:
                    self._pending_hits.pop(k, None)

    def _local_pending(self) -> dict[str, int]:
        tot: dict[str, int] = defaultdict(int)
        for c in self._counters.values():
            for k, v in c.items():
                tot[k] += v
        return tot

    # -- admin / observability ---------------------------------------------------
    async def stats(self, force: bool = False) -> dict:
        if not self.active:
            return {"available": False}
        ts, cached = self._stats_cache
        if not force and time.monotonic() - ts < 15 and cached:
            data = dict(cached)
        else:
            await self.ensure_ready()
            data = await self.client.run(lambda c: repo.read_stats(c, int(time.time())))  # type: ignore[union-attr]
            self._stats_cache = (time.monotonic(), dict(data))
        pend = self._local_pending()
        hits = data["hits"] + data["coalesced"] + pend.get("hits", 0) + pend.get("coalesced", 0)
        misses = data["misses"] + data["expired_events"] + pend.get("misses", 0) + pend.get("expired", 0)
        total = hits + misses
        data.update({
            "available": True,
            "cache_hits": hits, "cache_misses": misses,
            "hit_rate_pct": round(100.0 * hits / total, 1) if total else None,
            # ESTIMATE: every Turso-served hit is counted as one avoided upstream call. The
            # legacy 10-min SQLite/memory cache may have absorbed some of these anyway, and
            # RapidAPI billing/overage rules are unknown here, so this is an upper bound on
            # requests saved, not guaranteed money saved.
            "estimated_upstream_saved": hits,
            "estimated_note": "Estimate only: counts cache hits that would otherwise have needed "
                              "an upstream call; not a guaranteed cost saving.",
            "unflushed_local_events": sum(pend.values()),
        })
        return data

    async def health(self) -> dict:
        info: dict[str, Any] = {"backend": self.cfg.backend, "enabled": self.cfg.enabled}
        if not self.cfg.enabled:
            info["status"] = "disabled"
            return info
        if not self.cfg.configured and not self.active:
            info.update(status="unconfigured", missing=self.cfg.missing())
            return info
        try:
            host = urlsplit(self.cfg.database_url).hostname
        except ValueError:
            host = None
        if host:
            info["host"] = host
        t0 = time.monotonic()
        try:
            await self.ensure_ready()
            ver = await self.client.run(repo.schema_version, retries=0)  # type: ignore[union-attr]
            info.update(status="ok", schema_version=ver, latest_schema_version=schema.LATEST,
                        latency_ms=round((time.monotonic() - t0) * 1000, 1))
        except CacheBackendError as exc:
            info.update(status="down", error=str(exc)[:200])
        info["client"] = self.client.status() if self.client else None
        info["last_flush_ok"] = self._last_flush_ok
        info["fallback_to_upstream"] = self.cfg.fallback_to_upstream
        return info

    async def list_rows(self, **kw: Any) -> dict:
        await self.ensure_ready()
        return await self.client.run(lambda c: repo.list_rows(c, **kw))  # type: ignore[union-attr]

    async def detail(self, row_id: int) -> Optional[dict]:
        await self.ensure_ready()
        return await self.client.run(lambda c: repo.get_detail(c, row_id))  # type: ignore[union-attr]

    async def delete(self, row_id: int) -> int:
        await self.ensure_ready()
        self._stats_cache = (0.0, {})
        return await self.client.run(lambda c: repo.delete_row(c, row_id))  # type: ignore[union-attr]

    async def invalidate_series(self, provider: str, series_id: str) -> int:
        await self.ensure_ready()
        self._stats_cache = (0.0, {})
        return await self.client.run(lambda c: repo.invalidate_series(c, provider, series_id))  # type: ignore[union-attr]


_service: Optional[EpisodeCacheService] = None


def get_service() -> EpisodeCacheService:
    global _service
    if _service is None:
        _service = EpisodeCacheService(load_cache_config())
    return _service


def configure(cfg: Optional[CacheConfig] = None,
              client: Optional[TursoClient] = None) -> EpisodeCacheService:
    """Replace the singleton (used by tests and by startup after env changes)."""
    global _service
    _service = EpisodeCacheService(cfg or load_cache_config(), client)
    return _service
