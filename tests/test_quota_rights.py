"""Real quota sync + per-provider key rights + overview + docs page."""
from __future__ import annotations

import json
import os

from tests.conftest import ADMIN_PASS, ADMIN_USER

from app.core.crypto import encrypt_key
from app.core.security import generate_raw_key, hash_key, key_prefix
from app.providers import pool
from app.store import connect, utcnow

MASTER = os.getenv("TOOLNET_MASTER_KEY", "")
PROV = "pooltest"


def _login(client):
    client.cookies.clear()
    r = client.post("/admin/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _add_conn(name, provider=PROV):
    from app.store import run_migrations
    run_migrations()  # idempotent; unit tests may run before app lifespan
    with connect() as db:
        db.execute("DELETE FROM provider_connections WHERE provider=? AND name=?", (provider, name))
        cur = db.execute(
            """INSERT INTO provider_connections
               (provider, name, key_encrypted, key_hint, enabled, status, priority,
                quota_limit, quota_window, requests_used, quota_reset_at,
                consecutive_failures, created_at)
               VALUES (?, ?, ?, '****t', 1, 'active', 50, NULL, 'monthly', 0, NULL, 0, ?)""",
            (provider, name, encrypt_key("k-" + name, MASTER), utcnow()),
        )
        return cur.lastrowid


def _get_conn(cid):
    with connect() as db:
        return dict(db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone())


def _make_customer(name, allowed):
    raw = generate_raw_key()
    with connect() as db:
        db.execute(
            """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked, rpm,
               daily_quota, monthly_quota, created_at, last_used_at, expires_at, allowed_providers)
               VALUES (?, ?, ?, 1, 0, 60, NULL, NULL, ?, NULL, NULL, ?)""",
            (name, key_prefix(raw), hash_key(raw), utcnow(),
             json.dumps(allowed) if allowed is not None else None),
        )
    return raw


def test_quota_sync_adopts_upstream_plan():
    cid = _add_conn("qsync")
    with connect() as db:
        pool.sync_quota_from_headers(db, cid, {
            "X-RateLimit-Requests-Limit": "100",
            "X-RateLimit-Requests-Remaining": "97",
            "X-RateLimit-Requests-Reset": "2675458",
        })
    row = _get_conn(cid)
    assert row["quota_limit"] == 100
    assert row["quota_remaining"] == 97
    assert row["quota_reset_at"] and row["quota_synced_at"]
    view = pool.public_view(dict_to_row(cid))
    assert "97 left" in view["quota_text"]
    assert view["quota_remaining"] == 97


def dict_to_row(cid):
    with connect() as db:
        return db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone()


def test_quota_sync_preserves_owner_cap_and_exhaustion():
    cid = _add_conn("qowner")
    with connect() as db:
        db.execute("UPDATE provider_connections SET quota_limit=50 WHERE id=?", (cid,))
        pool.sync_quota_from_headers(db, cid, {
            "x-ratelimit-requests-limit": "100",
            "x-ratelimit-requests-remaining": "0",
            "x-ratelimit-requests-reset": "9999",
        })
    row = _get_conn(cid)
    assert row["quota_limit"] == 50  # owner cap wins
    assert row["quota_remaining"] == 0
    with connect() as db:
        full = db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone()
        assert pool.public_view(full)["state"] == "exhausted"


def test_quota_sync_ignores_garbage():
    cid = _add_conn("qbad")
    with connect() as db:
        pool.sync_quota_from_headers(db, cid, {"X-RateLimit-Requests-Limit": "abc"})
        pool.sync_quota_from_headers(db, cid, {})
    row = _get_conn(cid)
    assert row["quota_limit"] is None and row["quota_remaining"] is None


def test_provider_forbidden(client):
    raw = _make_customer("noprov", ["dramabox"])
    r = client.get("/v1/reelshort/search?q=x", headers={"X-API-Key": raw})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "PROVIDER_FORBIDDEN"


def test_provider_allowed_and_legacy_null(client):
    raw = _make_customer("yesprov", ["reelshort", "dramabox"])
    assert client.get("/v1/reelshort/search?q=x", headers={"X-API-Key": raw}).status_code == 200
    raw2 = _make_customer("legacyprov", None)
    assert client.get("/v1/reelshort/search?q=x", headers={"X-API-Key": raw2}).status_code == 200


def test_admin_key_providers_crud(client):
    csrf = _login(client)
    h = {"X-CSRF-Token": csrf}
    r = client.post("/admin/api/keys",
                    json={"name": "provkey", "allowed_providers": ["reelshort", "netshort"]}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["key"]["allowed_providers"] == ["netshort", "reelshort"]
    kid = r.json()["key"]["id"]
    r = client.post("/admin/api/keys", json={"name": "badprov", "allowed_providers": ["nope"]}, headers=h)
    assert r.status_code == 400
    r = client.patch(f"/admin/api/keys/{kid}", json={"allowed_providers": ["shortmax"]}, headers=h)
    assert r.status_code == 200
    assert r.json()["key"]["allowed_providers"] == ["shortmax"]
    assert "key_hash" not in r.text


def test_overview_auth_and_shape(client):
    client.cookies.clear()
    assert client.get("/admin/api/stats/overview").status_code == 401
    _login(client)
    r = client.get("/admin/api/stats/overview")
    assert r.status_code == 200
    j = r.json()
    assert {"totals", "cache_served", "upstream_calls", "days", "top_keys", "connections"} <= set(j)
    assert {"total", "ok", "errors"} <= set(j["totals"])
    assert isinstance(j["days"], list)


def test_overview_page_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin/overview", follow_redirects=False)
    assert r.status_code == 302
    _login(client)
    r = client.get("/admin/overview")
    assert r.status_code == 200 and "Requests per day" in r.text


def test_developers_page_public_no_secrets(client):
    import re
    r = client.get("/developers")
    assert r.status_code == 200
    text = r.text
    assert "X-API-Key" in text and "cURL" in text and "JavaScript" in text
    assert "PROVIDER_FORBIDDEN" in text
    low = text.lower()
    # Placeholders (tn_live_xxxx) are fine; real key material is not.
    assert not re.search(r'tn_live_[A-Za-z0-9_\-]{40,}', text)  # real keys: 43 chars
    assert "x-rapidapi-key" not in low  # upstream header name must not appear
    assert not re.search(r'\b[0-9a-f]{40,}\b', low)  # rapidapi-style hex secrets
