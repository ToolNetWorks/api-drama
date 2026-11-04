import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.store import connect as store_connect

BASE_URL = "https://www.reelshort.com/"


def connect():
    """Use main store connection (Layerbase PostgreSQL)."""
    return store_connect()


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
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
    return {"scanned": len(movies), "database": "layerbase-postgresql"}


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
