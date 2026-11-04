"""Isolated test env: temp SQLite DB, ephemeral master key. Runs app lifespan."""
from __future__ import annotations

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="tn_test_")
os.environ["DB_PATH"] = os.path.join(_tmp, "test.sqlite3")
os.environ["LOG_FILE"] = os.path.join(_tmp, "test.log")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("TOOLNET_MASTER_KEY", Fernet.generate_key().decode())
# Tests must NEVER reach a real Turso database, whatever the developer's .env says.
os.environ["EPISODE_CACHE_ENABLED"] = "false"
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["TURSO_AUTH_TOKEN"] = ""
os.environ["ADMIN_SESSION_TTL_H"] = "1"
os.environ["COOKIE_SECURE"] = "false"
os.environ["RAPIDAPI_REELSHORT_SEARCH_PATH"] = ""
os.environ["RAPIDAPI_REELSHORT_DETAIL_PATH"] = ""
os.environ["RAPIDAPI_REELSHORT_EPISODES_PATH"] = ""
os.environ["ALLOW_VIDEO_URLS"] = "false"
os.environ["REQUIRE_DISTRIBUTABLE_FLAG"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main_module  # noqa: E402
from app.core.admin_auth import hash_password  # noqa: E402
from app.core.rate_limit import limiter  # noqa: E402
from app.store import connect, utcnow  # noqa: E402

ADMIN_USER = "owner"
ADMIN_PASS = "correct-horse-9"


def _seed_series() -> None:
    with connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO series
               (book_id, t_book_id, title, poster, description, chapter_count,
                paid_start, tags, shelves, first_chapter_id, read_count, updated_at)
               VALUES ('book1', 't1', 'Alpha Love Story', 'p1', 'A sweet drama',
                       10, NULL, '[]', '[]', 'ch1', 5, ?)""",
            (utcnow(),),
        )
        db.execute(
            """INSERT OR IGNORE INTO series
               (book_id, t_book_id, title, poster, description, chapter_count,
                paid_start, tags, shelves, first_chapter_id, read_count, updated_at)
               VALUES ('book2', 't2', 'Beta Revenge', 'p2', 'A dark drama',
                       20, NULL, '[]', '[]', 'ch9', 7, ?)""",
            (utcnow(),),
        )


def _seed_admin() -> None:
    with connect() as db:
        db.execute(
            """INSERT OR IGNORE INTO admin_users (username, pw_hash, created_at)
               VALUES (?, ?, ?)""",
            (ADMIN_USER, hash_password(ADMIN_PASS), utcnow()),
        )


@pytest.fixture(scope="session")
def client():
    with TestClient(main_module.app) as c:
        _seed_series()
        _seed_admin()
        yield c


@pytest.fixture(autouse=True)
def _reset_limiter():
    limiter.reset()
    yield
    limiter.reset()
