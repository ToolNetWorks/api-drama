#!/usr/bin/env python3
"""Migrate data from local SQLite to Layerbase PostgreSQL.

Step 1: Run migrations on Layerbase to create tables
Step 2: Copy data from local SQLite to Layerbase
"""
from __future__ import annotations

import os
import sys
import sqlite3
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.config import load_settings
from app.core.crypto import decrypt_key, encrypt_key

# Load settings
settings = load_settings()

# Local SQLite
LOCAL_DB = Path(__file__).resolve().parent / "data" / "reelshort.sqlite3"

TABLES_TO_MIGRATE = [
    "series",
    "admin_users",
    "api_keys",
    "provider_connections",
    "provider_settings",
    "request_logs",
    "schema_migrations",
]


def run_migrations_on_pg():
    """Run SQL migrations on PostgreSQL."""
    print("📦 Running migrations on Layerbase...")
    
    import psycopg2
    import psycopg2.extras
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(settings.database_url)
    query = parse_qs(parsed.query)
    for k in list(query.keys()):
        if k not in ("sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout", "application_name"):
            del query[k]
    
    clean_query = "&".join(f"{k}={v[0]}" for k, v in query.items())
    clean_dsn = f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    if clean_query:
        clean_dsn += f"?{clean_query}"

    pg = psycopg2.connect(clean_dsn)
    pg.autocommit = True
    pg_cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Read and execute migration files
    from app.store import MIGRATIONS_DIR
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    
    for path in files:
        version = path.stem
        print(f"   Applying {version}...")
        sql = path.read_text(encoding="utf-8")
        try:
            pg_cur.execute(sql)
        except Exception as e:
            print(f"   ⚠️ {version}: {e}")
    
    # Ensure legacy series table
    pg_cur.execute("""
        CREATE TABLE IF NOT EXISTS series (
            book_id TEXT PRIMARY KEY, t_book_id TEXT, title TEXT NOT NULL,
            poster TEXT, description TEXT, chapter_count INTEGER,
            paid_start INTEGER, tags TEXT NOT NULL, shelves TEXT NOT NULL,
            first_chapter_id TEXT, read_count INTEGER, updated_at TEXT NOT NULL
        )
    """)
    
    pg.close()
    print("   ✅ Migrations applied")


def migrate_data():
    """Copy data from local SQLite to Layerbase."""
    print(f"\n🔄 Migrating data from {LOCAL_DB} to Layerbase...")
    print(f"   Target: {settings.database_url[:60]}...")

    local = sqlite3.connect(str(LOCAL_DB))
    local.row_factory = sqlite3.Row

    import psycopg2
    import psycopg2.extras
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(settings.database_url)
    query = parse_qs(parsed.query)
    for k in list(query.keys()):
        if k not in ("sslmode", "sslcert", "sslkey", "sslrootcert", "connect_timeout", "application_name"):
            del query[k]
    
    clean_query = "&".join(f"{k}={v[0]}" for k, v in query.items())
    clean_dsn = f"postgresql://{parsed.username}:{parsed.password}@{parsed.hostname}:{parsed.port or 5432}{parsed.path}"
    if clean_query:
        clean_dsn += f"?{clean_query}"

    pg = psycopg2.connect(clean_dsn)
    pg.autocommit = True
    pg_cur = pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    try:
        for table in TABLES_TO_MIGRATE:
            print(f"\n📋 Migrating table: {table}")
            
            rows = local.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                print(f"   (empty)")
                continue

            cols = list(rows[0].keys())
            
            # Get column info from PostgreSQL
            pg_cur.execute(f"""
                SELECT column_name FROM information_schema.columns 
                WHERE table_name = '{table}'
            """)
            pg_cols = {r['column_name'] for r in pg_cur.fetchall()}
            
            common_cols = [c for c in cols if c in pg_cols]
            if not common_cols:
                print(f"   ⚠️ No common columns, skipping")
                continue

            placeholders = ", ".join(["%s"] * len(common_cols))
            col_names = ", ".join(common_cols)
            
            conflict_clause = ""
            if table == "admin_users" and "username" in common_cols:
                conflict_clause = " ON CONFLICT (username) DO NOTHING"
            elif table == "provider_connections" and "id" in common_cols:
                conflict_clause = " ON CONFLICT (id) DO NOTHING"
            elif table == "provider_settings" and "provider" in common_cols:
                conflict_clause = " ON CONFLICT (provider) DO NOTHING"
            elif table == "schema_migrations" and "version" in common_cols:
                conflict_clause = " ON CONFLICT (version) DO NOTHING"
            elif table == "api_keys" and "key_hash" in common_cols:
                conflict_clause = " ON CONFLICT (key_hash) DO NOTHING"

            inserted = 0
            for row in rows:
                values = [row[c] for c in common_cols]
                try:
                    pg_cur.execute(
                        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}){conflict_clause}",
                        values
                    )
                    inserted += 1
                except Exception as e:
                    print(f"   ⚠️ Row failed: {e}")

            print(f"   ✅ {inserted}/{len(rows)} rows inserted")

        print("\n✅ Data migration complete!")
        print("\n📝 Next steps:")
        print("   1. Start the app: .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8090")
        print("   2. Go to Admin UI → Providers → Re-enter RapidAPI keys (encrypted with new master key)")
        print("   3. Remove ADMIN_PASSWORD from .env after first login")

    finally:
        local.close()
        pg.close()


if __name__ == "__main__":
    run_migrations_on_pg()
    migrate_data()