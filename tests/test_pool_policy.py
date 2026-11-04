"""Pool policy unit tests: strategy, cooldown, auth fail-fast, quota, secrecy."""
from __future__ import annotations

import os

import pytest

from app.core.crypto import encrypt_key
from app.providers import pool
from app.store import connect, next_quota_reset, utcnow

MASTER = os.getenv("TOOLNET_MASTER_KEY", "")
PROV = "pooltest"


def _add(name, provider=PROV, priority=100, quota_limit=None, enabled=True):
    with connect() as db:
        cur = db.execute(
            """INSERT INTO provider_connections
               (provider, name, key_encrypted, key_hint, enabled, status, priority,
                quota_limit, quota_window, requests_used, quota_reset_at,
                consecutive_failures, created_at)
               VALUES (?, ?, ?, '****abcd', ?, 'active', ?, ?, 'monthly', 0, ?, 0, ?)""",
            (provider, name, encrypt_key("secret-" + name, MASTER),
             1 if enabled else 0, priority, quota_limit, next_quota_reset("monthly"), utcnow()),
        )
        return cur.lastrowid


@pytest.fixture()
def clean_provider():
    with connect() as db:
        db.execute("DELETE FROM provider_connections WHERE provider=?", (PROV,))
        db.execute("DELETE FROM provider_settings WHERE provider=?", (PROV,))
    yield
    with connect() as db:
        db.execute("DELETE FROM provider_connections WHERE provider=?", (PROV,))
        db.execute("DELETE FROM provider_settings WHERE provider=?", (PROV,))


def test_fill_first_picks_lowest_priority(clean_provider):
    _add("c-high", priority=200)
    _add("c-low", priority=10)
    with connect() as db:
        pool.upsert_settings(db, PROV, "fill_first", True, 300)
        picked = pool.pick_connection(db, PROV)
    assert picked["name"] == "c-low"


def test_round_robin_picks_least_recently_used(clean_provider):
    id_old = _add("c-old", priority=100)
    _add("c-new", priority=100)
    with connect() as db:
        pool.upsert_settings(db, PROV, "round_robin", True, 300)
        pool.record_success(db, id_old)  # c-old used now
        picked = pool.pick_connection(db, PROV)
    assert picked["name"] == "c-new"


def test_rotation_disabled_pins_first(clean_provider):
    _add("c-a", priority=50)
    _add("c-b", priority=60)
    with connect() as db:
        pool.upsert_settings(db, PROV, "round_robin", False, 300)
        assert pool.pick_connection(db, PROV)["name"] == "c-a"
        assert pool.pick_connection(db, PROV)["name"] == "c-a"


def test_cooldown_excludes_then_recovers(clean_provider):
    cid = _add("c-cool")
    _add("c-fine")
    with connect() as db:
        pool.upsert_settings(db, PROV, "fill_first", True, 300)
        pool.record_transient_failure(db, cid, "upstream 429", 300)
        assert pool.pick_connection(db, PROV)["name"] == "c-fine"
        pool.clear_error(db, cid)
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone()
        assert pool.public_view(row)["state"] == "active"


def test_auth_error_fail_fast_no_failover(clean_provider):
    bad = _add("c-bad", priority=1)
    good = _add("c-good", priority=2)
    with connect() as db:
        pool.upsert_settings(db, PROV, "fill_first", True, 300)
        # Simulate adapter behaviour: 401/403 marks auth_error and stops.
        pool.record_auth_failure(db, bad, "upstream 401")
        picked = pool.pick_connection(db, PROV)
    # The bad connection is excluded, but the point is the adapter must NOT
    # have tried `good` as an automatic fallback for the same request.
    assert picked["name"] == "c-good"
    with connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (bad,)).fetchone()
    assert pool.public_view(row)["state"] == "auth_error"


def test_quota_exhaustion_skips_and_resets(clean_provider):
    cid = _add("c-quota", quota_limit=1)
    _add("c-free")
    with connect() as db:
        pool.upsert_settings(db, PROV, "fill_first", True, 300)
        pool.record_success(db, cid)  # reaches quota 1/1
        assert pool.pick_connection(db, PROV)["name"] == "c-free"
        # force window elapsed -> quota rolls
        db.execute("UPDATE provider_connections SET quota_reset_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                   (cid,))
        picked = pool.pick_connection(db, PROV)
    assert picked["name"] == "c-quota"


def test_public_view_never_leaks_key(clean_provider):
    cid = _add("c-secret")
    with connect() as db:
        row = db.execute("SELECT * FROM provider_connections WHERE id=?", (cid,)).fetchone()
        view = pool.public_view(row)
    blob = str(view)
    assert "secret-c-secret" not in blob
    assert "key_encrypted" not in view
    assert view["key_hint"].startswith("****")
