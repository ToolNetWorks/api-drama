# RapidAPI ReelShort — exact endpoint info required (req 3/4)

Status 2026-10-13: GetEpisode VERIFIED from the official RapidAPI cURL
(`GET /reelshort/series/{series_id}/episodes/{episode_id}`). Search, series
detail and episode LIST are still NOT configured — no official paths known
and no guessing per policy. The gateway serves local metadata
(`live:false`, explicit "NOT live data" notes) for those until docs arrive.

Still needed (same three options as before — listing URL, playground Code
Snippet, or method+path+params sketch) for:
- `reelShortSearchSeries` — search series by keyword
- `reelShortGetSeries` — series detail by id
- episode list for a series (distinct from single-episode GetEpisode)

## What the owner must provide (any ONE of these)

1. **RapidAPI listing URL** of the subscribed API, e.g.
   `https://rapidapi.com/<owner>/api/<slug>` — the "Endpoints" tab lists the
   exact paths. (The host `reelshort-short-drama-api.p.rapidapi.com` alone
   does not reveal the owner/page.)
2. **Code Snippet** from the RapidAPI playground ("Test Endpoint" → Code
   Snippet → Python/Python-httpx) for each operation:
   - `reelShortSearchSeries` — search series by keyword
   - `reelShortGetSeries` — series detail by id
   - `reelShortGetEpisode` — episode/playback info by series id + episode id
3. Or the provider's own OpenAPI/sketch: HTTP method + full path including
   placeholders + required query params + which field carries the video URL
   and its licence/distribution flag.

## How to apply (no code change needed)

```bash
# in .env — path may contain {id} and {episode_id} placeholders:
RAPIDAPI_REELSHORT_SEARCH_PATH=/v3/search
RAPIDAPI_REELSHORT_DETAIL_PATH=/v3/series/{id}
RAPIDAPI_REELSHORT_EPISODES_PATH=/v3/series/{id}/episodes/{episode_id}
sudo docker compose up -d   # or: sudo systemctl restart api-drama
```

Then in the dashboard (Providers → Test is key-only): call the public API
for series `6a7619d41dcbbd8e12015bfe` episode `0jqdr2ia9b`. A 2xx on the
configured path flips the connection to `endpoint_ok=1` (verified); a 404
marks `endpoint_ok=0` (path wrong). Video is PASS only when upstream returns
a non-empty `video_url` on an asset the licence marks distributable
(`ALLOW_VIDEO_URLS=true` + distributable flag, else still redacted).
