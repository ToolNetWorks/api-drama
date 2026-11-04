#!/usr/bin/env python3
"""Create or reset an owner admin account (Argon2id hash, no plaintext stored).

Usage:
    cd /home/ubuntu/api-drama && .venv/bin/python tools/create_admin.py --username owner
    # prompts for password twice, then inserts/updates admin_users.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.admin_auth import hash_password
from app.store import connect, run_migrations, utcnow


def main() -> int:
    parser = argparse.ArgumentParser(description="Create/reset ToolNet admin account")
    parser.add_argument("--username", required=True, help="Admin username")
    args = parser.parse_args()
    username = args.username.strip()
    if not username:
        print("username must not be empty", file=sys.stderr)
        return 2
    pw1 = getpass.getpass("New password (min 8 chars): ")
    pw2 = getpass.getpass("Repeat password: ")
    if pw1 != pw2:
        print("passwords do not match", file=sys.stderr)
        return 2
    try:
        pw_hash = hash_password(pw1)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    run_migrations()
    with connect() as db:
        db.execute(
            """INSERT INTO admin_users (username, pw_hash, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash""",
            (username, pw_hash, utcnow()),
        )
    print(f"admin '{username}' ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
