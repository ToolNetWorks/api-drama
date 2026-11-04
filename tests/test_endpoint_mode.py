"""Connection Test endpoint-mode (mocked transport, no real upstream)."""
from __future__ import annotations

import dataclasses
import os

from tests.conftest import ADMIN_PASS, ADMIN_USER

import app.routers.admin_providers as ap
from app.core.config import settings as real_settings
from app.core.crypto import encrypt_key
from app.store import connect

MASTER = os.getenv("TOOLNET_MASTER_KEY", "")
PROV = "reelshort"
EP_PATH = "/reelshort/series/{id}/episodes/{episode_id}"


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
            (PROV, name, encrypt_key("dummy-key-" + name, MASTER),
             __import__("app.store", fromlist=["utcnow"]).utcnow()),
        )
        return cur.lastrowid


def _get(cid):
    with connect() as db:
        return dict(db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone())


def _ep_settings():
    return dataclasses.replace(
        real_settings,
        upstream_episodes_path=EP_PATH,
        rapid_test_series_id="6a7619d41dcbbd8e12015bfe",
        rapid_test_episode_id="8giu100fn0",
    )


def _fake_client(status, payload=None):
    import json as _json

    class FakeResp:
        status_code = status
        headers = {}
        text = _json.dumps(payload or {})
        def json(self):
            return payload or {}
    class FakeClient:
        seen = {}
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, url, *a, **k):
            FakeClient.seen["url"] = url
            FakeClient.seen["key"] = (k.get("headers") or {}).get("X-RapidAPI-Key", "")
            return FakeResp()
    return FakeClient


def test_endpoint_200_marks_healthy(client, monkeypatch):
    csrf = _login(client)
    cid = _add_conn("ep200")
    monkeypatch.setattr(ap, "settings", _ep_settings())
    fake = _fake_client(200, {"episode": 11, "duration": 69, "video_url": "https://x/y.m3u8"})
    monkeypatch.setattr("httpx.AsyncClient", fake)
    r = client.post(f"/admin/api/providers/reelshort/connections/{cid}/test",
                    headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["mode"] == "endpoint" and j["endpoint_ok"] == 1
    assert "/reelshort/series/6a7619d41dcbbd8e12015bfe/episodes/8giu100fn0" in fake.seen["url"]
    assert fake.seen["key"] == "dummy-key-ep200"  # per-connection key, not hardcoded
    row = _get(cid)
    assert row["endpoint_ok"] == 1 and row["last_check_ok"] == 1 and row["status"] == "active"
    assert "dummy-key-ep200" not in r.text


def test_endpoint_404_marks_endpoint_bad_not_healthy(client, monkeypatch):
    csrf = _login(client)
    cid = _add_conn("ep404")
    monkeypatch.setattr(ap, "settings", _ep_settings())
    monkeypatch.setattr("httpx.AsyncClient", _fake_client(404, {"message": "Endpoint no"}))
    r = client.post(f"/admin/api/providers/reelshort/connections/{cid}/test",
                    headers={"X-CSRF-Token": csrf})
    j = r.json()
    assert j["ok"] is False and j["mode"] == "endpoint" and j["endpoint_ok"] == 0
    assert j["status"] == 404
    row = _get(cid)
    assert row["endpoint_ok"] == 0 and row["last_check_ok"] == 0
    assert row["status"] == "active"  # 404 is not an auth failure
    assert row["key_valid"] == 1      # gateway accepted the key


def test_endpoint_403_marks_key_rejected(client, monkeypatch):
    csrf = _login(client)
    cid = _add_conn("ep403")
    monkeypatch.setattr(ap, "settings", _ep_settings())
    monkeypatch.setattr("httpx.AsyncClient", _fake_client(403, {"message": "forbidden"}))
    r = client.post(f"/admin/api/providers/reelshort/connections/{cid}/test",
                    headers={"X-CSRF-Token": csrf})
    j = r.json()
    assert j["ok"] is False and j["key_valid"] is False
    assert _get(cid)["status"] == "auth_error"
