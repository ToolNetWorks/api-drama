"""Pure cache policy: validation, TTL derivation, video-URL expiry, payload rebuild.

No I/O here so every rule is unit-testable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.episode_cache.config import CacheConfig

# Keys that may carry a playable URL. They are stored ONLY in the video_url
# column (never inside extra_json) so one place controls retention + redaction.
VIDEO_FIELDS = ("video_url", "videoUrl", "play_url", "playUrl", "source_url", "url")
_VF_KEY = "__video_field"
_EXPIRES_QS = ("Expires", "expires", "exp", "e", "expire", "x-expires")
_EXPIRES_PAYLOAD = ("video_url_expires_at", "url_expires_at", "expires_at", "expire_at", "expires")


@dataclass(frozen=True)
class EpisodeRecord:
    provider: str
    series_id: str
    episode_id: str
    episode_number: Optional[int]
    video_url: Optional[str]
    duration: Optional[int]
    next_episode_id: Optional[str]
    next_episode_number: Optional[int]
    extra_json: str
    fetched_at: int
    metadata_expires_at: int
    video_url_expires_at: Optional[int]


@dataclass(frozen=True)
class SeriesRecord:
    provider: str
    series_id: str
    series_title: Optional[str]
    series_pic: Optional[str]
    episode_count: Optional[int]
    description: Optional[str]
    language: Optional[str]
    fetched_at: int
    updated_at: int
    metadata_expires_at: int


def _valid_series_id(v: Any) -> bool:
    return isinstance(v, str) and bool(v.strip()) and len(v.strip()) <= 128


def normalize_series(provider: str, series_id: str, now: int, metadata_ttl_s: int,
                     *, title: Optional[Any] = None, pic: Optional[Any] = None,
                     episode_count: Optional[Any] = None, description: Optional[Any] = None,
                     language: Optional[Any] = None) -> tuple[Optional[SeriesRecord], str]:
    """Validate a series payload (full GetSeries or minimal link row).

    Returns (record|None, reason). None fields are preserved as NULL so a
    minimal link row never overwrites known metadata (repo uses COALESCE +
    fetched_at guard). Trailer-only / empty series are rejected.
    """
    if not provider or not _valid_series_id(series_id):
        return None, "bad_series_key"
    sid = series_id.strip()

    def _text(v: Any, cap: int = 500) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        v = v.strip()
        return v[:cap] if v else None

    t = _text(title)
    p = _text(pic, 2000)
    d = _text(description, 8000)
    lang = _text(language, 32)
    cnt = _int(episode_count)
    if cnt is not None and (cnt < 0 or cnt > 100000):
        cnt = None
    if metadata_ttl_s <= 0:
        return None, "ttl_zero"
    return SeriesRecord(
        provider=provider, series_id=sid, series_title=t, series_pic=p,
        episode_count=cnt, description=d, language=lang,
        fetched_at=now, updated_at=now, metadata_expires_at=now + metadata_ttl_s,
    ), "ok"


def validate_skeleton(episode_number: Any, episode_id: Any) -> bool:
    """Skeleton rows (from episode lists / next_episode) need number + id only.

    Episode 0 is the trailer and is never stored as an official episode.
    """
    n = _int(episode_number)
    if n is None or n <= 0 or n > 100000:
        return False
    return isinstance(episode_id, str) and bool(episode_id.strip()) and len(episode_id.strip()) <= 128


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and re.fullmatch(r"-?\d{1,12}", v.strip()):
        return int(v.strip())
    return None


def _sane_epoch(v: Optional[int], now: int) -> Optional[int]:
    if v is None:
        return None
    if v > 10_000_000_000:  # milliseconds
        v //= 1000
    if now - 86400 <= v <= now + 5 * 365 * 86400:
        return v
    return None


def _parse_when(v: Any, now: int) -> Optional[int]:
    n = _int(v)
    if n is not None:
        return _sane_epoch(n, now)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return _sane_epoch(int(dt.timestamp()), now)
        except ValueError:
            return None
    return None


def explicit_video_expiry(url: str, data: dict, now: int) -> Optional[int]:
    """Real expiry advertised by upstream (URL signature or payload), else None."""
    try:
        q = parse_qs(urlsplit(url).query)
    except ValueError:
        q = {}
    low = {k.lower(): v for k, v in q.items()}
    for key in _EXPIRES_QS:
        vals = low.get(key.lower())
        if vals:
            ts = _parse_when(vals[0], now)
            if ts:
                return ts
    amz_exp, amz_date = low.get("x-amz-expires"), low.get("x-amz-date")
    if amz_exp and amz_date:
        try:
            start = datetime.strptime(amz_date[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return int(start.timestamp()) + int(amz_exp[0])
        except ValueError:
            pass
    for key in _EXPIRES_PAYLOAD:
        if key in data:
            ts = _parse_when(data[key], now)
            if ts:
                return ts
    ein = _int(data.get("expires_in"))
    if ein and 0 < ein < 5 * 365 * 86400:
        return now + ein
    return None


def parse_cache_control(header: Optional[str]) -> tuple[bool, Optional[int]]:
    """Return (forbids_storage, max_age_seconds)."""
    if not header:
        return False, None
    forbid, max_age = False, None
    for part in header.lower().split(","):
        part = part.strip()
        if part in ("no-store", "no-cache", "private"):
            forbid = True
        elif part.startswith("max-age="):
            n = _int(part.split("=", 1)[1])
            if n is not None:
                max_age = max(0, n)
    return forbid, max_age


def identify_video_field(data: dict) -> Optional[str]:
    for f in VIDEO_FIELDS:
        if f in data:
            return f
    return None


def normalize(cfg: CacheConfig, provider: str, series_id: str, episode_id: str, data: Any,
              now: int, *, store_video: bool, cache_control: Optional[str] = None,
              respect_cache_control: bool = True) -> tuple[Optional[EpisodeRecord], str]:
    """Validate upstream payload. Returns (record|None, reason). None => do not cache."""
    if not isinstance(data, dict):
        return None, "payload_not_object"
    ep_id = data.get("episode_id")
    if not isinstance(ep_id, str) or not ep_id.strip():
        return None, "missing_episode_id"
    if ep_id != episode_id:
        return None, "episode_id_mismatch"
    number = _int(data.get("episode"))
    if number is None or number < 0:
        return None, "missing_episode_number"
    for sid_key in ("series_id", "book_id"):
        if sid_key in data and str(data[sid_key]) != series_id:
            return None, "series_mismatch"
    forbid, max_age = parse_cache_control(cache_control)
    if respect_cache_control and forbid:
        return None, "upstream_cache_control"

    meta_ttl, vid_ttl = cfg.metadata_ttl(provider), cfg.video_ttl(provider)
    if respect_cache_control and max_age is not None:
        meta_ttl, vid_ttl = min(meta_ttl, max_age), min(vid_ttl, max_age)
    if meta_ttl <= 0:
        return None, "ttl_zero"

    vfield = identify_video_field(data)
    raw_url = data.get(vfield) if vfield else None
    url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
    video_url: Optional[str] = None
    video_exp: Optional[int] = None
    if store_video:
        if url:
            explicit = explicit_video_expiry(url, data, now)
            if explicit is not None:
                video_exp = explicit - cfg.video_expiry_margin_s
                video_exp = min(video_exp, now + vid_ttl) if max_age is not None else video_exp
            else:
                video_exp = now + vid_ttl
            if video_exp > now:
                video_url = url
            else:
                video_exp = now  # already expired: never serve it
        else:
            video_exp = now + vid_ttl  # "no URL upstream" is itself cached for the video TTL

    nxt = data.get("next_episode")
    nxt_id = nxt_no = None
    if isinstance(nxt, dict):
        v = nxt.get("episode_id")
        nxt_id = v if isinstance(v, str) and v else None
        nxt_no = _int(nxt.get("episode"))
    duration = _int(data.get("duration"))

    body = {k: v for k, v in data.items() if k not in VIDEO_FIELDS}
    if vfield:
        body[_VF_KEY] = vfield
    extra = json.dumps(body, separators=(",", ":"), default=str, ensure_ascii=False)
    if len(extra.encode("utf-8")) > cfg.max_extra_bytes:
        slim = {k: body[k] for k in ("episode", "episode_id", "duration", "next_episode",
                                      "distributable", "allow_distribution", "licenced", _VF_KEY)
                if k in body}
        extra = json.dumps(slim, separators=(",", ":"), default=str, ensure_ascii=False)

    return EpisodeRecord(
        provider=provider, series_id=series_id, episode_id=episode_id,
        episode_number=number, video_url=video_url, duration=duration,
        next_episode_id=nxt_id, next_episode_number=nxt_no, extra_json=extra,
        fetched_at=now, metadata_expires_at=now + meta_ttl, video_url_expires_at=video_exp,
    ), "ok"


def rebuild_payload(row: dict, include_video: bool = True) -> Optional[dict]:
    """Rebuild the upstream-shaped episode dict from a cache row (pre-redaction).

    include_video=False nulls the video field so an expired URL can never leak."""
    try:
        body = json.loads(row.get("extra_json") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    vfield = body.pop(_VF_KEY, None)
    if vfield:
        body[vfield] = row.get("video_url") if include_video else None
    return body


def metadata_fresh(row: dict, now: int) -> bool:
    return int(row.get("metadata_expires_at") or 0) > now


def video_fresh(row: dict, now: int) -> bool:
    exp = row.get("video_url_expires_at")
    return exp is not None and int(exp) > now
