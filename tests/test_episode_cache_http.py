"""HTTP-level episode cache: customer endpoint (auth/quota/redaction/shared cache) + admin API.

RapidAPI is never contacted: httpx.AsyncClient is replaced by a counting fake, while the
real pool/quota accounting (SQLite) runs. Turso is a local libSQL file test double.
"""
from __future__ import annotations

import dataclasses
import json
import os

import pytest

import app.providers.rapidapi_reelshort as rs
from app.core.crypto import encrypt_key
from app.core.security import generate_raw_key, hash_key, key_prefix
from app.episode_cache import service as ec_service
from app.store import connect, utcnow
from tests.conftest import ADMIN_PASS, ADMIN_USER
from tests.ec_helpers import (
    EP_PAYLOAD, TEST_TOKEN, broken_service, make_cfg, make_service, sql,
)

MASTER = os.getenv("TOOLNET_MASTER_KEY", "")
EP_PATH = "/reelshort/series/{id}/episodes/{episode_id}"
SERIES = "book1"  # seeded in conftest so legacy stub fallback exists too
SIGNED = "https://cdn.example.com/v/ep.m3u8?Expires={exp}&Signature=SECRETSIG"


def _fake_http(status=200, payload=None, headers=None):
    class FakeResp:
        status_code = status
        text = ""

        def __init__(self):
            self.headers = headers or {}

        def json(self):
            return payload if payload is not None else {}

    class FakeClient:
        calls = 0

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, *a, **k):
            FakeClient.calls += 1
            return FakeResp()
    return FakeClient


def _make_customer(name, allowed=None, daily=None):
    raw = generate_raw_key()
    with connect() as db:
        db.execute(
            """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked, rpm,
               daily_quota, monthly_quota, created_at, last_used_at, expires_at, allowed_providers)
               VALUES (?, ?, ?, 1, 0, 600, ?, NULL, ?, NULL, NULL, ?)""",
            (name, key_prefix(raw), hash_key(raw), daily, utcnow(),
             json.dumps(allowed) if allowed is not None else None))
        kid = db.execute("SELECT id FROM api_keys WHERE key_hash=?", (hash_key(raw),)).fetchone()[0]
    return raw, kid


def _ensure_connection():
    with connect() as db:
        n = db.execute("SELECT COUNT(*) FROM provider_connections WHERE provider='reelshort' "
                       "AND enabled=1").fetchone()[0]
        if n == 0:
            db.execute(
                """INSERT INTO provider_connections
                   (provider, name, key_encrypted, key_hint, enabled, status, priority, quota_limit,
                    quota_window, requests_used, quota_reset_at, consecutive_failures, created_at)
                   VALUES ('reelshort','ec-conn',?, '****x', 1,'active',50,NULL,'monthly',0,NULL,0,?)""",
                (encrypt_key("dummy-key", MASTER), utcnow()))


def _upstream_used():
    with connect() as db:
        return db.execute("SELECT COALESCE(SUM(requests_used),0) FROM provider_connections "
                          "WHERE provider='reelshort'").fetchone()[0]


def _log_rows(key_id):
    with connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT status, connection_id, provider FROM request_logs WHERE key_id=? ORDER BY id",
            (key_id,)).fetchall()]


def _url(eid):
    return f"/v1/reelshort/series/{SERIES}/episodes/{eid}"


@pytest.fixture
def env(client, tmp_path, monkeypatch):
    """Cache ON (local libSQL double), upstream path verified, ALLOW_VIDEO_URLS=false.

    Upstream failure tests legitimately mutate provider_connections (cooldown/auth_error);
    the guard restores them so other tests in the shared session DB are unaffected."""
    _ensure_connection()
    with connect() as db:
        saved = [dict(r) for r in db.execute(
            "SELECT * FROM provider_connections WHERE provider='reelshort'").fetchall()]
    path = str(tmp_path / "http_cache.db")
    svc = make_service(path)
    ec_service._service = svc
    monkeypatch.setattr(rs, "settings", dataclasses.replace(
        rs.settings, upstream_episodes_path=EP_PATH, allow_video_urls=False))

    class E:  # noqa: N801
        pass
    e = E()
    e.svc, e.path, e.mp = svc, path, monkeypatch

    def fake(status=200, payload=None, headers=None):
        cls = _fake_http(status, payload, headers)
        monkeypatch.setattr("httpx.AsyncClient", cls)
        return cls
    e.fake = fake
    yield e
    svc.client.close()
    ec_service._service = None
    with connect() as db:
        for row in saved:
            cols = [c for c in row if c != "id"]
            db.execute("UPDATE provider_connections SET " + ", ".join(f"{c}=?" for c in cols)
                       + " WHERE id=?", (*[row[c] for c in cols], row["id"]))


def _payload(eid, **kw):
    return dict(EP_PAYLOAD, episode_id=eid, **kw)


def _get(client, key, eid):
    return client.get(_url(eid), headers={"X-API-Key": key})


# ---- 3/10/11: shared cache, auth, quotas ----------------------------------------------

def test_cache_shared_across_customers_hit_skips_rapidapi_and_quota(client, env):
    fake = env.fake(200, _payload("share1"))
    a, a_id = _make_customer("cust-a")
    b, b_id = _make_customer("cust-b")
    used0 = _upstream_used()
    r1 = _get(client, a, "share1")
    used1 = _upstream_used()
    r2 = _get(client, b, "share1")
    r3 = _get(client, a, "share1")
    used2 = _upstream_used()
    assert r1.status_code == r2.status_code == r3.status_code == 200
    d1, d2 = r1.json()["data"], r2.json()["data"]
    assert d1["cache_hit"] is False and d1["source"].startswith("upstream:")
    assert d2["cache_hit"] is True and d2["source"] == "cache:turso"
    assert fake.calls == 1                       # one RapidAPI call for 3 requests / 2 customers
    assert used1 == used0 + 1 and used2 == used1  # RapidAPI quota only moved on the real call
    assert d1["episode"]["episode_id"] == d2["episode"]["episode_id"] == "share1"
    assert d1["episode"]["duration"] == d2["episode"]["duration"] == 103
    assert d2["episode"]["next_episode"] == {"episode": 15, "episode_id": "5h6g34m5y5"}
    # each customer's own usage is recorded, hits included; hit rows carry no connection_id
    la, lb = _log_rows(a_id), _log_rows(b_id)
    assert [x["status"] for x in la] == [200, 200] and len(lb) == 1
    assert lb[0]["connection_id"] is None and la[0]["connection_id"] is not None
    assert la[1]["connection_id"] is None


def test_authentication_still_required_on_cache_hit(client, env):
    env.fake(200, _payload("auth1"))
    a, _ = _make_customer("cust-auth")
    assert _get(client, a, "auth1").status_code == 200
    assert client.get(_url("auth1")).status_code == 401
    assert client.get(_url("auth1"), headers={"X-API-Key": "tn_live_wrong"}).status_code == 401
    forbidden, _ = _make_customer("cust-noprov", allowed=["dramabox"])
    r = _get(client, forbidden, "auth1")
    assert r.status_code == 403 and r.json()["error"]["code"] == "PROVIDER_FORBIDDEN"


def test_customer_daily_quota_counts_cache_hits(client, env):
    fake = env.fake(200, _payload("quota1"))
    k, kid = _make_customer("cust-quota", daily=2)
    assert _get(client, k, "quota1").status_code == 200   # MISS
    assert _get(client, k, "quota1").status_code == 200   # HIT still consumes customer quota
    r = _get(client, k, "quota1")
    assert r.status_code == 429 and r.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert fake.calls == 1


# ---- 12: video redaction on every source -----------------------------------------------

def test_video_url_redacted_for_upstream_and_turso_when_not_allowed(client, env):
    exp = 4102444800 - 1  # far future, valid
    env.fake(200, _payload("red1", video_url=SIGNED.format(exp=exp), distributable=True))
    env.svc.cfg = dataclasses.replace(env.svc.cfg, store_video_url="true")  # force store
    k, _ = _make_customer("cust-red")
    miss = _get(client, k, "red1")
    hit = _get(client, k, "red1")
    for r in (miss, hit):
        assert r.status_code == 200
        ep = r.json()["data"]["episode"]
        assert ep["video_url"] is None and ep["video_redacted"] is True
        assert "cdn.example.com" not in r.text and "SECRETSIG" not in r.text
    assert hit.json()["data"]["cache_hit"] is True
    # cached in Turso, but cache presence did not open distribution rights
    assert sql(env.path, "SELECT video_url IS NOT NULL FROM episode_cache WHERE episode_id='red1'")[0][0] == 1


def test_default_auto_mode_does_not_persist_urls_while_disallowed(client, env):
    env.fake(200, _payload("red2", video_url=SIGNED.format(exp=4102444000)))
    k, _ = _make_customer("cust-red2")
    assert _get(client, k, "red2").status_code == 200
    assert sql(env.path, "SELECT video_url FROM episode_cache WHERE episode_id='red2'")[0][0] is None


def test_url_served_only_when_allowed_and_distributable_and_fresh(client, env):
    env.mp.setattr(rs, "settings", dataclasses.replace(rs.settings, allow_video_urls=True))
    fake = env.fake(200, _payload("vid1", video_url=SIGNED.format(exp=4102444000),
                                  distributable=True))
    k, _ = _make_customer("cust-vid")
    r1, r2 = _get(client, k, "vid1"), _get(client, k, "vid1")
    assert r1.json()["data"]["episode"]["video_url"].startswith("https://cdn.example.com")
    assert r2.json()["data"]["cache_hit"] is True
    assert r2.json()["data"]["episode"]["video_url"].startswith("https://cdn.example.com")
    assert fake.calls == 1
    sql(env.path, "UPDATE episode_cache SET video_url_expires_at=1 WHERE episode_id='vid1'")
    r3 = _get(client, k, "vid1")  # URL expired -> refreshed upstream, never served stale
    assert fake.calls == 2 and r3.json()["data"]["cache_hit"] is False
    # not marked distributable upstream -> redacted even though allowed + cached
    fake2 = env.fake(200, _payload("vid2", video_url=SIGNED.format(exp=4102444000)))
    r4 = _get(client, k, "vid2")
    assert r4.json()["data"]["episode"]["video_url"] is None and fake2.calls == 1


# ---- 6: upstream failures --------------------------------------------------------------

@pytest.mark.parametrize("status", [401, 403, 429, 500, 502])
def test_upstream_failures_never_cached_and_keep_valid_rows(client, env, status):
    k, _ = _make_customer(f"cust-fail{status}")
    env.fake(200, _payload(f"keep{status}"))
    assert _get(client, k, f"keep{status}").status_code == 200
    sql(env.path, "UPDATE episode_cache SET metadata_expires_at=1")
    before = sql(env.path, "SELECT * FROM episode_cache")
    env.fake(status, {"message": "nope"})
    r = _get(client, k, f"keep{status}")                 # existing semantics: stub fallback
    r2 = _get(client, k, f"new{status}")
    assert r.status_code == 200 and r.json()["data"]["live"] is False
    assert r2.json()["data"]["source"] == "local-cache"
    assert sql(env.path, "SELECT * FROM episode_cache") == before  # nothing cached / lost


def test_upstream_no_store_cache_control_is_honoured(client, env):
    fake = env.fake(200, _payload("nostore1"), headers={"cache-control": "no-store"})
    k, _ = _make_customer("cust-nostore")
    assert _get(client, k, "nostore1").status_code == 200
    assert _get(client, k, "nostore1").status_code == 200
    assert fake.calls == 2
    assert sql(env.path, "SELECT COUNT(*) FROM episode_cache WHERE episode_id='nostore1'")[0][0] == 0


# ---- 7: Turso unavailable ---------------------------------------------------------------

def test_turso_down_falls_back_to_upstream_but_auth_and_quota_still_apply(client, env):
    env.svc.client.close()
    env.svc.client = broken_service().client
    fake = env.fake(200, _payload("down1"))
    k, kid = _make_customer("cust-down", daily=2)
    r = _get(client, k, "down1")
    assert r.status_code == 200 and r.json()["data"]["cache_hit"] is False
    assert fake.calls == 1
    assert client.get(_url("down1")).status_code == 401           # auth not bypassed
    assert _get(client, k, "down1").status_code == 200
    assert _get(client, k, "down1").status_code == 429            # quota not bypassed
    assert TEST_TOKEN not in r.text


def test_turso_down_fail_closed_returns_503_envelope(client, env):
    env.svc.client.close()
    env.svc.cfg = dataclasses.replace(env.svc.cfg, fallback_to_upstream=False)
    env.svc.client = broken_service().client
    fake = env.fake(200, _payload("down2"))
    k, _ = _make_customer("cust-down2")
    r = _get(client, k, "down2")
    assert r.status_code == 503 and r.json()["error"]["code"] == "CACHE_UNAVAILABLE"
    assert fake.calls == 0


def test_legacy_behaviour_unchanged_when_cache_inactive(client, env):
    ec_service.configure(make_cfg(enabled=False), None)  # inactive singleton
    fake = env.fake(200, _payload("legacy1"))
    k, _ = _make_customer("cust-legacy")
    r1, r2 = _get(client, k, "legacy1"), _get(client, k, "legacy1")
    assert r1.json()["data"]["cache_hit"] is False and r2.json()["data"]["cache_hit"] is True
    assert r2.json()["data"]["source"].startswith("upstream:")  # legacy L1/L2 semantics
    assert fake.calls == 1


# ---- health ---------------------------------------------------------------------------------

def test_public_health_reports_cache_status_without_secrets(client, env):
    j = client.get("/v1/health").json()
    assert j["ok"] is True and j["episode_cache"]["status"] == "enabled"
    assert "host" not in j["episode_cache"] and TEST_TOKEN not in json.dumps(j)
    assert client.get("/health").json()["ok"] is True


# ---- 15: admin cache management ----------------------------------------------------------------

def _login(client):
    client.cookies.clear()
    r = client.post("/admin/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    return {"X-CSRF-Token": r.json()["csrf_token"]}


def _seed(client, env, n, series=SERIES, prefix="adm"):
    k, _ = _make_customer("cust-seed")
    env.fake(200, _payload("x"))
    for i in range(n):
        env.fake(200, _payload(f"{prefix}{i}", video_url=SIGNED.format(exp=4102444000)))
        assert _get(client, k, f"{prefix}{i}").status_code == 200
    return k


def test_admin_requires_session_and_csrf(client, env):
    client.cookies.clear()
    assert client.get("/admin/api/cache/status").status_code == 401
    assert client.get("/admin/api/cache/entries").status_code == 401
    assert client.post("/admin/api/cache/series/invalidate",
                       json={"provider": "reelshort", "series_id": SERIES}).status_code == 401
    assert client.get("/admin/cache", follow_redirects=False).status_code == 302
    csrf = _login(client)
    assert client.delete("/admin/api/cache/entries/1").status_code == 403       # no CSRF header
    assert client.post("/admin/api/cache/entries/1/refresh").status_code == 403
    assert client.get("/admin/cache").status_code == 200
    assert "Episode Cache" in client.get("/admin/cache").text
    assert csrf


def test_admin_status_entries_pagination_search_detail(client, env):
    _seed(client, env, 12)
    _login(client)
    st = client.get("/admin/api/cache/status").json()
    assert st["health"]["status"] == "ok" and st["stats"]["total_rows"] == 12
    assert st["stats"]["cache_misses"] >= 12 and st["stats"]["estimated_note"]
    assert TEST_TOKEN not in json.dumps(st) and st["config"]["turso_token_set"] is True
    page1 = client.get("/admin/api/cache/entries?limit=5&offset=0").json()
    page3 = client.get("/admin/api/cache/entries?limit=5&offset=10").json()
    assert len(page1["items"]) == 5 and page1["has_more"] is True
    assert len(page3["items"]) == 2 and page3["has_more"] is False
    found = client.get("/admin/api/cache/entries?q=adm1&provider=reelshort").json()
    assert {i["episode_id"] for i in found["items"]} == {"adm1", "adm10", "adm11"}
    assert client.get("/admin/api/cache/entries?limit=1000").status_code == 422   # bounded
    assert client.get("/admin/api/cache/entries?state=bogus").status_code == 422
    row_id = page1["items"][0]["id"]
    d = client.get(f"/admin/api/cache/entries/{row_id}").json()["entry"]
    assert "video_url" not in d and "SECRETSIG" not in json.dumps(d)
    assert client.get("/admin/api/cache/entries/999999").status_code == 404


def test_admin_refresh_delete_invalidate(client, env):
    k = _seed(client, env, 3, prefix="mg")
    csrf = _login(client)
    rows = client.get("/admin/api/cache/entries?limit=10").json()["items"]
    target = next(r for r in rows if r["episode_id"] == "mg0")
    fake = env.fake(200, _payload("mg0"))
    used0 = _upstream_used()
    r = client.post(f"/admin/api/cache/entries/{target['id']}/refresh", headers=csrf)
    assert r.status_code == 200 and r.json()["persisted"] is True and fake.calls == 1
    assert _upstream_used() == used0 + 1                      # refresh really uses RapidAPI
    r = client.delete(f"/admin/api/cache/entries/{target['id']}", headers=csrf)
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert client.delete(f"/admin/api/cache/entries/{target['id']}", headers=csrf).status_code == 404
    r = client.post("/admin/api/cache/series/invalidate", headers=csrf,
                    json={"provider": "reelshort", "series_id": SERIES})
    assert r.json()["invalidated"] == 2
    exp = client.get("/admin/api/cache/entries?state=expired").json()
    assert len(exp["items"]) == 2 and all(i["metadata_state"] == "expired" for i in exp["items"])
    fake2 = env.fake(200, _payload("mg1"))
    assert _get(client, k, "mg1").json()["data"]["cache_hit"] is False   # invalidated -> refetch
    assert fake2.calls == 1
    bad = client.post("/admin/api/cache/series/invalidate", headers=csrf,
                      json={"provider": "reelshort", "series_id": "../x y"})
    assert bad.status_code == 400


def test_dashboard_stays_up_when_turso_is_down(client, env):
    _seed(client, env, 1, prefix="dn")
    _login(client)
    env.svc.client.close()
    env.svc.client = broken_service().client
    st = client.get("/admin/api/cache/status")
    assert st.status_code == 200 and st.json()["health"]["status"] == "down"
    assert TEST_TOKEN not in st.text
    assert client.get("/admin/api/cache/entries").status_code == 503
    assert client.get("/admin/cache").status_code == 200
    assert client.get("/admin/overview").status_code == 200
    assert client.get("/admin/api/stats/overview").status_code == 200
    assert client.get("/admin/api/keys").status_code == 200
    assert client.get("/health").status_code == 200


def test_admin_cache_inactive_reports_missing_env_not_fake_credentials(client):
    ec_service.configure(make_cfg(enabled=True, database_url="", auth_token=""), None)
    try:
        _login(client)
        j = client.get("/admin/api/cache/status").json()
        assert j["health"]["status"] == "unconfigured"
        assert j["health"]["missing"] == ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"]
        assert client.get("/admin/api/cache/entries").status_code == 503
    finally:
        ec_service._service = None
