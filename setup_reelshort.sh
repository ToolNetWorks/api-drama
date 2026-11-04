#!/usr/bin/env bash
set -euo pipefail
ROOT=/home/ubuntu/api-drama
mkdir -p "$ROOT/app/providers" "$ROOT/data"
touch "$ROOT/app/__init__.py" "$ROOT/app/providers/__init__.py"
cat > "$ROOT/requirements.txt" <<'EOF'
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
httpx>=0.27,<1
beautifulsoup4>=4.12,<5
EOF
cat > "$ROOT/app/providers/reelshort.py" <<'PY'
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup

BASE = "https://www.reelshort.com"

async def discover(locale: str = "en") -> list[dict]:
    if locale not in {"en", "vi"}:
        raise ValueError("locale must be en or vi")
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": "DramaCatalogBot/0.1 (metadata discovery)"}) as client:
        response = await client.get(f"{BASE}/{locale}")
        response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    items = {}
    for link in soup.select('a[href]'):
        href = urljoin(BASE, link.get('href', ''))
        parsed = urlparse(href)
        if parsed.hostname not in {"www.reelshort.com", "reelshort.com"}:
            continue
        if not any(x in parsed.path.lower() for x in ("/movie/", "/book/", "/video/")):
            continue
        title = link.get_text(" ", strip=True)
        image = link.find('img')
        if not title and image:
            title = image.get('alt', '').strip()
        if not title:
            continue
        items[href] = {"title": title[:300], "url": href, "poster": urljoin(BASE, image.get('src', '')) if image and image.get('src') else None, "locale": locale}
    return list(items.values())
PY
cat > "$ROOT/app/main.py" <<'PY'
import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from app.providers.reelshort import discover

DB = Path(os.getenv('DRAMA_DB', '/home/ubuntu/api-drama/data/drama.db'))
LOCK = asyncio.Lock()

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=15)
    db.row_factory = sqlite3.Row
    return db

@asynccontextmanager
async def lifespan(app: FastAPI):
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS series (url TEXT PRIMARY KEY, title TEXT NOT NULL, poster TEXT, locale TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    yield

app = FastAPI(title='Drama API', version='0.1.0', lifespan=lifespan)

@app.get('/health')
def health():
    return {'ok': True, 'provider': 'reelshort'}

@app.post('/api/reelshort/scan')
async def scan(locale: str = Query('en', pattern='^(en|vi)$')):
    if LOCK.locked():
        raise HTTPException(409, 'Scan already running')
    async with LOCK:
        try:
            items = await discover(locale)
        except Exception as exc:
            raise HTTPException(502, f'ReelShort fetch failed: {type(exc).__name__}') from exc
        with connect() as db:
            db.executemany('INSERT INTO series(url,title,poster,locale) VALUES(:url,:title,:poster,:locale) ON CONFLICT(url) DO UPDATE SET title=excluded.title,poster=excluded.poster,updated_at=CURRENT_TIMESTAMP', items)
        return {'discovered': len(items), 'locale': locale, 'note': 'HTML links only; zero results may require site-specific parser adjustments'}

@app.get('/api/reelshort/series')
def series(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    with connect() as db:
        rows = db.execute('SELECT * FROM series ORDER BY updated_at DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    return {'items': [dict(row) for row in rows], 'limit': limit, 'offset': offset}

@app.get('/api/reelshort/stats')
def stats():
    with connect() as db:
        total = db.execute('SELECT COUNT(*) FROM series').fetchone()[0]
    return {'series': total}
PY
cat > "$ROOT/Dockerfile" <<'EOF'
FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /srv/data
ENV DRAMA_DB=/srv/data/drama.db
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
EOF
cd "$ROOT"
python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -m compileall -q app
printf '\nSetup complete: %s\nRun: cd %s && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080\n' "$ROOT" "$ROOT"
