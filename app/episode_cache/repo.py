"""Turso queries for the episode cache. Sync functions taking a libsql connection
(executed off-loop by TursoClient). Every query is indexed or bounded.
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from app.episode_cache.policy import EpisodeRecord

COLS = ("id", "provider", "series_id", "episode_id", "episode_number", "video_url", "duration",
        "next_episode_id", "next_episode_number", "extra_json", "fetched_at",
        "metadata_expires_at", "video_url_expires_at", "last_accessed_at", "hit_count",
        "created_at", "updated_at")
_SELECT = "SELECT " + ", ".join(COLS) + " FROM episode_cache "
COUNT_CAP = 10000
MAX_PAGE = 100


def _row(r: Any) -> dict:
    return dict(zip(COLS, r))


def get_row(conn: Any, provider: str, series_id: str, episode_id: str) -> Optional[dict]:
    r = conn.execute(_SELECT + "WHERE provider=? AND series_id=? AND episode_id=?",
                     (provider, series_id, episode_id)).fetchone()
    return _row(r) if r else None


def upsert(conn: Any, rec: EpisodeRecord) -> None:
    """UPSERT keyed on UNIQUE(provider, series_id, episode_id). Never lets an older
    fetch overwrite a newer one; preserves created_at/hit_count/last_accessed_at."""
    conn.execute(
        """INSERT INTO episode_cache
           (provider, series_id, episode_id, episode_number, video_url, duration,
            next_episode_id, next_episode_number, extra_json, fetched_at,
            metadata_expires_at, video_url_expires_at, last_accessed_at, hit_count,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,?,?)
           ON CONFLICT(provider, series_id, episode_id) DO UPDATE SET
             episode_number=excluded.episode_number, video_url=excluded.video_url,
             duration=excluded.duration, next_episode_id=excluded.next_episode_id,
             next_episode_number=excluded.next_episode_number, extra_json=excluded.extra_json,
             fetched_at=excluded.fetched_at, metadata_expires_at=excluded.metadata_expires_at,
             video_url_expires_at=excluded.video_url_expires_at, updated_at=excluded.updated_at
           WHERE excluded.fetched_at >= episode_cache.fetched_at""",
        (rec.provider, rec.series_id, rec.episode_id, rec.episode_number, rec.video_url,
         rec.duration, rec.next_episode_id, rec.next_episode_number, rec.extra_json,
         rec.fetched_at, rec.metadata_expires_at, rec.video_url_expires_at,
         rec.fetched_at, rec.fetched_at))


# -- distributed singleflight lease (atomic conditional upsert; server clock) ----
_SRV_NOW = "CAST(strftime('%s','now') AS INTEGER)"


def try_acquire_lock(conn: Any, key: str, owner: str, lease_s: int) -> bool:
    rows = conn.execute(
        f"""INSERT INTO episode_cache_locks (lock_key, owner, lease_expires_at)
            VALUES (?, ?, {_SRV_NOW} + ?)
            ON CONFLICT(lock_key) DO UPDATE SET owner=excluded.owner,
                lease_expires_at=excluded.lease_expires_at
            WHERE episode_cache_locks.lease_expires_at <= {_SRV_NOW}
            RETURNING owner""", (key, owner, int(lease_s))).fetchall()
    return bool(rows) and rows[0][0] == owner


def release_lock(conn: Any, key: str, owner: str) -> None:
    conn.execute("DELETE FROM episode_cache_locks WHERE lock_key=? AND owner=?", (key, owner))


# -- admin operations ------------------------------------------------------------

def delete_row(conn: Any, row_id: int) -> int:
    r = conn.execute("DELETE FROM episode_cache WHERE id=? RETURNING id", (row_id,)).fetchall()
    return len(r)


def delete_key(conn: Any, provider: str, series_id: str, episode_id: str) -> int:
    r = conn.execute("DELETE FROM episode_cache WHERE provider=? AND series_id=? AND episode_id=? "
                     "RETURNING id", (provider, series_id, episode_id)).fetchall()
    return len(r)


def invalidate_series(conn: Any, provider: str, series_id: str) -> int:
    """Soft-invalidate: expire both TTLs so the next request refreshes; keeps counters."""
    r = conn.execute(
        "UPDATE episode_cache SET metadata_expires_at=0, video_url_expires_at=0, updated_at=? "
        "WHERE provider=? AND series_id=? RETURNING id",
        (int(time.time()), provider, series_id)).fetchall()
    return len(r)


# -- series catalog (1 row per provider+series; episode rows link via same key) ---

def upsert_series(conn: Any, rec: Any) -> None:
    """UPSERT a series row. NULL fields never overwrite known metadata; an older
    fetch never overwrites a newer one (fetched_at guard)."""
    conn.execute(
        """INSERT INTO series_cache
           (provider, series_id, series_title, series_pic, episode_count, description,
            language, fetched_at, updated_at, metadata_expires_at, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(provider, series_id) DO UPDATE SET
             series_title=COALESCE(excluded.series_title, series_cache.series_title),
             series_pic=COALESCE(excluded.series_pic, series_cache.series_pic),
             episode_count=COALESCE(excluded.episode_count, series_cache.episode_count),
             description=COALESCE(excluded.description, series_cache.description),
             language=COALESCE(excluded.language, series_cache.language),
             fetched_at=excluded.fetched_at, updated_at=excluded.updated_at,
             metadata_expires_at=excluded.metadata_expires_at
           WHERE excluded.fetched_at >= series_cache.fetched_at""",
        (rec.provider, rec.series_id, rec.series_title, rec.series_pic, rec.episode_count,
         rec.description, rec.language, rec.fetched_at, rec.updated_at,
         rec.metadata_expires_at, rec.fetched_at))


def ensure_series_minimal(conn: Any, provider: str, series_id: str, now: int,
                          metadata_ttl_s: int) -> None:
    """Create the link row if missing. INSERT OR IGNORE: never touches existing rows."""
    conn.execute(
        """INSERT OR IGNORE INTO series_cache
           (provider, series_id, fetched_at, updated_at, metadata_expires_at, created_at)
           VALUES (?,?,?,?,?,?)""",
        (provider, series_id, now, now, now + metadata_ttl_s, now))


def get_series_row(conn: Any, provider: str, series_id: str) -> Optional[dict]:
    r = conn.execute("SELECT id, provider, series_id, series_title, series_pic, episode_count, "
                     "description, language, fetched_at, updated_at, metadata_expires_at, "
                     "created_at FROM series_cache WHERE provider=? AND series_id=?",
                     (provider, series_id)).fetchone()
    if not r:
        return None
    keys = ("id", "provider", "series_id", "series_title", "series_pic", "episode_count",
            "description", "language", "fetched_at", "updated_at", "metadata_expires_at",
            "created_at")
    return dict(zip(keys, r))


def list_series(conn: Any, *, provider: str = "", q: str = "",
                limit: int = 25, offset: int = 0) -> dict:
    limit = max(1, min(limit, MAX_PAGE))
    offset = max(0, offset)
    where, args = [], []
    if provider:
        where.append("provider=?")
        args.append(provider)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(series_id LIKE ? ESCAPE '\\' OR series_title LIKE ? ESCAPE '\\')")
        args += ["%" + esc + "%", "%" + esc + "%"]
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    rows = conn.execute(
        "SELECT id, provider, series_id, series_title, episode_count, fetched_at, "
        "updated_at, metadata_expires_at FROM series_cache " + clause +
        "ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit + 1, offset)).fetchall()
    has_more = len(rows) > limit
    items = [{"id": r[0], "provider": r[1], "series_id": r[2], "series_title": r[3],
              "episode_count": r[4], "fetched_at": r[5], "updated_at": r[6],
              "metadata_expires_at": r[7]} for r in rows[:limit]]
    return {"items": items, "limit": limit, "offset": offset, "has_more": has_more}


def insert_episode_skeleton(conn: Any, provider: str, series_id: str,
                            episode_number: int, episode_id: str, now: int,
                            metadata_ttl_s: int) -> bool:
    """Skeleton from an episode list / next_episode pointer: number + id only.

    INSERT OR IGNORE so a skeleton can never overwrite a fully fetched row.
    Returns True when a new skeleton row was created.
    """
    cur = conn.execute("SELECT 1 FROM episode_cache WHERE provider=? AND series_id=? "
                       "AND episode_id=?", (provider, series_id, episode_id)).fetchone()
    if cur:
        return False
    conn.execute(
        """INSERT OR IGNORE INTO episode_cache
           (provider, series_id, episode_id, episode_number, fetched_at,
            metadata_expires_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (provider, series_id, episode_id, episode_number, now, now + metadata_ttl_s,
         now, now))
    return True


def resolve_episode_id_by_number(conn: Any, provider: str, series_id: str,
                                 episode_number: int) -> Optional[str]:
    """Resolve an episode number (1, 2, 3...) to its episode_id via direct row or
    the preceding episode's next_episode pointer."""
    r = conn.execute(
        "SELECT episode_id FROM episode_cache WHERE provider=? AND series_id=? "
        "AND episode_number=? ORDER BY fetched_at DESC LIMIT 1",
        (provider, series_id, episode_number)).fetchone()
    if r and r[0]:
        return str(r[0])
    r2 = conn.execute(
        "SELECT next_episode_id FROM episode_cache WHERE provider=? AND series_id=? "
        "AND next_episode_number=? AND next_episode_id IS NOT NULL "
        "ORDER BY fetched_at DESC LIMIT 1",
        (provider, series_id, episode_number)).fetchone()
    if r2 and r2[0]:
        return str(r2[0])
    return None


def find_closest_preceding_episode(conn: Any, provider: str, series_id: str,
                                   target_number: int) -> Optional[tuple[int, str, Optional[int], Optional[str]]]:
    """Return (episode_number, episode_id, next_episode_number, next_episode_id) for the
    highest cached episode < target_number."""
    r = conn.execute(
        "SELECT episode_number, episode_id, next_episode_number, next_episode_id "
        "FROM episode_cache WHERE provider=? AND series_id=? "
        "AND episode_number IS NOT NULL AND episode_number < ? "
        "ORDER BY episode_number DESC LIMIT 1",
        (provider, series_id, target_number)).fetchone()
    if not r or r[0] is None or not r[1]:
        return None
    return int(r[0]), str(r[1]), (int(r[2]) if r[2] is not None else None), (str(r[3]) if r[3] else None)


def list_episodes(conn: Any, provider: str, series_id: str, *,
                  limit: int = 200, offset: int = 0, now: Optional[int] = None) -> dict:
    """Ordered episode roster for one series. No video URLs (list never leaks them)."""
    now = now or int(time.time())
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = conn.execute(
        "SELECT episode_number, episode_id, duration, fetched_at, metadata_expires_at, "
        "video_url_expires_at, hit_count, last_accessed_at, (video_url IS NOT NULL), "
        "(extra_json IS NOT NULL), next_episode_number, next_episode_id FROM episode_cache "
        "WHERE provider=? AND series_id=? "
        "ORDER BY episode_number ASC LIMIT ? OFFSET ?",
        (provider, series_id, limit + 1, offset)).fetchall()
    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        items.append({
            "episode_number": r[0], "episode_id": r[1], "duration": r[2],
            "fetched_at": r[3], "metadata_expires_at": r[4], "video_url_expires_at": r[5],
            "hit_count": r[6], "last_accessed_at": r[7],
            "has_video_url": bool(r[8]),
            "detail_fetched": bool(r[9]),
            "next_episode_number": r[10],
            "next_episode_id": r[11],
            "metadata_state": "fresh" if (r[4] or 0) > now else "expired",
            "video_state": ("none" if r[5] is None else "fresh" if r[5] > now else "expired"),
        })
    return {"items": items, "limit": limit, "offset": offset, "has_more": has_more}


def count_series_episodes(conn: Any, provider: str, series_id: str,
                          now: Optional[int] = None) -> dict:
    now = now or int(time.time())
    r = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN extra_json IS NOT NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN video_url IS NOT NULL AND video_url_expires_at > ? THEN 1 ELSE 0 END) "
        "FROM episode_cache WHERE provider=? AND series_id=?",
        (now, provider, series_id)).fetchone()
    return {"episodes_tracked": int(r[0] or 0), "episodes_detailed": int(r[1] or 0),
            "episodes_with_fresh_video": int(r[2] or 0)}


def list_rows(conn: Any, *, provider: str = "", q: str = "", state: str = "",
              limit: int = 25, offset: int = 0, now: Optional[int] = None) -> dict:
    now = now or int(time.time())
    limit = max(1, min(limit, MAX_PAGE))
    offset = max(0, offset)
    where, args = [], []
    if provider:
        where.append("provider=?")
        args.append(provider)
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(series_id LIKE ? ESCAPE '\\' OR episode_id LIKE ? ESCAPE '\\')")
        args += [esc + "%", esc + "%"]
    if state == "fresh":
        where.append("metadata_expires_at > ?")
        args.append(now)
    elif state == "expired":
        where.append("metadata_expires_at <= ?")
        args.append(now)
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    rows = conn.execute(
        "SELECT id, provider, series_id, episode_id, episode_number, fetched_at, "
        "metadata_expires_at, video_url_expires_at, hit_count, last_accessed_at, "
        "(video_url IS NOT NULL) FROM episode_cache " + clause +
        "ORDER BY id DESC LIMIT ? OFFSET ?", (*args, limit + 1, offset)).fetchall()
    has_more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        items.append({
            "id": r[0], "provider": r[1], "series_id": r[2], "episode_id": r[3],
            "episode_number": r[4], "fetched_at": r[5], "metadata_expires_at": r[6],
            "video_url_expires_at": r[7], "hit_count": r[8], "last_accessed_at": r[9],
            "has_video_url": bool(r[10]),
            "metadata_state": "fresh" if (r[6] or 0) > now else "expired",
            "video_state": ("none" if r[7] is None else "fresh" if r[7] > now else "expired"),
        })
    return {"items": items, "limit": limit, "offset": offset, "has_more": has_more}


def mask_url(url: Optional[str]) -> Optional[str]:
    """Admin view: scheme://host/path only; signed query strings are never shown."""
    if not url:
        return None
    try:
        p = urlsplit(url)
        return f"{p.scheme}://{p.netloc}{p.path[:60]}" + ("?…" if p.query else "")
    except ValueError:
        return "(unparseable)"


def get_detail(conn: Any, row_id: int) -> Optional[dict]:
    r = conn.execute(_SELECT + "WHERE id=?", (row_id,)).fetchone()
    if not r:
        return None
    d = _row(r)
    d["video_url_masked"] = mask_url(d.pop("video_url"))
    return d


# -- batched usage metrics -------------------------------------------------------

def flush_hits(conn: Any, hits: Iterable[tuple[int, int, str, str, str]]) -> None:
    """hits: (count, last_ts, provider, series_id, episode_id). One call, many rows."""
    conn.executemany(
        "UPDATE episode_cache SET hit_count = hit_count + ?, "
        "last_accessed_at = MAX(COALESCE(last_accessed_at, 0), ?) "
        "WHERE provider=? AND series_id=? AND episode_id=?", list(hits))


_STAT_COLS = ("hits", "misses", "expired", "coalesced", "upstream_calls", "upstream_errors",
              "backend_errors", "not_cacheable", "lock_timeouts")


def flush_stats(conn: Any, day: str, per_provider: dict[str, dict[str, int]]) -> None:
    sets = ", ".join(f"{c}={c}+excluded.{c}" for c in _STAT_COLS)
    cols = ", ".join(_STAT_COLS)
    ph = ", ".join("?" for _ in _STAT_COLS)
    conn.executemany(
        f"INSERT INTO episode_cache_stats (day, provider, {cols}) VALUES (?, ?, {ph}) "
        f"ON CONFLICT(day, provider) DO UPDATE SET {sets}",
        [(day, prov, *[int(c.get(k, 0)) for k in _STAT_COLS]) for prov, c in per_provider.items()])


def housekeeping(conn: Any) -> None:
    conn.execute(f"DELETE FROM episode_cache_locks WHERE lease_expires_at < {_SRV_NOW} - 3600")
    conn.execute("DELETE FROM episode_cache_stats WHERE day < date('now', '-400 days')")


def read_stats(conn: Any, now: int) -> dict:
    """Aggregate stats without scanning episode_cache: counter row + tiny stats table +
    capped, index-range expiry counts."""
    total = conn.execute("SELECT value FROM episode_cache_counters WHERE name='total_rows'"
                         ).fetchone()
    agg = conn.execute("SELECT COALESCE(SUM(hits),0), COALESCE(SUM(misses),0), "
                       "COALESCE(SUM(expired),0), COALESCE(SUM(coalesced),0), "
                       "COALESCE(SUM(upstream_calls),0), COALESCE(SUM(upstream_errors),0), "
                       "COALESCE(SUM(not_cacheable),0), COALESCE(SUM(backend_errors),0) "
                       "FROM episode_cache_stats").fetchone()
    exp_meta = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM episode_cache WHERE metadata_expires_at <= ? "
        f"LIMIT {COUNT_CAP + 1})", (now,)).fetchone()[0]
    exp_vid = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM episode_cache WHERE video_url_expires_at IS NOT NULL "
        f"AND video_url_expires_at <= ? LIMIT {COUNT_CAP + 1})", (now,)).fetchone()[0]
    size = None
    try:
        pc = conn.execute("PRAGMA page_count").fetchone()
        ps = conn.execute("PRAGMA page_size").fetchone()
        if pc and ps and pc[0] is not None and ps[0] is not None:
            size = int(pc[0]) * int(ps[0])
    except Exception:  # noqa: BLE001 — metric is optional
        size = None
    try:
        series_total = conn.execute("SELECT COUNT(*) FROM series_cache").fetchone()[0]
    except Exception:  # noqa: BLE001 — pre-v2 database
        series_total = 0
    return {
        "total_rows": int(total[0]) if total else 0,
        "series_total": int(series_total or 0),
        "hits": int(agg[0]), "misses": int(agg[1]), "expired_events": int(agg[2]),
        "coalesced": int(agg[3]), "upstream_calls": int(agg[4]), "upstream_errors": int(agg[5]),
        "not_cacheable": int(agg[6]), "backend_errors": int(agg[7]),
        "expired_metadata": min(int(exp_meta), COUNT_CAP),
        "expired_video": min(int(exp_vid), COUNT_CAP),
        "expired_capped": int(exp_meta) > COUNT_CAP or int(exp_vid) > COUNT_CAP,
        "db_size_bytes": size,
    }


def schema_version(conn: Any) -> int:
    """Read-only (no DDL): 0 when the schema has not been migrated yet."""
    try:
        r = conn.execute("SELECT COALESCE(MAX(version), 0) FROM episode_cache_schema").fetchone()
        return int(r[0] or 0)
    except Exception:  # noqa: BLE001 — missing table
        return 0
