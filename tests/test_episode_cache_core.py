"""Episode cache core behaviour (service + policy + schema) against a local libSQL test double."""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import libsql
import pytest

from app.episode_cache import policy, repo, schema
from app.episode_cache.client import CacheBackendError, scrub
from app.providers.base import UpstreamError
from tests.ec_helpers import (
    EP_PAYLOAD, TEST_TOKEN, Upstream, broken_service, make_cfg, make_service, register, sql,
    upstream_error,
)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "turso_double.db")


def run(coro):
    return asyncio.run(coro)


# --- 1/2: MISS -> upstream -> UPSERT, then HIT without upstream -----------------------

def test_miss_calls_upstream_and_upserts_then_hit_skips_upstream(db):
    up = Upstream()
    register(up)

    async def go():
        svc = make_service(db)
        r1 = await svc.get_episode("tp", "series1", "ep14")
        r2 = await svc.get_episode("tp", "series1", "ep14")
        return r1, r2

    r1, r2 = run(go())
    assert up.calls == 1
    assert r1.hit is False and r1.source == "upstream:rapidapi-1" and r1.connection_id == 7
    assert r2.hit is True and r2.source == "cache:turso" and r2.connection_id is None
    rows = sql(db, "SELECT provider, series_id, episode_id, episode_number, duration, "
                   "next_episode_id, next_episode_number FROM episode_cache")
    assert rows == [("tp", "series1", "ep14", 14, 103, "5h6g34m5y5", 15)]
    # same shape on HIT as on MISS (video URL excluded: not stored while ALLOW_VIDEO_URLS=false)
    assert {k: v for k, v in r1.payload.items() if k != "video_url"} == \
           {k: v for k, v in r2.payload.items() if k != "video_url"}


def test_upsert_never_duplicates_and_preserves_counters(db):
    up = Upstream()
    register(up)

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")
        await svc.get_episode("tp", "s", "ep14")
        await svc.flush()
        await svc.refresh("tp", "s", "ep14")
        await svc.refresh("tp", "s", "ep14")

    run(go())
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == 1
    assert sql(db, "SELECT hit_count FROM episode_cache")[0][0] == 1  # preserved across UPSERTs


def test_cache_key_isolation_between_provider_and_series(db):
    up = Upstream()
    register(up)
    register(up, provider="tp2")

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "sA", "ep14")
        await svc.get_episode("tp", "sB", "ep14")
        await svc.get_episode("tp2", "sA", "ep14")

    run(go())
    assert up.calls == 3  # different provider/series never share a row
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == 3


# --- 4: expired -> refresh ----------------------------------------------------------

def test_expired_metadata_triggers_refresh(db):
    up = Upstream()
    register(up)

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")
        sql(db, "UPDATE episode_cache SET metadata_expires_at=1")
        r = await svc.get_episode("tp", "s", "ep14")
        return r

    r = run(go())
    assert up.calls == 2 and r.hit is False
    assert sql(db, "SELECT metadata_expires_at > ? FROM episode_cache", (int(time.time()),))[0][0] == 1
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == 1


# --- 5: independent metadata / video URL TTL ------------------------------------------

DIST = dict(EP_PAYLOAD, distributable=True)


def test_video_expired_metadata_fresh_refreshes_only_when_url_would_be_served(db):
    up = Upstream(payload=DIST)
    register(up, allow_video=True)

    async def go():
        svc = make_service(db)
        r1 = await svc.get_episode("tp", "s", "ep14")
        assert r1.payload["video_url"]
        sql(db, "UPDATE episode_cache SET video_url_expires_at=1")  # URL expired, metadata fresh
        r2 = await svc.get_episode("tp", "s", "ep14")
        return r2

    r2 = run(go())
    assert up.calls == 2 and r2.hit is False and r2.payload["video_url"]


def test_expired_video_url_is_never_served_even_if_metadata_fresh(db):
    up = Upstream(payload=DIST)
    register(up, allow_video=False)  # URL would be redacted anyway -> no refetch needed

    async def go():
        svc = make_service(db, store_video_url="true")
        await svc.get_episode("tp", "s", "ep14")
        assert sql(db, "SELECT video_url IS NOT NULL FROM episode_cache")[0][0] == 1
        sql(db, "UPDATE episode_cache SET video_url_expires_at=1")
        return await svc.get_episode("tp", "s", "ep14")

    r = run(go())
    assert up.calls == 1 and r.hit is True
    assert r.payload["video_url"] is None  # stale URL stripped from the rebuilt payload


def test_metadata_expired_video_fresh_still_refreshes(db):
    up = Upstream(payload=DIST)
    register(up, allow_video=True)

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")
        sql(db, "UPDATE episode_cache SET metadata_expires_at=1")
        return await svc.get_episode("tp", "s", "ep14")

    r = run(go())
    assert up.calls == 2 and r.hit is False


def test_ttls_are_independent_columns(db):
    up = Upstream(payload=DIST)
    register(up, allow_video=True)

    async def go():
        svc = make_service(db, metadata_ttl_s=1000, video_url_ttl_s=100)
        await svc.get_episode("tp", "s", "ep14")

    run(go())
    meta, vid, fetched = sql(db, "SELECT metadata_expires_at, video_url_expires_at, fetched_at "
                                 "FROM episode_cache")[0]
    assert meta - fetched == 1000 and vid - fetched == 100


def test_video_not_persisted_when_disallowed_and_auto(db):
    up = Upstream()
    register(up, allow_video=False)
    run(make_service(db).get_episode("tp", "s", "ep14"))
    row = sql(db, "SELECT video_url, video_url_expires_at FROM episode_cache")[0]
    assert row == (None, None)


def test_policy_explicit_expiry_overrides_default_and_margin():
    cfg = make_cfg()
    now = int(time.time())
    url = f"https://cdn.example.com/a.m3u8?Expires={now + 600}&Signature=zzz"
    rec, why = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD, video_url=url), now,
                                store_video=True)
    assert why == "ok" and rec.video_url_expires_at == now + 600 - cfg.video_expiry_margin_s
    assert rec.metadata_expires_at == now + cfg.metadata_ttl_s
    # default (no explicit expiry) is the 24h TTL, never "forever"
    rec2, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD), now, store_video=True)
    assert rec2.video_url_expires_at == now + cfg.video_url_ttl_s
    # already-expired explicit URL is not stored for serving
    old = f"https://cdn.example.com/a.m3u8?Expires={now - 5}"
    rec3, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD, video_url=old), now,
                               store_video=True)
    assert rec3.video_url is None and rec3.video_url_expires_at <= now


def test_policy_payload_expiry_and_amz():
    cfg = make_cfg()
    now = int(time.time())
    rec, _ = policy.normalize(cfg, "tp", "s", "ep14",
                              dict(EP_PAYLOAD, expires_in=900), now, store_video=True)
    assert rec.video_url_expires_at == now + 900 - cfg.video_expiry_margin_s
    amz = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    u = f"https://cdn.example.com/a.m3u8?X-Amz-Date={amz}&X-Amz-Expires=3600"
    rec2, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD, video_url=u), now,
                               store_video=True)
    assert rec2.video_url_expires_at == now + 3600 - cfg.video_expiry_margin_s


def test_policy_cache_control_and_provider_ttl_override(monkeypatch):
    cfg = make_cfg()
    now = int(time.time())
    rec, why = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD), now, store_video=True,
                                cache_control="private, no-store")
    assert rec is None and why == "upstream_cache_control"
    rec, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD), now, store_video=True,
                              cache_control="public, max-age=120")
    assert rec.metadata_expires_at == now + 120 and rec.video_url_expires_at == now + 120
    rec, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD), now, store_video=True,
                              cache_control="no-store", respect_cache_control=False)
    assert rec is not None  # operator opted out of honouring it
    monkeypatch.setenv("EPISODE_VIDEO_URL_TTL_SECONDS_TP", "300")
    rec, _ = policy.normalize(cfg, "tp", "s", "ep14", dict(EP_PAYLOAD), now, store_video=True)
    assert rec.video_url_expires_at == now + 300  # per-provider override


@pytest.mark.parametrize("payload,reason", [
    (dict(EP_PAYLOAD, episode_id=None), "missing_episode_id"),
    ({k: v for k, v in EP_PAYLOAD.items() if k != "episode_id"}, "missing_episode_id"),
    (dict(EP_PAYLOAD, episode_id="OTHER"), "episode_id_mismatch"),
    ({k: v for k, v in EP_PAYLOAD.items() if k != "episode"}, "missing_episode_number"),
    (dict(EP_PAYLOAD, series_id="not-this-series"), "series_mismatch"),
    (["not", "an", "object"], "payload_not_object"),
])
def test_invalid_upstream_responses_are_not_cached(db, payload, reason):
    cfg = make_cfg()
    rec, why = policy.normalize(cfg, "tp", "s", "ep14", payload, int(time.time()), store_video=True)
    assert rec is None and why == reason
    up = Upstream(payload=payload)
    register(up)
    run(make_service(db).get_episode("tp", "s", "ep14"))
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == 0


# --- 6: upstream errors never cached and never destroy valid rows ---------------------

@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 502, 503])
def test_upstream_errors_do_not_cache_or_lose_data(db, status):
    good = Upstream()
    register(good)

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")  # valid row
        before = sql(db, "SELECT * FROM episode_cache")
        sql(db, "UPDATE episode_cache SET metadata_expires_at=1")
        before = sql(db, "SELECT * FROM episode_cache")
        bad = Upstream(error=upstream_error(status))
        register(bad)
        with pytest.raises(UpstreamError):
            await svc.get_episode("tp", "s", "ep14")        # expired row + upstream failure
        with pytest.raises(UpstreamError):
            await svc.get_episode("tp", "s", "brand-new")   # miss + upstream failure
        return before

    before = run(go())
    assert sql(db, "SELECT * FROM episode_cache") == before  # untouched, nothing new cached
    assert sql(db, "SELECT COUNT(*) FROM episode_cache")[0][0] == 1


# --- 7: Turso unavailable ------------------------------------------------------------

def test_turso_unavailable_falls_back_to_upstream_when_configured():
    up = Upstream()
    register(up)

    async def go():
        svc = broken_service(fallback_to_upstream=True)
        r = await svc.get_episode("tp", "s", "ep14")
        return svc, r

    svc, r = run(go())
    assert r.hit is False and r.persisted is False and up.calls == 1
    assert svc._counters["tp"]["backend_errors"] >= 1


def test_turso_unavailable_fails_closed_when_fallback_disabled():
    up = Upstream()
    register(up)

    async def go():
        svc = broken_service(fallback_to_upstream=False)
        with pytest.raises(UpstreamError) as ei:
            await svc.get_episode("tp", "s", "ep14")
        return ei.value

    err = run(go())
    assert err.code == "CACHE_UNAVAILABLE" and err.status == 503 and up.calls == 0


def test_circuit_breaker_opens_and_stops_hammering_backend():
    attempts = {"n": 0}
    svc = broken_service(breaker_threshold=3, max_retries=0)
    inner = svc.client._connect_fn

    def counting():
        attempts["n"] += 1
        return inner()
    svc.client._connect_fn = counting
    register(Upstream())

    async def go():
        for _ in range(10):
            await svc.get_episode("tp", "s", "ep14")

    run(go())
    assert svc.client.breaker_open
    assert attempts["n"] <= 6  # bounded: stops once the breaker opens (no unbounded retries)


def test_retry_is_bounded_with_backoff_then_succeeds(db):
    calls = {"n": 0}
    svc = make_service(db, max_retries=2)
    real = svc.client._connect_fn

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("Hrana: stream expired")  # transient
        return real()
    svc.client._connect_fn = flaky

    async def go():
        return await svc.client.run(lambda c: c.execute("SELECT 1").fetchone())

    assert run(go()) == (1,)
    assert calls["n"] == 3

    # permanent errors are not retried
    svc2 = make_service(db, max_retries=3)
    n = {"c": 0}

    def perm():
        n["c"] += 1
        raise ValueError("401 unauthorized")
    svc2.client._connect_fn = perm
    with pytest.raises(CacheBackendError):
        run(svc2.client.run(lambda c: c.execute("SELECT 1")))
    assert n["c"] == 1


def test_operation_timeout_is_enforced(db):
    svc = make_service(db, timeout_s=0.3)

    async def go():
        t0 = time.monotonic()
        with pytest.raises(CacheBackendError):
            await svc.client.run(lambda c: time.sleep(2))
        return time.monotonic() - t0

    assert run(go()) < 1.5


# --- 8: concurrent requests (singleflight, in-process) --------------------------------

def test_twenty_concurrent_requests_make_one_upstream_call(db):
    up = Upstream(delay=0.4)
    register(up)

    async def go():
        svc = make_service(db)
        res = await asyncio.gather(*[svc.get_episode("tp", "s", "ep14") for _ in range(20)])
        return svc, res

    svc, res = run(go())
    assert up.calls == 1
    assert sum(1 for r in res if not r.hit) == 1
    assert all(r.hit and r.source == "cache:turso" and r.connection_id is None
               for r in res if r.hit)
    assert svc._counters["tp"]["coalesced"] == 19 and svc._counters["tp"]["misses"] == 1


def test_leader_error_propagates_to_followers_without_extra_upstream_calls(db):
    up = Upstream(delay=0.2, error=upstream_error(502))
    register(up)

    async def go():
        svc = make_service(db)
        return await asyncio.gather(*[svc.get_episode("tp", "s", "ep14") for _ in range(8)],
                                    return_exceptions=True)

    res = run(go())
    assert up.calls == 1 and all(isinstance(r, UpstreamError) for r in res)


# --- 9: distributed lease: atomic, recoverable ----------------------------------------

def test_lock_acquire_is_atomic_across_threads(db):
    svc = make_service(db)
    run(svc.ensure_ready())
    wins: list[bool] = []
    barrier = threading.Barrier(8)

    def worker(i: int):
        conn = libsql.connect(db, autocommit=True, _check_same_thread=False, timeout=5)
        barrier.wait()
        wins.append(repo.try_acquire_lock(conn, "k", f"owner{i}", 30))
        conn.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_dead_worker_lease_expires_and_waiter_takes_over(db):
    up = Upstream()
    register(up)

    async def go():
        svc = make_service(db, lock_wait_s=10.0)
        await svc.ensure_ready()
        # a worker that died holding the lease (expires in ~2s, never released)
        sql(db, "INSERT INTO episode_cache_locks VALUES ('tp:s:ep14', 'dead-worker', "
                "CAST(strftime('%s','now') AS INTEGER) + 2)")
        t0 = time.monotonic()
        r = await svc.get_episode("tp", "s", "ep14")
        return r, time.monotonic() - t0

    r, elapsed = run(go())
    assert up.calls == 1 and r.hit is False
    assert 0.5 < elapsed < 8
    assert sql(db, "SELECT COUNT(*) FROM episode_cache_locks")[0][0] == 0  # released by new owner


def test_lock_released_after_upstream_failure(db):
    register(Upstream(error=upstream_error(500)))

    async def go():
        svc = make_service(db)
        with pytest.raises(UpstreamError):
            await svc.get_episode("tp", "s", "ep14")

    run(go())
    assert sql(db, "SELECT COUNT(*) FROM episode_cache_locks")[0][0] == 0


def test_live_lease_wait_budget_exhaustion_is_bounded_and_counted(db):
    up = Upstream()
    register(up)

    async def go():
        svc = make_service(db, lock_wait_s=1.0)
        await svc.ensure_ready()
        sql(db, "INSERT INTO episode_cache_locks VALUES ('tp:s:ep14', 'slow-holder', "
                "CAST(strftime('%s','now') AS INTEGER) + 60)")
        r = await svc.get_episode("tp", "s", "ep14")
        return svc, r

    svc, r = run(go())
    assert up.calls == 1 and svc._counters["tp"]["lock_timeouts"] == 1  # documented limit
    assert sql(db, "SELECT owner FROM episode_cache_locks")[0][0] == "slow-holder"  # untouched


# --- 12/13: process restart keeps data -------------------------------------------------

def test_data_survives_service_restart(db):
    up = Upstream()
    register(up)
    run(make_service(db).get_episode("tp", "s", "ep14"))
    up2 = Upstream()
    register(up2)
    r = run(make_service(db).get_episode("tp", "s", "ep14"))  # brand-new service + connections
    assert r.hit is True and up2.calls == 0


# --- batched metrics (no per-hit writes) -------------------------------------------------

def test_hits_are_batched_not_written_per_request(db):
    register(Upstream())

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")
        for _ in range(10):
            await svc.get_episode("tp", "s", "ep14")
        before = sql(db, "SELECT hit_count, last_accessed_at FROM episode_cache")[0]
        await svc.flush()
        after = sql(db, "SELECT hit_count, last_accessed_at FROM episode_cache")[0]
        stats = await svc.stats(force=True)
        return before, after, stats

    before, after, stats = run(go())
    assert before == (0, None)  # zero Turso writes on cache hits
    assert after[0] == 10 and after[1]
    assert stats["hits"] == 10 and stats["misses"] == 1
    assert stats["total_rows"] == 1 and stats["hit_rate_pct"] == round(100 * 10 / 11, 1)
    assert stats["estimated_upstream_saved"] == 10 and "Estimate" in stats["estimated_note"]


def test_flush_failure_requeues_counters(db):
    register(Upstream())

    async def go():
        svc = make_service(db)
        await svc.get_episode("tp", "s", "ep14")
        await svc.get_episode("tp", "s", "ep14")
        real = svc.client._connect_fn
        svc.client._connect_fn = lambda: (_ for _ in ()).throw(ValueError("down"))
        svc.client._pool = type(svc.client._pool)()  # drop pooled conns
        svc.client._created = 0
        await svc.flush()
        pending = dict(svc._pending_hits)
        svc.client._connect_fn = real
        svc.client._open_until = 0
        svc.client._fail_streak = 0
        await svc.flush()
        return pending

    pending = run(go())
    assert pending  # re-queued, not lost
    assert sql(db, "SELECT hit_count FROM episode_cache")[0][0] == 1


# --- 16: migration idempotency / schema ---------------------------------------------------

def test_migration_is_idempotent_and_keeps_data(db):
    conn = libsql.connect(db, autocommit=True)
    all_versions = [v for v, _, _ in schema.MIGRATIONS]
    assert schema.migrate(conn) == all_versions
    conn.execute("INSERT INTO episode_cache (provider, series_id, episode_id, fetched_at, "
                 "metadata_expires_at, created_at, updated_at) VALUES ('p','s','e',1,2,1,1)")
    assert schema.migrate(conn) == []                  # re-run: nothing to do
    # crash-between-DDL-and-version-row simulation: rerun applies safely without data loss
    conn.execute("DELETE FROM episode_cache_schema")
    assert schema.migrate(conn) == all_versions
    assert conn.execute("SELECT COUNT(*) FROM episode_cache").fetchone()[0] == 1
    assert [r[0] for r in conn.execute("SELECT version FROM episode_cache_schema ORDER BY version").fetchall()] == all_versions
    assert repo.schema_version(conn) == schema.LATEST


def test_unique_constraint_and_indexes_and_counter_triggers(db):
    conn = libsql.connect(db, autocommit=True)
    schema.migrate(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(episode_cache)").fetchall()]
    for c in ("id", "provider", "series_id", "episode_id", "episode_number", "video_url", "duration",
              "next_episode_id", "next_episode_number", "fetched_at", "metadata_expires_at",
              "video_url_expires_at", "last_accessed_at", "hit_count", "created_at", "updated_at"):
        assert c in cols
    idx = {r[1] for r in conn.execute("PRAGMA index_list(episode_cache)").fetchall()}
    assert {"idx_ec_meta_exp", "idx_ec_video_exp", "idx_ec_fetched"} <= idx
    ins = ("INSERT INTO episode_cache (provider, series_id, episode_id, fetched_at, "
           "metadata_expires_at, created_at, updated_at) VALUES ('p','s',?,1,2,1,1)")
    conn.execute(ins, ("e1",))
    with pytest.raises(Exception):
        conn.execute(ins, ("e1",))  # UNIQUE(provider, series_id, episode_id)
    conn.execute(ins, ("e2",))
    assert conn.execute("SELECT value FROM episode_cache_counters").fetchone()[0] == 2
    conn.execute("DELETE FROM episode_cache WHERE episode_id='e1'")
    assert conn.execute("SELECT value FROM episode_cache_counters").fetchone()[0] == 1
    # hot lookup uses an index, not a scan
    plan = " ".join(str(r) for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM episode_cache WHERE provider='p' AND series_id='s' "
        "AND episode_id='e2'").fetchall())
    assert "USING INDEX" in plan.upper() or "USING COVERING INDEX" in plan.upper()
    assert "SCAN" not in plan.replace("SEARCH", "")


def test_admin_ops_repo_invalidate_delete_list(db):
    register(Upstream())

    async def go():
        svc = make_service(db)
        for i in range(30):
            await svc.get_episode("tp", "sA" if i < 20 else "sB", f"e{i}")
        p1 = await svc.list_rows(limit=10, offset=0)
        p3 = await svc.list_rows(limit=10, offset=20)
        found = await svc.list_rows(q="sB", limit=100)
        n = await svc.invalidate_series("tp", "sA")
        exp = await svc.list_rows(state="expired", limit=100)
        d = await svc.delete(p1["items"][0]["id"])
        return p1, p3, found, n, exp, d

    p1, p3, found, n, exp, d = run(go())
    assert len(p1["items"]) == 10 and p1["has_more"] is True
    assert len(p3["items"]) == 10 and p3["has_more"] is False
    assert len(found["items"]) == 10 and all(i["series_id"] == "sB" for i in found["items"])
    assert n == 20 and len(exp["items"]) == 20 and d == 1
    assert sql(db, "SELECT value FROM episode_cache_counters")[0][0] == 29


# --- 14: secrets never in logs / health ----------------------------------------------------

def test_no_secrets_in_logs_health_or_repr(db, caplog):
    caplog.set_level(logging.DEBUG)
    register(Upstream())

    async def go():
        svc = broken_service()
        await svc.get_episode("tp", "s", "ep14")
        return await svc.health(), svc

    health, svc = run(go())
    assert TEST_TOKEN not in caplog.text
    assert TEST_TOKEN not in str(health) and TEST_TOKEN not in repr(svc.cfg)
    assert TEST_TOKEN not in str(svc.client.status())
    assert scrub(f"x {TEST_TOKEN} y", (TEST_TOKEN,)) == "x *** y"
    jwt = "eyJhbGciOiJFZERTQSJ9.eyJpYXQiOjE3MDAwMDAwMDB9.c2lnbmF0dXJlc2lnbmF0dXJl"
    assert jwt not in scrub(f"auth {jwt}")


def test_app_log_filter_redacts_jwt_shaped_tokens():
    import app.main as m
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1,
                            "fail token eyJhbGciOiJFZERTQSJ9.eyJpYXQiOjE3MDAwMDAwMDB9.c2ln1234", (), None)
    m._RedactingFilter().filter(rec)
    assert "eyJ" not in rec.msg and "***" in rec.msg


def test_config_defaults_and_missing_vars(monkeypatch):
    from app.episode_cache.config import load_cache_config
    for k in ("EPISODE_CACHE_ENABLED", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_cache_config()
    assert cfg.enabled is False and cfg.active is False
    assert cfg.metadata_ttl_s == 2592000 and cfg.video_url_ttl_s == 86400 and cfg.timeout_s == 5.0
    monkeypatch.setenv("EPISODE_CACHE_ENABLED", "true")
    cfg = load_cache_config()
    assert cfg.active is False and cfg.missing() == ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"]
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://x.aws-ap-southeast-2.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "t")
    assert load_cache_config().active is True
