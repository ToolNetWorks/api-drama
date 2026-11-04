"""Integration: provider connections CRUD + settings, CSRF-gated, secret-safe."""
from __future__ import annotations

from tests.conftest import ADMIN_PASS, ADMIN_USER


def _login(client):
    client.cookies.clear()
    r = client.post("/admin/api/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200
    return r.json()["csrf_token"]


def test_connections_crud_and_settings(client):
    csrf = _login(client)
    h = {"X-CSRF-Token": csrf}
    # unauthenticated read is rejected
    client.cookies.clear()
    assert client.get("/admin/api/providers/reelshort/connections").status_code == 401
    csrf = _login(client)
    h = {"X-CSRF-Token": csrf}

    base = "/admin/api/providers/dramabox/connections"
    r = client.post(base, json={"name": "k1", "api_key": "supersecretkey1", "priority": 5}, headers=h)
    assert r.status_code == 201, r.text
    conn = r.json()["connection"]
    assert conn["key_hint"] == "****key1"
    assert "supersecretkey1" not in r.text
    cid = conn["id"]

    # duplicate name rejected
    r = client.post(base, json={"name": "k1", "api_key": "anothersecretkey"}, headers=h)
    assert r.status_code == 409

    # list shows hint only
    lst = client.get(base).json()["connections"]
    assert len(lst) == 1 and lst[0]["key_hint"] == "****key1"

    # update quota + disable
    r = client.patch(f"{base}/{cid}", json={"quota_limit": 500, "quota_window": "daily"}, headers=h)
    assert r.status_code == 200
    assert r.json()["connection"]["quota_limit"] == 500
    r = client.post(f"{base}/{cid}/disable", headers=h)
    assert r.json()["connection"]["enabled"] is False
    r = client.post(f"{base}/{cid}/enable", headers=h)
    assert r.json()["connection"]["enabled"] is True

    # settings validation
    r = client.put("/admin/api/providers/dramabox/settings",
                   json={"strategy": "bogus", "rotation_enabled": True, "cooldown_seconds": 60},
                   headers=h)
    assert r.status_code == 400
    r = client.put("/admin/api/providers/dramabox/settings",
                   json={"strategy": "fill_first", "rotation_enabled": False, "cooldown_seconds": 60},
                   headers=h)
    assert r.status_code == 200
    assert r.json()["settings"]["strategy"] == "fill_first"

    # rotate key resets hint, clears error state
    r = client.post(f"{base}/{cid}/key", json={"api_key": "rotatedsecret99"}, headers=h)
    assert r.json()["connection"]["key_hint"] == "****et99"

    # delete
    assert client.delete(f"{base}/{cid}", headers=h).status_code == 200
    assert client.get(base).json()["connections"] == []


def test_mutation_without_csrf_rejected(client):
    csrf = _login(client)
    r = client.post("/admin/api/providers/reelshort/connections",
                    json={"name": "no-csrf", "api_key": "somesecretkey"})
    assert r.status_code in (401, 403)


def test_stats_endpoint_no_secrets(client):
    csrf = _login(client)
    h = {"X-CSRF-Token": csrf}
    client.post("/admin/api/providers/netshort/connections",
                json={"name": "s1", "api_key": "statsecretkey1"}, headers=h)
    r = client.get("/admin/api/providers/netshort/stats")
    assert r.status_code == 200
    blob = r.text
    assert "statsecretkey1" not in blob
    assert "key_encrypted" not in blob
