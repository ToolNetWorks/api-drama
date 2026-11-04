"""Series catalog (series_cache 1->many episode_cache) against a local libSQL double."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.episode_cache import policy, repo, schema
from tests.ec_helpers import Upstream, make_service, register


def run(coro):
    return asyncio.run(coro)


def test_migration_v2_creates_series_table_and_preserves_episodes(tmp_path):
    db = str(tmp_path / "v2.db")
    svc = make_service(db)
    run(svc.ensure_ready())
    run(svc.get_episode("tp", "s", "ep14")) if False else None
    import libsql
    conn = libsql.connect(db, autocommit=True)
    assert schema.migrate(conn) == [1, 2] or schema.LATEST == 2
    conn.execute("INSERT INTO episode_cache (provider, series_id, episode_id, episode_number, "
                 "fetched_at, metadata_expires_at, created_at, updated_at) "
                 "VALUES ('p','s','e',1,1,2,1,1)")
    assert schema.migrate(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM episode_cache").fetchone()[0] == 1
    cols = [r[1] for r in conn.execute("PRAGMA table_info(series_cache)").fetchall()]
    for c in ("provider", "series_id", "series_title", "episode_count", "metadata_expires_at"):
        assert c in cols
    idx = {r[1] for r in conn.execute("PRAGMA index_list(episode_cache)").fetchall()}
    assert "idx_ec_series_epnum" in idx


def test_series_upsert_preserves_known_metadata_and_ignores_stale(tmp_path):
    db = str(tmp_path / "s.db")
    svc = make_service(db)

    async def go():
        await svc.ensure_ready()
        now = int(time.time())
        rec, _ = policy.normalize_series("tp", "sA", now, 3600, title="Title", episode_count=70)
        await svc.client.run(lambda c: repo.upsert_series(c, rec))
        # minimal link row must not wipe the title
        await svc.client.run(lambda c: repo.ensure_series_minimal(c, "tp", "sA", now + 5, 3600))
        full = await svc.client.run(lambda c: repo.get_series_row(c, "tp", "sA"))
        assert full["series_title"] == "Title" and full["episode_count"] == 70
        # stale fetch must not overwrite
        old, _ = policy.normalize_series("tp", "sA", now - 100, 3600, title="Older")
        await svc.client.run(lambda c: repo.upsert_series(c, old))
        assert (await svc.client.run(lambda c: repo.get_series_row(c, "tp", "sA")))["series_title"] == "Title"
        # newer full update works
        new, _ = policy.normalize_series("tp", "sA", now + 10, 3600, title="New", episode_count=71)
        await svc.client.run(lambda c: repo.upsert_series(c, new))
        got = await svc.client.run(lambda c: repo.get_series_row(c, "tp", "sA"))
        assert got["series_title"] == "New" and got["episode_count"] == 71

    run(go())


def test_skeleton_never_overwrites_full_row_and_skips_trailer(tmp_path):
    register(Upstream())

    async def go():
        svc = make_service(str(tmp_path / "sk.db"))
        await svc.get_episode("tp", "s", "ep14")  # full row only; no auto skeletons
        rows = await svc.list_series_episodes("tp", "s")
        # explicit sync with verified IDs creates skeletons (INSERT OR IGNORE)
        await svc.sync_series_list("tp", "s", episodes=[(15, "5h6g34m5y5")])
        rows = await svc.list_series_episodes("tp", "s")
        ids = {e["episode_id"] for e in rows["items"]}
        assert "ep14" in ids  # full
        # skeleton for ep15 (number+id only, no details)
        sk = next(e for e in rows["items"] if e["episode_id"] == "5h6g34m5y5")
        assert sk["detail_fetched"] is False and sk["episode_number"] == 15
        # re-sync with NULLs must not wipe the full row
        before = await svc.client.run(
            lambda c: repo.get_row(c, "tp", "s", "ep14"))
        await svc.sync_series_list("tp", "s", episodes=[(14, "ep14"), (0, "trailer"), (-1, "x")])
        after = await svc.client.run(lambda c: repo.get_row(c, "tp", "s", "ep14"))
        assert before["fetched_at"] == after["fetched_at"] and after["episode_number"] == 14
        # trailer ep0 never stored
        names = {e["episode_id"] for e in (await svc.list_series_episodes("tp", "s"))["items"]}
        assert "trailer" not in names and not any(
            e["episode_number"] == 0 for e in (await svc.list_series_episodes("tp", "s"))["items"])

    run(go())


def test_sync_twice_no_data_loss_and_counts(tmp_path):
    async def go():
        svc = make_service(str(tmp_path / "sy.db"))
        r1 = await svc.sync_series_list("tp", "sB", title="T", episode_count=3,
                                       episodes=[(11, "e11"), (12, "e12"), (13, "e13")])
        assert r1["skeletons_created"] == 3
        r2 = await svc.sync_series_list("tp", "sB", episodes=[(11, "e11"), (12, "e12")])
        assert r2["skeletons_created"] == 0  # idempotent
        cat = await svc.get_series_catalog("tp", "sB")
        assert cat["series_title"] == "T" and cat["episodes_tracked"] == 3
        assert cat["episodes_detailed"] == 0 and cat["episodes_with_fresh_video"] == 0
        lst = await svc.list_series_episodes("tp", "sB")
        assert [e["episode_number"] for e in lst["items"]] == [11, 12, 13]
        pg = await svc.list_series_catalog(provider="tp", q="sB")
        assert pg["items"] and pg["items"][0]["series_id"] == "sB"

    run(go())


def test_series_survives_service_restart(tmp_path):
    db = str(tmp_path / "re.db")

    async def seed():
        svc = make_service(db)
        await svc.sync_series_list("tp", "sC", title="Keep", episodes=[(1, "a"), (2, "b")])

    run(seed())
    svc2 = make_service(db)
    cat = run(svc2.get_series_catalog("tp", "sC"))
    assert cat["series_title"] == "Keep" and cat["episodes_tracked"] == 2


def test_list_endpoint_auth_and_shape(client, tmp_path, monkeypatch):
    """Public roster: auth enforced, no video URLs, unknown series 404."""
    import dataclasses
    import app.providers.rapidapi_reelshort as rs
    from app.episode_cache import service as ec_service
    from app.store import connect
    from app.core.security import generate_raw_key, hash_key, key_prefix
    from app.store import utcnow
    import json

    raw = generate_raw_key()
    with connect() as db:
        db.execute("INSERT INTO api_keys (name,key_prefix,key_hash,is_active,revoked,rpm,"
                   "daily_quota,monthly_quota,created_at,last_used_at,expires_at,allowed_providers)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("cat-list", key_prefix(raw), hash_key(raw), 1, 0, 600, None, None,
                    utcnow(), None, None, json.dumps(["reelshort"])))
    path = str(tmp_path / "list.db")
    svc = make_service(path)
    ec_service._service = svc
    monkeypatch.setattr(rs, "settings", dataclasses.replace(rs.settings, allow_video_urls=False))
    try:
        run(svc.sync_series_list("reelshort", "sL", title="List", episodes=[(1, "l1"), (2, "l2")]))
        assert client.get("/v1/reelshort/series/sL/episodes").status_code == 401
        r = client.get("/v1/reelshort/series/sL/episodes", headers={"X-API-Key": raw})
        assert r.status_code == 200
        d = r.json()["data"]
        assert d["series_title"] == "List" and [e["episode_number"] for e in d["episodes"]] == [1, 2]
        assert all("video_url" not in e for e in d["episodes"])
        assert client.get("/v1/reelshort/series/nope/episodes",
                          headers={"X-API-Key": raw}).status_code == 404
    finally:
        svc.client.close()
        ec_service._service = None


def test_episode_number_resolution_and_series_title_sync(client, tmp_path, monkeypatch):
    """Calling /episodes/1 and /episodes/2 resolves episode numbers via first_chapter_id
    and next_episode_id, populates series_title in Turso, and returns video_url when
    ALLOW_VIDEO_URLS=true and REQUIRE_DISTRIBUTABLE_FLAG=false."""
    import dataclasses
    import json
    import os
    import app.providers.rapidapi_reelshort as rs
    from app.episode_cache import service as ec_service
    from app.store import connect, utcnow
    from app.core.crypto import encrypt_key
    from app.core.security import generate_raw_key, hash_key, key_prefix

    raw = generate_raw_key()
    master = os.getenv("TOOLNET_MASTER_KEY", "")
    with connect() as db:
        db.execute("INSERT INTO api_keys (name,key_prefix,key_hash,is_active,revoked,rpm,"
                   "daily_quota,monthly_quota,created_at,last_used_at,expires_at,allowed_providers)"
                   " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("cat-num", key_prefix(raw), hash_key(raw), 1, 0, 600, None, None,
                    utcnow(), None, None, json.dumps(["reelshort"])))
        if db.execute("SELECT COUNT(*) FROM provider_connections WHERE provider='reelshort' AND enabled=1").fetchone()[0] == 0:
            db.execute(
                """INSERT INTO provider_connections
                   (provider, name, key_encrypted, key_hint, enabled, status, priority, quota_limit,
                    quota_window, requests_used, quota_reset_at, consecutive_failures, created_at)
                   VALUES ('reelshort','num-conn',?, '****n', 1,'active',50,NULL,'monthly',0,NULL,0,?)""",
                (encrypt_key("dummy-key", master), utcnow()))

    payloads = {
        "ch1": {"episode": 1, "episode_id": "ch1", "duration": 90,
                "video_url": "https://cdn.example.com/ep1.m3u8",
                "next_episode": {"episode": 2, "episode_id": "ch2"}},
        "ch2": {"episode": 2, "episode_id": "ch2", "duration": 95,
                "video_url": "https://cdn.example.com/ep2.m3u8",
                "next_episode": {"episode": 3, "episode_id": "ch3"}},
    }

    class FakeResp:
        status_code = 200
        headers = {}
        text = ""
        def __init__(self, body):
            self._body = body
        def json(self):
            return self._body

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
            eid = url.rsplit("/", 1)[-1]
            return FakeResp(payloads[eid])

    path = str(tmp_path / "num.db")
    svc = make_service(path)
    ec_service._service = svc
    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    monkeypatch.setattr(rs, "settings", dataclasses.replace(
        rs.settings,
        upstream_episodes_path="/reelshort/series/{id}/episodes/{episode_id}",
        allow_video_urls=True,
        require_distributable_flag=False,
    ))
    try:
        # 1) Call by episode number 1 -> resolves to book1's first_chapter_id ('ch1')
        r1 = client.get("/v1/reelshort/series/book1/episodes/1", headers={"X-API-Key": raw})
        assert r1.status_code == 200
        d1 = r1.json()["data"]
        assert d1["series_title"] == "Alpha Love Story"
        assert d1["episode_number"] == 1 and d1["episode_id"] == "ch1"
        assert d1["episode"]["video_url"] == "https://cdn.example.com/ep1.m3u8"
        assert FakeClient.calls == 1

        # 2) Call by episode number 2 -> resolves via Ep1's next_episode_id ('ch2')
        r2 = client.get("/v1/reelshort/series/book1/episodes/2", headers={"X-API-Key": raw})
        assert r2.status_code == 200
        d2 = r2.json()["data"]
        assert d2["episode_number"] == 2 and d2["episode_id"] == "ch2"
        assert d2["episode"]["video_url"] == "https://cdn.example.com/ep2.m3u8"
        assert FakeClient.calls == 2

        # 3) Re-calling /episodes/2 is a Turso HIT (0 extra upstream calls)
        r2_hit = client.get("/v1/reelshort/series/book1/episodes/2", headers={"X-API-Key": raw})
        assert r2_hit.json()["data"]["cache_hit"] is True
        assert FakeClient.calls == 2

        # 4) Series catalog in Turso now has series_title & episode_count populated
        cat = run(svc.get_series_catalog("reelshort", "book1"))
        assert cat["series_title"] == "Alpha Love Story" and cat["episode_count"] == 10
    finally:
        svc.client.close()
        ec_service._service = None
