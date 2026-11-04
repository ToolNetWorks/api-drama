#!/usr/bin/env python
"""Turso episode-cache operator tool (never prints secrets).

  python tools/turso_migrate.py --check      # connectivity + schema version, NO writes
  python tools/turso_migrate.py --migrate    # apply pending migrations (idempotent)

Reads TURSO_DATABASE_URL / TURSO_AUTH_TOKEN from the environment / .env. Exits 2 when
credentials are missing (and says exactly which), 1 on connection errors.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.episode_cache import repo, schema  # noqa: E402
from app.episode_cache.client import scrub  # noqa: E402
from app.episode_cache.config import load_cache_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--migrate", action="store_true")
    args = ap.parse_args()
    cfg = load_cache_config()
    if not cfg.configured:
        print("MISSING ENV: " + ", ".join(cfg.missing()))
        return 2
    import libsql

    host = urlsplit(cfg.database_url).hostname
    t0 = time.monotonic()
    try:
        conn = libsql.connect(cfg.database_url, auth_token=cfg.auth_token,
                              timeout=cfg.timeout_s, autocommit=True)
        ver = repo.schema_version(conn)
        lat = (time.monotonic() - t0) * 1000
        print(f"connected host={host} latency_ms={lat:.0f} schema_version={ver} "
              f"latest={schema.LATEST}")
        if args.migrate:
            applied = schema.migrate(conn)
            print(f"migrations applied: {applied or 'none (already up to date)'}; "
                  f"version now {repo.schema_version(conn)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED host={host}: {type(exc).__name__}: {scrub(str(exc), (cfg.auth_token,))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
