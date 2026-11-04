import os
import tempfile
import sys

_tmp = tempfile.mkdtemp(prefix='tn_test_')
os.environ['DB_PATH'] = os.path.join(_tmp, 'test.sqlite3')
os.environ['LOG_FILE'] = os.path.join(_tmp, 'test.log')
os.environ['TOOLNET_MASTER_KEY'] = 'test_key'
os.environ['EPISODE_CACHE_ENABLED'] = 'false'
os.environ['DATABASE_URL'] = ''
os.environ['TURSO_DATABASE_URL'] = ''
os.environ['TURSO_AUTH_TOKEN'] = ''
os.environ['ADMIN_SESSION_TTL_H'] = '1'
os.environ['COOKIE_SECURE'] = 'false'
os.environ['RAPIDAPI_REELSHORT_SEARCH_PATH'] = ''
os.environ['RAPIDAPI_REELSHORT_DETAIL_PATH'] = ''
os.environ['RAPIDAPI_REELSHORT_EPISODES_PATH'] = ''
os.environ['ALLOW_VIDEO_URLS'] = 'false'
os.environ['REQUIRE_DISTRIBUTABLE_FLAG'] = 'true'

import app.main as main_module
from fastapi.testclient import TestClient
from app.store import connect, utcnow

print('App created, testing client...', file=sys.stderr)
with TestClient(main_module.app) as c:
    print('Client created', file=sys.stderr)
    db = connect()
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print('Tables:', [t['name'] for t in tables], file=sys.stderr)
    
    db.execute(
        '''INSERT OR IGNORE INTO series
           (book_id, t_book_id, title, poster, description, chapter_count,
            paid_start, tags, shelves, first_chapter_id, read_count, updated_at)
           VALUES ('book1', 't1', 'Alpha Love Story', 'p1', 'A sweet drama',
                   10, NULL, '[]', '[]', 'ch1', 5, ?)''',
        (utcnow(),),
    )
    print('Seed OK', file=sys.stderr)