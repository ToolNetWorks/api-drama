-- 006: ReelShort series metadata cache (moved from local SQLite to Layerbase PostgreSQL)
-- Columns match the local SQLite schema used by rapidapi_reelshort.py scan

CREATE TABLE IF NOT EXISTS series (
    book_id TEXT PRIMARY KEY,
    t_book_id TEXT,
    title TEXT,
    poster TEXT,
    description TEXT,
    chapter_count INTEGER,
    paid_start INTEGER,
    tags TEXT,
    shelves TEXT,
    first_chapter_id TEXT,
    read_count INTEGER,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_series_title ON series(title);
CREATE INDEX IF NOT EXISTS idx_series_updated ON series(updated_at);

-- Trigger to auto-update updated_at on INSERT/UPDATE (PostgreSQL specific)
-- For SQLite, this is handled by the application