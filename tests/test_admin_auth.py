"""Admin authN/Z: session cookie, CSRF, pages guard, docs hiding."""
from __future__ import annotations

from tests.conftest import ADMIN_PASS, ADMIN_USER


def _login(client, username=ADMIN_USER, password=ADMIN_PASS):
    return client.post("/admin/api/login", json={"username": username, "password": password})


def test_login_page_public(client):
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Sign in" in r.text
    assert "rapidapi" not in r.text.lower()


def test_providers_page_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin/providers", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin"


def test_keys_page_requires_login(client):
    client.cookies.clear()
    r = client.get("/admin/keys", follow_redirects=False)
    assert r.status_code == 302


def test_login_wrong_password(client):
    client.cookies.clear()
    r = _login(client, password="wrong-password-1")
    assert r.status_code == 401
    assert r.json()["ok"] is False


def test_login_ok_sets_httponly_cookie_and_csrf(client):
    client.cookies.clear()
    r = _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["csrf_token"]
    set_cookie = r.headers.get("set-cookie", "")
    assert "tn_admin_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie or "samesite=lax" in set_cookie.lower()


def test_me_and_csrf_gating(client):
    client.cookies.clear()
    assert client.get("/admin/api/me").status_code == 401
    r = _login(client)
    csrf = r.json()["csrf_token"]
    # session works
    assert client.get("/admin/api/me").status_code == 200
    # mutation without CSRF is rejected
    r2 = client.post("/admin/api/keys", json={"name": "x"})
    assert r2.status_code in (401, 403)
    # mutation with CSRF succeeds
    r3 = client.post("/admin/api/keys", json={"name": "csrf-ok-key"},
                     headers={"X-CSRF-Token": csrf})
    assert r3.status_code == 201, r3.text
    full = r3.json()["api_key"]
    assert full.startswith("tn_live_")
    # list view never leaks hash or full key
    lst = client.get("/admin/api/keys").json()["keys"]
    mine = [k for k in lst if k["name"] == "csrf-ok-key"][0]
    assert "key_hash" not in mine
    assert full not in str(mine)


def test_logout_invalidates_session(client):
    _login(client)
    assert client.get("/admin/api/me").status_code == 200
    assert client.post("/admin/api/logout").status_code == 200
    assert client.get("/admin/api/me").status_code == 401


def test_admin_api_hidden_from_openapi(client):
    spec = client.get("/openapi.json").json()
    admin_paths = [p for p in spec["paths"] if p.startswith("/admin")]
    assert admin_paths == [], admin_paths
    assert "/v1/reelshort/search" in spec["paths"]
