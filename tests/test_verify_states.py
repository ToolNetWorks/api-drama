"""Four-state verification + live labelling (mocked transport, no real upstream)."""
from __future__ import annotations

import os
from types import SimpleNamespace

from tests.conftest import ADMIN_PASS, ADMIN_USER

from app.core.crypto import encrypt_key
from app.store import connect, utcnow

MASTER = os.getenv("TOOLNET_MASTER_KEY", "")
PROV = "reelshort"


def _login(client):
    client.cookies.clear()
    r = client.post("/admin/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def _add_conn(name):
    with connect() as db:
        db.execute("DELETE FROM provider_connections WHERE provider=? AND name=?", (PROV, name))
        cur = db.execute(
            """INSERT INTO provider_connections
               (provider, name, key_encrypted, key_hint, enabled, status, priority,
                quota_limit, quota_window, requests_used, quota_reset_at,
                consecutive_failures, created_at)
               VALUES (?, ?, ?, '****test', 1, 'active', 50, NULL, 'monthly', 0, NULL, 0, ?)""",
            (PROV, name, encrypt_key("dummy-key-" + name, MASTER), utcnow()),
        )
        return cur.lastrowid


def _get(cid):
    with connect() as db:
        return dict(db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone())


def _fake_client(status):
    class FakeResp:
        status_code = status
        headers = {}
        text = '{"message":"Endpoint \\u0027/__toolnet_key_check__\\u0027 does not exist"}'
    class FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return FakeResp()
    return FakeClient


def test_probe_404_never_marks_healthy(client, monkeypatch):
    csrf = _login(client)
    cid = _add_conn("probe404")
    monkeypatch.setattr("httpx.AsyncClient", _fake_client(404))
    r = client.post(f"/admin/api/providers/reelshort/connections/{cid}/test",
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False
    assert j["key_valid"] is True
    assert j.get("endpoint_ok") is None
    row = _get(cid)
    assert row["status"] == "active"          # not failed
    assert row["last_check_ok"] is None       # never verified
    assert row["key_valid"] == 1
    assert row["requests_used"] == 1          # quota consumed, honestly counted
    assert "dummy-key-probe404" not in r.text


def test_probe_403_marks_key_invalid_not_healthy(client, monkeypatch):
    csrf = _login(client)
    cid = _add_conn("probe403")
    monkeypatch.setattr("httpx.AsyncClient", _fake_client(403))
    r = client.post(f"/admin/api/providers/reelshort/connections/{cid}/test",
                    headers={"X-CSRF-Token": csrf})
    j = r.json()
    assert j["ok"] is False and j["key_valid"] is False
    row = _get(cid)
    assert row["status"] == "auth_error"
    assert row["key_valid"] == 0
    assert row["last_check_ok"] != 1


def test_local_results_labelled_not_live(client):
    from app.core.security import generate_raw_key, hash_key, key_prefix
    raw = generate_raw_key()
    with connect() as db:
        db.execute(
            """INSERT INTO api_keys (name, key_prefix, key_hash, is_active, revoked, rpm,
               daily_quota, monthly_quota, created_at, last_used_at, expires_at)
               VALUES ('liveflag', ?, ?, 1, 0, 60, NULL, NULL, ?, NULL, NULL)""",
            (key_prefix(raw), hash_key(raw), utcnow()),
        )
    h = {"X-API-Key": raw}
    for url in ("/v1/reelshort/search?q=altarboy",
                "/v1/reelshort/series/book1",
                "/v1/reelshort/series/book1/episodes/ch1"):
        r = client.get(url, headers=h)
        assert r.status_code == 200, url
        d = r.json()["data"]
        assert d["live"] is False, url
        assert d["source"] == "local-cache", url
