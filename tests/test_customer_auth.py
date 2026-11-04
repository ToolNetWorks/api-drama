"""Customer gateway authN/Z + standard envelope errors + legacy intact."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.security import generate_raw_key, hash_key, key_prefix
from app.store import connect, utcnow


def _make_key(name="cust", **overrides):
    raw = generate_raw_key()
    row = {"name": name, "key_prefix": key_prefix(raw), "key_hash": hash_key(raw),
           "is_active": 1, "revoked": 0, "rpm": 60, "daily_quota": None,
           "monthly_quota": None, "created_at": utcnow(), "last_used_at": None,
           "expires_at": None}
    row.update(overrides)
    with connect() as db:
        cur = db.execute(
            """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked, rpm,
               daily_quota, monthly_quota, created_at, last_used_at, expires_at)
               VALUES (:name, :key_prefix, :key_hash, :is_active, :revoked, :rpm,
               :daily_quota, :monthly_quota, :created_at, :last_used_at, :expires_at)""", row)
        key_id = cur.lastrowid
    return raw, key_id


def _code(r):
    body = r.json()
    assert body["success"] is False
    assert "request_id" in body and body["request_id"]
    return body["error"]["code"]


def test_missing_key_envelope(client):
    r = client.get("/v1/reelshort/search?q=alpha")
    assert r.status_code == 401
    assert _code(r) == "KEY_MISSING"


def test_invalid_key_envelope(client):
    r = client.get("/v1/reelshort/search", headers={"X-API-Key": "tn_live_nope"})
    assert r.status_code == 401
    assert _code(r) == "KEY_INVALID"


def test_expired_key_envelope(client):
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    raw, _ = _make_key("expired", expires_at=past)
    r = client.get("/v1/reelshort/search", headers={"X-API-Key": raw})
    assert r.status_code == 401
    assert _code(r) == "KEY_EXPIRED"


def test_revoked_key_envelope(client):
    raw, _ = _make_key("revoked", revoked=1, is_active=0)
    r = client.get("/v1/reelshort/search", headers={"X-API-Key": raw})
    assert r.status_code == 401
    assert _code(r) == "KEY_INVALID"


def test_valid_key_search_envelope_and_request_id(client):
    raw, _ = _make_key("valid")
    r = client.get("/v1/reelshort/search?q=alpha", headers={"X-API-Key": raw})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["request_id"]
    assert r.headers.get("X-Request-ID") == body["request_id"]
    assert body["data"]["total"] >= 1


def test_detail_and_episode_not_found(client):
    raw, _ = _make_key("valid2")
    h = {"X-API-Key": raw}
    r = client.get("/v1/reelshort/series/does-not-exist", headers=h)
    assert r.status_code == 404
    assert _code(r) == "NOT_FOUND"
    r = client.get("/v1/reelshort/series/does-not-exist/episodes/e1", headers=h)
    assert r.status_code == 404
    assert _code(r) == "NOT_FOUND"


def test_episode_video_withheld_by_default(client):
    raw, _ = _make_key("valid3")
    r = client.get("/v1/reelshort/series/book1/episodes/ch1",
                   headers={"X-API-Key": raw})
    assert r.status_code == 200, r.text
    ep = r.json()["data"]["episode"]
    assert ep["video_url"] is None
    assert ep["video_redacted"] is True


def test_daily_quota_enforced(client):
    raw, _ = _make_key("quota1", daily_quota=1)
    h = {"X-API-Key": raw}
    assert client.get("/v1/reelshort/search", headers=h).status_code == 200
    r = client.get("/v1/reelshort/search", headers=h)
    assert r.status_code == 429
    assert _code(r) == "QUOTA_EXCEEDED"


def test_rate_limit_enforced(client):
    raw, _ = _make_key("rate1", rpm=2)
    h = {"X-API-Key": raw}
    assert client.get("/v1/reelshort/search", headers=h).status_code == 200
    assert client.get("/v1/reelshort/search", headers=h).status_code == 200
    r = client.get("/v1/reelshort/search", headers=h)
    assert r.status_code == 429
    assert _code(r) == "RATE_LIMITED"


def test_legacy_endpoints_unchanged(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert set(r.json()) == {"ok", "service", "version"}
    r = client.get("/api/reelshort/series?limit=1")
    assert r.status_code == 200
    assert {"total", "limit", "offset", "items"} <= set(r.json())
    r = client.get("/api/reelshort/series/nope")
    assert r.status_code == 404
    assert r.json() == {"detail": "Series not found"}


def test_no_secret_leak_in_customer_responses(client):
    raw, _ = _make_key("leak")
    r = client.get("/v1/reelshort/search?q=beta", headers={"X-API-Key": raw})
    text = r.text.lower()
    assert "rapidapi-key" not in text
    assert "x-rapidapi" not in text
    assert "set-cookie" not in text
