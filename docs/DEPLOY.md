# Deploy & operations — api-drama gateway (Docker Compose, since 2026-10-13)

Runtime: `docker compose up -d` (container `api-drama`, image `api-drama:latest`,
loopback-only `127.0.0.1:8090`, user 1000:1000, restart `unless-stopped`,
mem 768 MB / 1 CPU, json-file logs 10 MB x5). Nginx + HTTPS unchanged
(still proxies 127.0.0.1:8090). Legacy systemd unit `api-drama.service`
is DISABLED but kept in `deploy/` for rollback.

## Ops cheatsheet

```bash
cd /home/ubuntu/api-drama
sudo docker compose ps                    # healthy?
sudo docker compose logs --tail=50 api    # logs
sudo docker compose up -d --build         # rebuild after code change
sudo docker compose down && sudo systemctl enable --now api-drama   # ROLLBACK
```

## 0. Pre-deploy (owner)

Status at time of writing: implementation + 29 tests green on an isolated DB;
production SQLite migrated additively (125 series rows intact, no data loss).
DO NOT deploy until the owner confirms sections 1–3.

## 1. Pre-deploy (owner)

- [ ] Set secrets in `/home/ubuntu/api-drama/.env` (never commit):
  `TOOLNET_MASTER_KEY` (Fernet, generate via
  `.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
  `ADMIN_USERNAME` + `ADMIN_PASSWORD` (bootstrap only, min 8 chars).
- [ ] Verify RapidAPI docs and fill `RAPIDAPI_REELSHORT_*_PATH` if live
  upstream is wanted. While empty, `/v1/*` serves the local metadata cache.
- [ ] Decide `ALLOW_VIDEO_URLS` (default `false`). Enable only with a
  distribution licence from the upstream.
- [ ] DNS: `dig +short api-drama.toolnet.tech` currently returns NOTHING
  (NXDOMAIN). Create the A/AAAA record first, then re-check before touching nginx.

## 2. First start (local, port 8090)

```bash
cd /home/ubuntu/api-drama
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8090
```

- Migrations run automatically on startup (001–003, additive).
- Bootstrap admin is created once from `ADMIN_USERNAME/ADMIN_PASSWORD`;
  REMOVE `ADMIN_PASSWORD` from `.env` immediately after first login.
- Log in at `http://127.0.0.1:8090/admin`, add RapidAPI connections under
  Providers, create customer keys under API Keys (full key shown once).

## 3. Production wiring (after DNS resolves)

1. Copy `deploy/api-drama.service` to `/etc/systemd/system/`, `daemon-reload`,
   `enable --now api-drama`. Service binds 127.0.0.1:8090 only.
2. Copy `deploy/logrotate-api-drama` to `/etc/logrotate.d/` (app also rotates
   its own 5 MB files; belt and suspenders).
3. Copy `deploy/nginx-api-drama.toolnet.tech.conf` to sites-available AFTER
   DNS resolves, symlink, `nginx -t`, reload, then certbot
   `--nginx -d api-drama.toolnet.tech`.
4. Set `COOKIE_SECURE=true` in `.env` once HTTPS is live, restart service.

## 4. Rollback

- SQLite backup: `cp data/reelshort.sqlite3 /tmp/reelshort.backup.$(date +%s).sqlite3`
  (one was taken automatically during verification).
- Schema is additive only; code rollback = `git checkout` + restart, no
  down-migration needed. Legacy `/api/*` and `/health` shapes are unchanged.
