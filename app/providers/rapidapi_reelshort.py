"""RapidAPI ReelShort adapter over the multi-connection pool.

Reads RAPIDAPI_REELSHORT_HOST / _ENABLED from .env. Secrets (per-connection
RapidAPI keys) live encrypted in provider_connections and are decrypted only
in-memory for a single outbound call. NEVER returned, logged, or cached.

Routing policy (load-bearing):
- Round Robin / Fill First among healthy connections with independent quota,
  per provider_settings (operator-configurable, rotation_enabled flag).
- Transient failures (429/5xx/timeout/network): mark connection cooling for
  cooldown_seconds, then try the NEXT healthy connection (bounded).
- Access errors (401/403): mark connection auth_error and FAIL FAST — never
  try another account on permission errors.
- No verified upstream path (RAPIDAPI_REELSHORT_*_PATH empty) or no healthy
  connection: serve local SQLite metadata cache instead of guessing endpoints.
- Video URLs: only when ALLOW_VIDEO_URLS=true AND upstream marks distributable;
  default redacted so the gateway cannot bypass paywall/DRM.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app import episode_cache
from app.core.cache import cache
from app.core.config import settings
from app.providers import pool
from app.episode_cache.service import FetchResult, ProviderHooks
from app.providers.base import DramaProvider, UpstreamError, UpstreamNotVerified, register
from app.store import connect as store_connect, utcnow

logger = logging.getLogger("toolnet.upstream.reelshort")

RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


def _local_connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(settings.db_path), timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _row_to_series(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("tags", "shelves"):
        try:
            item[key] = json.loads(item[key]) if item.get(key) else []
        except (ValueError, TypeError):
            item[key] = []
    return item


def _sanitize(payload: Any) -> Any:
    if isinstance(payload, dict):
        cleaned: dict[str, Any] = {}
        for key, value in payload.items():
            low = key.lower()
            if any(t in low for t in ("rapidapi-key", "x-rapidapi", "api-key", "apikey",
                                      "secret", "cookie", "set-cookie", "authorization")):
                continue
            cleaned[key] = _sanitize(value)
        return cleaned
    if isinstance(payload, list):
        return [_sanitize(v) for v in payload]
    return payload


# -- persistent L2 cache (SQLite) -------------------------------------------

def _l2_get(cache_key: str) -> Optional[Any]:
    try:
        with store_connect() as db:
            row = db.execute(
                "SELECT payload, expires_at FROM cache_entries WHERE cache_key=?", (cache_key,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= exp:
            return None
        return json.loads(row["payload"])
    except (ValueError, TypeError):
        return None


def _l2_set(cache_key: str, value: Any, ttl_s: int) -> None:
    if ttl_s <= 0:
        return
    try:
        from datetime import timedelta
        exp = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
        with store_connect() as db:
            db.execute(
                """INSERT INTO cache_entries (cache_key, payload, expires_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,
                   expires_at=excluded.expires_at""",
                (cache_key, json.dumps(value, default=str)[:200000], exp),
            )
    except (sqlite3.OperationalError, ValueError, TypeError):
        pass


def _cached_get(key: str) -> Optional[Any]:
    hit = cache.get(key)
    if hit is not None:
        return hit
    return _l2_get(key)


def _cached_set(key: str, value: Any, ttl_s: int) -> None:
    cache.set(key, value, ttl_s)
    _l2_set(key, value, ttl_s)


# -- pooled upstream call ----------------------------------------------------

async def _single_attempt(client: httpx.AsyncClient, url: str, params: dict,
                          host: str, raw_key: str) -> httpx.Response:
    return await client.get(
        url, params=params,
        headers={"X-RapidAPI-Host": host, "X-RapidAPI-Key": raw_key,
                 "Accept": "application/json", "User-Agent": "ToolNet-Drama-Gateway/1.0"},
    )


async def _pooled_get(path_template: str, params: Optional[dict] = None,
                      meta: Optional[dict] = None) -> tuple[Any, int, str]:
    """GET upstream via pool. Returns (sanitized_json, connection_id, connection_name).

    Fail-fast on 401/403 (no cross-account retry). Transient errors rotate to
    the next healthy connection within a bounded number of tries.

    `meta` (optional out-param) receives the upstream `cache_control` header so the
    episode cache can honour it; other callers are unaffected.
    """
    from app.core.crypto import decrypt_key

    if not settings.rapid_enabled:
        raise UpstreamError("UPSTREAM_DISABLED", "RapidAPI ReelShort upstream is disabled", 502)
    if not settings.rapid_host:
        raise UpstreamNotVerified("RAPIDAPI_REELSHORT_HOST is not configured")
    if not path_template:
        raise UpstreamNotVerified(
            "Upstream path not verified from RapidAPI docs. "
            "Set RAPIDAPI_REELSHORT_*_PATH after docs verification.")
    master = os.getenv("TOOLNET_MASTER_KEY", "").strip()
    if not master:
        raise UpstreamError("POOL_NO_MASTER_KEY",
                            "TOOLNET_MASTER_KEY is not set; cannot use stored connections", 500)

    url = f"https://{settings.rapid_host}{path_template}"
    params = params or {}
    max_rounds = 5  # bound total connection tries per gateway request
    last_err: Optional[UpstreamError] = None

    async with httpx.AsyncClient(timeout=settings.upstream_timeout_s) as client:
        for _ in range(max_rounds):
            with store_connect() as db:
                cfg = pool.get_settings(db, "reelshort")
                cooldown_s = int(cfg.get("cooldown_seconds") or 300)
                row = pool.pick_connection(db, "reelshort")
                if row is None:
                    break  # no healthy connection left
                conn_id, conn_name = row["id"], row["name"]
                try:
                    raw_key = decrypt_key(row["key_encrypted"], master)
                except ValueError as exc:
                    pool.record_auth_failure(db, conn_id, "decrypt failed")
                    last_err = UpstreamError("POOL_DECRYPT", str(exc), 500)
                    break
            # Single outbound attempt with this connection's key (outside DB txn).
            start = time.monotonic()
            try:
                resp = await _single_attempt(client, url, params, settings.rapid_host, raw_key)
                latency_ms = int((time.monotonic() - start) * 1000)
                # Adopt real quota signals from every upstream response (all
                # statuses carry x-ratelimit-* headers). Never fails the call.
                with store_connect() as _db:
                    pool.sync_quota_from_headers(_db, conn_id, resp.headers)
            except RETRYABLE_EXCEPTIONS as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                logger.warning("upstream net err conn=%s err=%s latency_ms=%d",
                               conn_name, type(exc).__name__, latency_ms)
                with store_connect() as db:
                    pool.record_transient_failure(db, conn_id, type(exc).__name__, cooldown_s)
                last_err = UpstreamError("UPSTREAM_NETWORK",
                                         f"Upstream network error: {type(exc).__name__}", 502)
                continue  # try next healthy connection
            finally:
                raw_key = ""  # drop key material immediately

            if resp.status_code in (401, 403):
                # FAIL FAST: do not try another account on permission errors.
                logger.error("upstream access denied conn=%s status=%d", conn_name, resp.status_code)
                with store_connect() as db:
                    pool.record_auth_failure(db, conn_id, f"upstream {resp.status_code}")
                raise UpstreamError("UPSTREAM_AUTH",
                                    f"Upstream denied access (status {resp.status_code}); "
                                    "connection marked auth_error, no failover attempted", 502)
            if resp.status_code == 429 or resp.status_code >= 500:
                logger.warning("upstream transient conn=%s status=%d latency_ms=%d",
                               conn_name, resp.status_code, latency_ms)
                with store_connect() as db:
                    pool.record_transient_failure(
                        db, conn_id, f"upstream {resp.status_code}", cooldown_s)
                last_err = UpstreamError("UPSTREAM_TRANSIENT",
                                         f"Upstream status {resp.status_code}; connection cooling down", 502)
                continue
            if resp.status_code >= 400:
                logger.warning("upstream client err conn=%s status=%d", conn_name, resp.status_code)
                if resp.status_code == 404:
                    # A 404 on an operator-configured path means the path itself
                    # is wrong — record it so the dashboard shows endpoint_ok=0
                    # instead of silently looking "active".
                    with store_connect() as db:
                        pool.mark_endpoint(db, conn_id, False)
                raise UpstreamError("UPSTREAM_ERROR",
                                    f"Upstream returned status {resp.status_code}", 502)
            try:
                body = resp.json()
            except ValueError as exc:
                raise UpstreamError("UPSTREAM_BAD_RESPONSE", "Upstream returned non-JSON", 502) from exc
            logger.info("upstream ok conn=%s latency_ms=%d", conn_name, latency_ms)
            if meta is not None:
                meta["cache_control"] = resp.headers.get("cache-control")
            with store_connect() as db:
                pool.record_success(db, conn_id)
                pool.mark_endpoint(db, conn_id, True)
            return _sanitize(body), conn_id, conn_name

    if last_err is not None:
        raise last_err
    raise UpstreamError("NO_HEALTHY_CONNECTION",
                        "No healthy upstream connection available (all cooling, exhausted, "
                        "auth_error, or disabled)", 502)


@register
class RapidApiReelShortProvider(DramaProvider):
    name = "reelshort"

    async def search(self, query: str, limit: int = 20, offset: int = 0) -> dict:
        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        ckey = f"rs2:search:{query.lower().strip()}:{limit}:{offset}"
        hit = _cached_get(ckey)
        if hit is not None:
            result = dict(hit)
            result["cache_hit"] = True
            return result
        if settings.upstream_search_path:
            try:
                body, conn_id, via = await _pooled_get(
                    settings.upstream_search_path, {"q": query, "limit": limit, "offset": offset})
                result = {"query": query, "limit": limit, "offset": offset,
                          "items": body.get("items", body) if isinstance(body, dict) else body,
                          "total": body.get("total") if isinstance(body, dict) else None,
                          "source": f"upstream:{via}", "live": True, "connection_id": conn_id,
                          "upstream_verified": True, "cache_hit": False}
                _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                            settings.cache_ttl_search_s)
                return result
            except UpstreamError as exc:
                if exc.code in ("UPSTREAM_NOT_VERIFIED",):
                    raise
                logger.warning("upstream search failed (%s); falling back to local cache", exc.code)
        like = f"%{query.strip()}%" if query.strip() else "%"
        with _local_connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM series WHERE title LIKE ? OR description LIKE ?",
                (like, like)).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM series WHERE title LIKE ? OR description LIKE ? "
                "ORDER BY title LIMIT ? OFFSET ?", (like, like, limit, offset)).fetchall()
            items = [_row_to_series(r) for r in rows]
        result = {"query": query, "limit": limit, "offset": offset, "total": total,
                  "items": items, "source": "local-cache", "live": False, "upstream_verified": False,
                  "note": "NOT live data: serving local metadata cache (no verified upstream path or no healthy connection).",
                  "cache_hit": False}
        _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                    settings.cache_ttl_search_s)
        return result

    async def get_series(self, series_id: str) -> Optional[dict]:
        ckey = f"rs2:detail:{series_id}"
        hit = _cached_get(ckey)
        if hit is not None:
            result = dict(hit)
            result["cache_hit"] = True
            return result
        if settings.upstream_detail_path:
            try:
                template = settings.upstream_detail_path.replace("{id}", series_id)
                body, conn_id, via = await _pooled_get(template, None)
                data = body.get("data", body) if isinstance(body, dict) else body
                result = {"id": series_id, "detail": data, "source": f"upstream:{via}", "live": True, "connection_id": conn_id,
                          "upstream_verified": True, "cache_hit": False}
                _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                            settings.cache_ttl_detail_s)
                return result
            except UpstreamError as exc:
                if exc.code in ("UPSTREAM_NOT_VERIFIED",):
                    raise
                logger.warning("upstream detail failed (%s); falling back to local cache", exc.code)
        with _local_connect() as db:
            row = db.execute("SELECT * FROM series WHERE book_id=?", (series_id,)).fetchone()
            if row is None:
                return None
            item = _row_to_series(row)
        result = {"id": series_id, "detail": item, "source": "local-cache", "live": False,
                  "upstream_verified": False,
                  "note": "NOT live data: serving local metadata cache.", "cache_hit": False}
        _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                    settings.cache_ttl_detail_s)
        return result

    async def _resolve_episode_number(self, series_id: str, target_num: int,
                                      smeta: Optional[dict]) -> Optional[str]:
        """Resolve 1-based episode_number to episode_id using Turso cache and/or
        walking the next_episode chain (bounded to 10 steps)."""
        if target_num <= 0:
            return None
        svc = episode_cache.get_service()
        if svc.active:
            direct = await svc.resolve_episode_id(self.name, series_id, target_num)
            if direct:
                return direct
        first_id = (smeta or {}).get("first_chapter_id")
        if target_num == 1 and first_id:
            return str(first_id)
        if not (bool(settings.upstream_episodes_path) and svc.active):
            return None
        cur_num: Optional[int] = None
        cur_id: Optional[str] = None
        prec = await svc.find_preceding_episode(self.name, series_id, target_num)
        if prec is not None:
            ep_no, ep_id, nxt_no, nxt_id = prec
            if nxt_no == target_num and nxt_id:
                return nxt_id
            if nxt_no is not None and nxt_id and ep_no < nxt_no <= target_num:
                cur_num, cur_id = nxt_no, nxt_id
            else:
                cur_num, cur_id = ep_no, ep_id
        elif first_id:
            cur_num, cur_id = 1, str(first_id)
        if cur_num is None or cur_id is None or target_num - cur_num > 10:
            return None
        while cur_num < target_num and cur_id:
            res = await svc.get_episode(self.name, series_id, cur_id)
            payload = res.payload if isinstance(res.payload, dict) else {}
            nxt = payload.get("next_episode") if isinstance(payload.get("next_episode"), dict) else {}
            nxt_id = nxt.get("episode_id")
            nxt_no = nxt.get("episode")
            if not isinstance(nxt_id, str) or not nxt_id.strip():
                return None
            cur_id = nxt_id.strip()
            cur_num = int(nxt_no) if isinstance(nxt_no, int) and nxt_no > cur_num else cur_num + 1
        return cur_id if cur_num == target_num else None

    async def get_episode(self, series_id: str, episode_id: str) -> Optional[dict]:
        smeta = _lookup_series_meta(series_id)
        raw_ep = episode_id.strip()
        if raw_ep.isdigit():
            resolved_id = await self._resolve_episode_number(series_id, int(raw_ep), smeta)
            if not resolved_id:
                return None
            episode_id = resolved_id
        ckey = f"rs2:episode:{series_id}:{episode_id}"
        # Persistent Turso cache takes over the episode path ONLY when active and a verified
        # upstream path exists; otherwise behaviour is exactly the legacy L1/L2 flow. Auth,
        # provider rights, rate limit and customer quota already ran in the router dependency.
        svc = episode_cache.get_service()
        use_turso = bool(settings.upstream_episodes_path) and svc.active
        if not use_turso:
            hit = _cached_get(ckey)
            if hit is not None:
                result = dict(hit)
                result["cache_hit"] = True
                return result
        if use_turso:
            try:
                res = await svc.get_episode(self.name, series_id, episode_id)
                # Redaction runs on the FINAL payload regardless of origin (upstream or Turso).
                data = self._apply_video_policy(res.payload)
                ep_num = data.get("episode") if isinstance(data, dict) else None
                return {"series_id": series_id,
                        "series_title": smeta.get("title") if smeta else None,
                        "episode_number": ep_num,
                        "episode_id": episode_id, "episode": data,
                        "source": res.source, "live": True,
                        # None on cache hits => no RapidAPI connection/quota is attributed.
                        "connection_id": res.connection_id,
                        "upstream_verified": True, "cache_hit": res.hit}
            except UpstreamError as exc:
                if exc.code in ("UPSTREAM_NOT_VERIFIED", "CACHE_UNAVAILABLE"):
                    raise
                logger.warning("upstream episode failed (%s); falling back to local stub", exc.code)
        elif settings.upstream_episodes_path:
            try:
                template = (settings.upstream_episodes_path.replace("{id}", series_id)
                            .replace("{episode_id}", episode_id))
                body, conn_id, via = await _pooled_get(template, None)
                data = body.get("data", body) if isinstance(body, dict) else body
                data = self._apply_video_policy(data)
                ep_num = data.get("episode") if isinstance(data, dict) else None
                result = {"series_id": series_id,
                          "series_title": smeta.get("title") if smeta else None,
                          "episode_number": ep_num,
                          "episode_id": episode_id, "episode": data,
                          "source": f"upstream:{via}", "live": True, "connection_id": conn_id,
                          "upstream_verified": True, "cache_hit": False}
                _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                            settings.cache_ttl_episode_s)
                return result
            except UpstreamError as exc:
                if exc.code in ("UPSTREAM_NOT_VERIFIED",):
                    raise
                logger.warning("upstream episode failed (%s); falling back to local stub", exc.code)
        with _local_connect() as db:
            row = db.execute("SELECT * FROM series WHERE book_id=?", (series_id,)).fetchone()
            if row is None:
                return None
            item = _row_to_series(row)
        episode: dict[str, Any] = {
            "series_id": series_id, "episode_id": episode_id, "title": item.get("title"),
            "chapter_count": item.get("chapter_count"),
            "first_chapter_id": item.get("first_chapter_id"),
            "is_first_chapter": episode_id == str(item.get("first_chapter_id") or ""),
            "video_url": None, "video_redacted": True,
            "redaction_reason": ("Video URLs withheld by default. URLs are provided only when "
                                 "the upstream licence permits distribution (ALLOW_VIDEO_URLS=true).")}
        result = {"series_id": series_id, "episode_id": episode_id, "episode": episode,
                  "source": "local-cache", "live": False, "upstream_verified": False,
                  "note": "NOT live data: full episode list requires a verified upstream path; local metadata stub.",
                  "cache_hit": False}
        if not use_turso:  # never persist the local stub in front of / alongside Turso
            _cached_set(ckey, {k: v for k, v in result.items() if k != "cache_hit"},
                        settings.cache_ttl_episode_s)
        return result

    async def list_series_episodes(self, series_id: str, limit: int = 200,
                                     offset: int = 0) -> Optional[dict]:
        """Ordered episode roster from Turso (no video URLs, no upstream calls).

        Returns None when the series is unknown to the cache so the router can
        404 instead of guessing an unverified upstream list endpoint.
        """
        svc = episode_cache.get_service()
        if not svc.active:
            return None
        catalog = await svc.get_series_catalog(self.name, series_id)
        if catalog is None:
            return None
        smeta = _lookup_series_meta(series_id)
        roster = await svc.list_series_episodes(self.name, series_id,
                                                limit=limit, offset=offset)
        return {"provider": self.name, "series_id": series_id,
                "series_title": catalog.get("series_title") or (smeta.get("title") if smeta else None),
                "episode_count": (catalog.get("episode_count")
                                  if catalog.get("episode_count") is not None
                                  else (smeta.get("episode_count") if smeta else None)),
                "first_chapter_id": smeta.get("first_chapter_id") if smeta else None,
                "episodes_tracked": catalog.get("episodes_tracked", 0),
                "episodes_detailed": catalog.get("episodes_detailed", 0),
                "episodes_with_fresh_video": catalog.get("episodes_with_fresh_video", 0),
                "episodes": roster["items"], "limit": roster["limit"],
                "offset": roster["offset"], "has_more": roster["has_more"],
                "source": "cache:turso", "live": False, "cache_hit": True}

    @staticmethod
    def _apply_video_policy(data: Any) -> Any:
        if not settings.allow_video_urls:
            if isinstance(data, dict):
                redacted = dict(data)
                for field in ("video_url", "videoUrl", "play_url", "playUrl", "url", "source_url"):
                    if field in redacted:
                        redacted[field] = None
                redacted["video_redacted"] = True
                redacted["redaction_reason"] = ("Withheld: ALLOW_VIDEO_URLS=false. Enable only when the "
                                                "upstream licence permits use/distribution.")
                return _sanitize(redacted)
            return data
        if isinstance(data, dict):
            distributable = data.get("distributable") or data.get("allow_distribution") or data.get("licenced")
            explicitly_denied = any(
                data.get(k) is False for k in ("distributable", "allow_distribution", "licenced")
            )
            if explicitly_denied or (settings.require_distributable_flag and distributable is not True):
                redacted = dict(data)
                for field in ("video_url", "videoUrl", "play_url", "playUrl", "url", "source_url"):
                    if field in redacted:
                        redacted[field] = None
                redacted["video_redacted"] = True
                redacted["redaction_reason"] = "Withheld: upstream did not mark asset as distributable."
                return _sanitize(redacted)
        return _sanitize(data)


def get_reelshort_provider() -> RapidApiReelShortProvider:
    from app.providers.base import get_provider as _get
    existing = _get("reelshort")
    if isinstance(existing, RapidApiReelShortProvider):
        return existing
    return RapidApiReelShortProvider()


# -- episode cache hooks (provider-specific bits; the cache itself is provider-agnostic) ----

def _lookup_series_meta(series_id: str, payload: Any = None) -> Optional[dict]:
    """Look up series title, poster, chapter_count, and first_chapter_id from local SQLite
    (or upstream payload if present) so Turso series_cache stores the human-readable title."""
    out: dict[str, Any] = {}
    if isinstance(payload, dict):
        if isinstance(payload.get("series_title"), str) and payload["series_title"].strip():
            out["title"] = payload["series_title"].strip()
        if payload.get("chapter_count") is not None:
            out["episode_count"] = payload.get("chapter_count")
    try:
        with _local_connect() as db:
            row = db.execute(
                "SELECT title, poster, chapter_count, description, first_chapter_id "
                "FROM series WHERE book_id=?", (series_id,)
            ).fetchone()
            if row is not None:
                out.setdefault("title", row["title"])
                out.setdefault("pic", row["poster"])
                out.setdefault("episode_count", row["chapter_count"])
                out.setdefault("description", row["description"])
                out.setdefault("first_chapter_id", row["first_chapter_id"])
    except sqlite3.OperationalError:
        pass
    return out if out else None


async def _fetch_episode_upstream(series_id: str, episode_id: str) -> FetchResult:
    """The ONE place the cache reaches RapidAPI: the existing pooled call (round robin /
    fill first, quota accounting, cooldown, fail-fast 401/403 all live inside it)."""
    template = (settings.upstream_episodes_path.replace("{id}", series_id)
                .replace("{episode_id}", episode_id))
    meta: dict = {}
    body, conn_id, via = await _pooled_get(template, None, meta=meta)
    data = body.get("data", body) if isinstance(body, dict) else body
    return FetchResult(data=data, connection_id=conn_id, via=via,
                       cache_control=meta.get("cache_control"))


def _episode_wants_video(payload: Any) -> bool:
    """Mirror of _apply_video_policy: will the final response carry a playable URL?"""
    if not settings.allow_video_urls or not isinstance(payload, dict):
        return False
    if any(payload.get(k) is False for k in ("distributable", "allow_distribution", "licenced")):
        return False
    if not settings.require_distributable_flag:
        return True
    return (payload.get("distributable") or payload.get("allow_distribution")
            or payload.get("licenced")) is True


episode_cache.register_hooks("reelshort", ProviderHooks(
    fetch=_fetch_episode_upstream,
    wants_video=_episode_wants_video,
    store_video=lambda: settings.allow_video_urls,
    series_meta=_lookup_series_meta,
))
