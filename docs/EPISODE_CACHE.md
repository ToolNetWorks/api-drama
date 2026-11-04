# Persistent Episode Cache (Turso / libSQL)

Shared, persistent cache for `GET /v1/reelshort/series/{series_id}/episodes/{episode_id}`.
Provider-agnostic: a provider only registers `ProviderHooks` (see the bottom of
`app/providers/rapidapi_reelshort.py`); DramaBox / NetShort / ShortMax can reuse it unchanged.

## What lives where

| Store | Holds |
|---|---|
| SQLite on the VPS (`data/reelshort.sqlite3`) — **untouched** | customer API keys, admin accounts/sessions, provider connections + RapidAPI keys (encrypted), quotas, usage logs, config |
| Turso `drama-cache` (AWS ap-southeast-2) | `episode_cache` rows, hit/miss aggregates, singleflight leases, schema version |

The cache code never opens the SQLite DB. Auth, provider rights, rate limit and customer
quota all run in the router dependency **before** the cache is consulted; every request
(hit or miss) is still logged and counted against the customer.

## Setup (operator)

1. `turso db create drama-cache --location syd` (AWS ap-southeast-2) – or reuse the existing one.
2. `turso db show drama-cache --url` → `TURSO_DATABASE_URL` (keep the `libsql://` scheme; it is passed to the SDK as is).
3. `turso db tokens create drama-cache` (prefer an expiring/scoped token) → `TURSO_AUTH_TOKEN`.
4. Put both in `/home/ubuntu/api-drama/.env` (never commit; `.env` is git/docker-ignored), plus `EPISODE_CACHE_ENABLED=true`.
5. `python tools/turso_migrate.py --check` (no writes) then `--migrate`. Migrations also run lazily at app start.
6. `sudo docker compose up -d` and open **Admin → Episode Cache**.

The cache stays **off** (legacy in-memory/SQLite 10-minute cache path) unless
`EPISODE_CACHE_ENABLED=true` and both Turso values are present. `/v1/health` and the admin page say exactly which are missing.

## SDK choice

Official `libsql` (PyPI, 0.1.11). Verified: speaks `libsql://` natively (Hrana), Python 3.12 manylinux wheel,
**synchronous only, no async API, no pool**. Therefore `app/episode_cache/client.py` provides a bounded pool of
reused connections executed on a dedicated thread pool, awaited with a hard timeout, so the event loop never blocks.
The URL is never rewritten to `http(s)://`.
The wheel is published for x86_64 (the VPS); an ARM host would need to verify wheel availability first.

## Schema (migration `episode_cache_v1`, idempotent, version in `episode_cache_schema`)

`episode_cache`: `id` PK, `provider`, `series_id`, `episode_id` (NOT NULL), `episode_number`, `video_url`, `duration`,
`next_episode_id`, `next_episode_number`, `extra_json` (sanitised remaining upstream fields, ≤8 KB, **never** video URLs),
`fetched_at`, `metadata_expires_at`, `video_url_expires_at`, `last_accessed_at`, `hit_count` (default 0), `created_at`,
`updated_at`; `UNIQUE(provider, series_id, episode_id)`. Timestamps are INTEGER unix seconds (UTC).
Indexes: the UNIQUE index (exact lookup and `(provider, series_id)` prefix filter), `idx_ec_meta_exp`, `idx_ec_video_exp`, `idx_ec_fetched`.
Helper tables: `episode_cache_counters` (exact row count kept by triggers → no `COUNT(*)` scans), `episode_cache_stats`
(per day/provider aggregates), `episode_cache_locks` (leases). Writes are `INSERT … ON CONFLICT DO UPDATE` (never duplicates; an older fetch cannot overwrite a newer one).
Only URLs/metadata are stored — never MP4/M3U8 content.

## Request flow

auth → provider rights → rate limit/quota → read Turso by `(provider, series_id, episode_id)` → fresh? serve : upstream via the **existing**
pooled RapidAPI call → validate → UPSERT → respond. Response contract unchanged; additive `cache_hit` / `source`
(`cache:turso`, `upstream:<connection>`; `cache:inflight` = shared with a concurrent request while Turso was unreachable).
`connection_id` is null on cache hits, so no RapidAPI connection/quota is attributed and the dashboard counts it as cache-served.

Not cached: any upstream error (401/403/404/429/5xx, timeouts), non-JSON, non-object payloads, missing `episode_id`/`episode`,
`episode_id` ≠ requested, `series_id`/`book_id` ≠ requested, upstream `Cache-Control: no-store|no-cache|private`
(switch `EPISODE_CACHE_RESPECT_CACHE_CONTROL`), local-stub fallbacks. A failed refresh never deletes or overwrites a valid row.

## TTLs

* Metadata: `EPISODE_METADATA_TTL_SECONDS` (30 d). Video URL: `EPISODE_VIDEO_URL_TTL_SECONDS` (24 h). Per provider:
  `EPISODE_METADATA_TTL_SECONDS_<PROVIDER>` / `EPISODE_VIDEO_URL_TTL_SECONDS_<PROVIDER>`. Upstream `max-age` caps both.
* A real expiry advertised by upstream (URL `Expires=`/`exp=`/`X-Amz-Expires`, payload `expires_at`/`expires_in`) wins over the default (minus a 60 s margin). No URL is assumed permanent.
* A URL is only *needed* when the final response would contain one (`ALLOW_VIDEO_URLS=true` **and** upstream marked distributable). Then an expired URL forces a refresh even if metadata is fresh (upstream has one endpoint, so the whole episode is re-fetched). Otherwise metadata alone is served and the stale URL is stripped. No `HEAD` probes.
* `EPISODE_CACHE_STORE_VIDEO_URL=auto` persists URLs only while `ALLOW_VIDEO_URLS=true`; `true`/`false` force it. Storing a URL never grants permission to return it: redaction (`_apply_video_policy`) always runs on the final payload, whatever the source.

## Singleflight and its limits

Layer 1: one asyncio Task per key per process. Layer 2: lease row in Turso acquired by an atomic conditional UPSERT
(`ON CONFLICT … DO UPDATE … WHERE lease_expires_at <= now RETURNING owner`, Turso server clock). The holder re-checks the cache
after winning, fetches, UPSERTs, then releases. A dead holder's lease just expires (`EPISODE_CACHE_LOCK_LEASE_SECONDS`, 60 s); waiters poll with
backoff for up to `EPISODE_CACHE_LOCK_WAIT_SECONDS` (20 s). (Postgres advisory locks are not used.)

This is **best-effort deduplication, not an exactly-once guarantee**. Known cases that can produce a bounded duplicate upstream call:
Turso unreachable (in-process dedup only; `EPISODE_CACHE_FALLBACK_TO_UPSTREAM=false` fails closed instead), an upstream call longer than the lease/wait budget (counted as
`lock_timeouts`), a lease expiring while a slow holder is still running (no heartbeat). If strict cross-container guarantees are ever needed, use an independent coordinator (e.g. a Redis `SET NX PX` lock in compose).
Evidence: `tests/test_episode_cache_multiworker.py` (4 OS processes × 5 requests per key → 1 upstream call per key; removing the lease makes it 4).

## Reliability and quota

Timeouts (`EPISODE_CACHE_TIMEOUT_SECONDS`), ≤`EPISODE_CACHE_MAX_RETRIES` retries with exponential backoff + full jitter for transient errors only, circuit breaker
(opens after 3 consecutive failures for 30 s) so a dead Turso adds no latency. Turso failure → upstream fallback (configurable); auth/rate limit/quota are never bypassed.
The Docker `HEALTHCHECK` stays on `/health`, which does not depend on Turso. RapidAPI usage changes only inside the existing `_pooled_get` (real calls); Round Robin / Fill First, cooldowns,
monthly reset and fallback are untouched.

## Free-tier friendliness

Hot path = 1 indexed read on a hit. `hit_count`/`last_accessed_at` and stats are batched in memory and flushed every 30 s (≤200 keys per flush, re-queued on failure,
flushed on shutdown) — **zero Turso writes per cache hit**. Row count comes from a trigger-maintained counter; expiry counts are index-range counts capped at 10 000; admin lists are paginated (≤100) with no totals.
Dashboard counters can lag ≤ flush interval and only include events flushed by *all* workers.

"Estimated Upstream Requests Saved" = Turso-served hits (plus coalesced requests). It is an **estimate/upper bound**: the legacy short-lived cache would have absorbed some, and the
real RapidAPI cost per request is unknown.

## Admin

`/admin/cache` (session required; static shell so a Turso outage cannot lock you out). API under `/admin/api/cache/*`: `status`, `series` (catalog with per-series counters), `series/{provider}/{series_id}` (series + ordered roster, no video URLs), `entries` (search/filter/pagination), `entries/{id}`,
`POST entries/{id}/refresh` (**consumes one RapidAPI request**), `DELETE entries/{id}`, `POST series/invalidate` (soft: expires both TTLs). Mutations need session + CSRF.
Video URLs are shown masked (query string stripped); no tokens/keys are ever returned.
The dashboard uses the shared admin design system — see `docs/ADMIN_UI.md`.

## Series catalog (migration `series_cache_v1`)

`series_cache`: one row per `(provider, series_id)` — `series_title`, `series_pic`, `episode_count`, `description`, `language` (all nullable), `fetched_at`, `updated_at`, `metadata_expires_at`. `episode_cache` rows link via the same `(provider, series_id)` key (no repeated titles; one index `idx_ec_series_epnum` serves ordered rosters).

Populating: episode fetches only create the minimal link row (never invented metadata). Full metadata + episode skeletons (number + id, `NULL` details) come from `sync_series_list()`, wired for future GetSeries integration once `RAPIDAPI_REELSHORT_DETAIL_PATH` is verified — search/detail/list paths are still unconfigured (see `docs/RAPIDAPI_ENDPOINTS.md`) and are never guessed. Skeletons are `INSERT OR IGNORE` (never overwrite a fetched row), episode 0 (trailer) is skipped, and a failed sync never deletes old data.

Public roster: `GET /v1/reelshort/series/{series_id}/episodes` (auth/quota/logged like other `/v1` routes) serves the ordered roster from Turso with per-episode cache state; `404` when unknown. It never fans out to upstream.

## Why `video_url` is EMPTY on the fixture (audit 2026-10-13)

Upstream **did** return a video field for episode 11 (the cached `extra_json` keeps the `__video_field: video_url` marker), but the column is `NULL` by storage policy, not by a redaction wipe: `EPISODE_CACHE_STORE_VIDEO_URL=auto` stores a URL only while `ALLOW_VIDEO_URLS=true`, and production keeps `ALLOW_VIDEO_URLS=false`. `normalize()` runs on the raw upstream payload (pre-redaction); `_apply_video_policy()` redacts only the response. So no URL ever entered Turso, and none leaks. To retain URLs while still redacting responses (requires a licence permitting storage), set `EPISODE_CACHE_STORE_VIDEO_URL=true` — responses stay redacted until `ALLOW_VIDEO_URLS=true` **and** upstream marks the asset distributable. No full URLs or credentials are ever logged.

## Rollback

1. Instant, no deploy: set `EPISODE_CACHE_ENABLED=false` in `.env` → `sudo docker compose up -d`. The gateway returns to the legacy cache path; Turso data is left intact.
2. Previous image: `sudo docker tag api-drama:pre-episode-cache api-drama:latest && sudo docker compose up -d --no-build`.
3. Data: SQLite was not modified by this feature; a verified backup is in `backups/<timestamp>/` (restore with the container stopped).
4. Turso: `DROP TABLE episode_cache, episode_cache_counters, episode_cache_stats, episode_cache_locks, series_cache, episode_cache_schema;` only if you want it gone (nothing else depends on them).

## Tests

`.venv/bin/python -m pytest -q`. Turso is replaced by a **local libSQL file opened through the same SDK and client code**; RapidAPI by a counting fake.
Tests never use `.env` credentials (`conftest.py` blanks the Turso variables). Real-Turso verification must be run by the operator with real credentials
(`tools/turso_migrate.py --check`, then the cache HIT/MISS check in the deployment notes).
