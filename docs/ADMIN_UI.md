# Admin UI (ToolNet Drama)

Owner console at `/admin/*`. Neutral dark system: hamburger + drawer nav,
topbar search, dense charcoal rows, restrained status colors. No gradients,
no decorative emoji. Static HTML shells + shared CSS/JS; all data via the
existing `/admin/api/*` JSON contracts (unchanged).

## Layout

- `app/static/admin/admin.css` — tokens, topbar, drawer, provider cards,
  connection rows, toggle switch, modal, KPI strip, charts, series/episode
  rows, tables, toasts; breakpoints 640 / 900px (mobile-first).
- `app/static/admin/admin.js` — `TN` helper: CSRF fetch, `mountShell(active,
  {search, searchPlaceholder, onSearch})`, toasts, busy buttons, modal
  builder, field/validation helpers.
- `app/admin_pages/providers.html` — default **Provider Directory** (2-col
  grid, 1-col mobile, topbar search filter); click a card opens the
  **Provider Detail** (`#/provider`) with horizontal compact connection rows:
  checkbox, name, status, usage bar, quota, inline error, Test / Details /
  Edit / Key / Clear / Delete, right-side toggle; Select all + Test selected;
  routing bar (strategy / rotation / cooldown); Add Connection opens a modal.
- `app/admin_pages/overview.html` — 4 prioritized KPIs, real per-day stacked
  chart + cache-vs-upstream split (from `/admin/api/stats/overview` and
  `/admin/api/cache/status`), provider health, latest-7-days table, top keys.
  "Recent traffic" is per-day aggregates — the API exposes no per-request
  feed, so no fake activity stream is rendered.
- `app/admin_pages/cache.html` — KPI strip, compact series rows (monogram;
  real poster only in detail header when `series_pic` exists), click opens
  episode rows (number, id, duration, hits, URL status + expiry,
  Refresh/Delete resolved via the entries search — roster rows carry no id).
  Video URLs are never rendered in full.
- `app/admin_pages/keys.html`, `login.html` — same shell/tokens.

## Backend contracts used (read-only list; no new endpoints)

Providers: `GET/POST /{p}/connections`, `PATCH /{id}`, `POST /{id}/key`,
`POST /{id}/enable|disable|clear-error|test`, `DELETE /{id}`,
`PUT /{p}/settings`. Cache: `status`, `series`, `series/{p}/{sid}`,
`entries`, `entries/{id}`, `POST entries/{id}/refresh`,
`DELETE entries/{id}`, `POST series/invalidate`. Stats: `overview`.

## Verification

`tests/` (118) green; Playwright harness `shot_admin.py` renders real pages
against seeded data and saves comparison screenshots (directory, detail,
add-modal, overview, cache, mobile nav/detail).
