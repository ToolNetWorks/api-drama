#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/api-drama
mkdir -p app/providers data
[ -f app/main.py ] && cp app/main.py "app/main.py.bak.$(date +%s)"
cat > app/providers/reelshort.py <<'PY'
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

BASE_URL = "https://www.reelshort.com/"
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "reelshort.sqlite3"


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS series (
        book_id TEXT PRIMARY KEY, t_book_id TEXT, title TEXT NOT NULL,
        poster TEXT, description TEXT, chapter_count INTEGER,
        paid_start INTEGER, tags TEXT NOT NULL, shelves TEXT NOT NULL,
        first_chapter_id TEXT, read_count INTEGER, updated_at TEXT NOT NULL
    )""")
    return db


def parse_homepage(html):
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#__NEXT_DATA__")
    if node is None or not node.string:
        raise ValueError("ReelShort NEXT_DATA missing")
    payload = json.loads(node.string)
    fallback = payload.get("props", {}).get("pageProps", {}).get("fallback", {})
    hall = fallback.get("/api/ms/hall/webInfo")
    if not isinstance(hall, dict):
        raise ValueError("ReelShort hall webInfo missing")
    merged = {}

    def add(book, shelf):
        book_id = book.get("book_id")
        if not isinstance(book_id, str) or not re.fullmatch(r"[a-zA-Z0-9]+", book_id):
            return
        title = book.get("book_title")
        if not isinstance(title, str) or not title.strip():
            return
        tags = [t.get("tag_name") for t in book.get("theme_list", []) if isinstance(t, dict) and t.get("tag_name")]
        if book_id not in merged:
            merged[book_id] = {
                "book_id": book_id, "t_book_id": book.get("t_book_id"),
                "title": title.strip(), "poster": book.get("book_pic") or book.get("default_pic"),
                "description": book.get("special_desc"), "chapter_count": book.get("chapter_count"),
                "paid_start": book.get("paid_start"), "tags": tags, "shelves": [],
                "first_chapter_id": (book.get("start_play") or {}).get("chapter_id") or book.get("chapter_id"),
                "read_count": book.get("read_count"),
            }
        entry = merged[book_id]
        if shelf not in entry["shelves"]:
            entry["shelves"].append(shelf)
        for key in ("poster", "description", "chapter_count", "paid_start", "first_chapter_id", "read_count"):
            if entry.get(key) is None:
                entry[key] = {"poster": book.get("book_pic") or book.get("default_pic"),
                    "first_chapter_id": (book.get("start_play") or {}).get("chapter_id") or book.get("chapter_id")}.get(key, book.get(key))
        entry["tags"] = sorted(set(entry["tags"] + tags))

    for banner in hall.get("banners", []):
        if isinstance(banner, dict):
            add(banner.get("jump_param") or {}, "Banners")
    for shelf in hall.get("bookShelfList", []):
        if not isinstance(shelf, dict):
            continue
        shelf_name = shelf.get("bookshelf_name") or f"Shelf {shelf.get('bs_id', 'unknown')}"
        for book in shelf.get("books", []):
            if isinstance(book, dict):
                add(book, shelf_name)
    return list(merged.values())


async def scan_reelshort():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; DramaMetadataIndexer/1.0)"}) as client:
        response = await client.get(BASE_URL)
        response.raise_for_status()
    movies = parse_homepage(response.text)
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        for m in movies:
            db.execute("""INSERT INTO series
            (book_id,t_book_id,title,poster,description,chapter_count,paid_start,tags,shelves,first_chapter_id,read_count,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(book_id) DO UPDATE SET
            t_book_id=COALESCE(excluded.t_book_id,series.t_book_id), title=excluded.title,
            poster=COALESCE(excluded.poster,series.poster), description=COALESCE(excluded.description,series.description),
            chapter_count=COALESCE(excluded.chapter_count,series.chapter_count),
            paid_start=COALESCE(excluded.paid_start,series.paid_start),
            tags=excluded.tags, shelves=excluded.shelves,
            first_chapter_id=COALESCE(excluded.first_chapter_id,series.first_chapter_id),
            read_count=COALESCE(excluded.read_count,series.read_count), updated_at=excluded.updated_at""",
            (m["book_id"],m["t_book_id"],m["title"],m["poster"],m["description"],
             m["chapter_count"],m["paid_start"],json.dumps(m["tags"]),json.dumps(m["shelves"]),
             m["first_chapter_id"],m["read_count"],now))
    return {"scanned": len(movies), "database": str(DB_PATH)}


def row_to_dict(row):
    if row is None:
        return None
    item = dict(row)
    for key in ("tags", "shelves"):
        item[key] = json.loads(item[key])
    return item


def list_series(limit=30, offset=0):
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM series").fetchone()[0]
        rows = db.execute("SELECT * FROM series ORDER BY title LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "items": [row_to_dict(r) for r in rows]}


def get_series(book_id):
    with connect() as db:
        row = db.execute("SELECT * FROM series WHERE book_id=?", (book_id,)).fetchone()
    return row_to_dict(row)
PY
cat > app/main.py <<'PY'
import logging
from fastapi import FastAPI, HTTPException, Query
from app.providers.reelshort import scan_reelshort, list_series, get_series

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("api-drama")
app = FastAPI(title="ToolNet Drama API", version="1.1.0")

@app.get("/health")
def health():
    return {"ok": True, "service": "api-drama", "version": "1.1.0"}

@app.post("/api/reelshort/scan")
async def scan():
    try:
        return await scan_reelshort()
    except (ValueError, Exception) as exc:
        logger.exception("ReelShort scan failed")
        raise HTTPException(502, "ReelShort scan failed; check server logs") from exc

@app.get("/api/reelshort/series")
def series(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return list_series(limit, offset)

@app.get("/api/reelshort/series/{book_id}")
def detail(book_id: str):
    movie = get_series(book_id)
    if movie is None:
        raise HTTPException(404, "Series not found")
    return movie
PY
.venv/bin/python -m compileall -q app
.venv/bin/python -c 'from app.main import app; print("Import OK")'
echo 'Ready. Restart uvicorn, then POST /api/reelshort/scan'
