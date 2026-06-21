import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime

DB_PATH = None


async def init(path: str):
    global DB_PATH
    DB_PATH = path
    async with aiosqlite.connect(path) as db:
        await db.executescript('''
            CREATE TABLE IF NOT EXISTS artists (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                deezer_id   TEXT,
                monitored   INTEGER DEFAULT 1,
                added_at    TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS albums (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
                title       TEXT    NOT NULL,
                year        TEXT,
                deezer_id   TEXT    UNIQUE,
                track_count INTEGER DEFAULT 0,
                status      TEXT    DEFAULT 'missing',
                error       TEXT,
                source      TEXT,
                updated_at  TEXT    DEFAULT (datetime('now')),
                UNIQUE(artist_id, title)
            );

            CREATE TABLE IF NOT EXISTS logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                level       TEXT NOT NULL,
                message     TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_albums_status     ON albums(status);
            CREATE INDEX IF NOT EXISTS idx_albums_artist_id  ON albums(artist_id);
            CREATE INDEX IF NOT EXISTS idx_logs_created      ON logs(created_at);
        ''')
        await db.commit()
        await _migrate(db)
        await db.execute("UPDATE albums SET status = 'missing' WHERE status = 'downloading'")
        await db.commit()


async def _migrate(conn):
    for sql in [
        'ALTER TABLE artists ADD COLUMN mb_id TEXT',
        'ALTER TABLE artists ADD COLUMN image_url TEXT',
        'ALTER TABLE artists ADD COLUMN spotify_id TEXT',
        'ALTER TABLE albums ADD COLUMN cover_url TEXT',
        'ALTER TABLE albums ADD COLUMN wanted INTEGER NOT NULL DEFAULT 1',
        'ALTER TABLE albums ADD COLUMN record_type TEXT NOT NULL DEFAULT \'album\'',
        'ALTER TABLE albums ADD COLUMN spotify_id TEXT',
        '''CREATE TABLE IF NOT EXISTS suggestions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name   TEXT    NOT NULL,
            reason        TEXT,
            source_artist TEXT,
            dismissed     INTEGER DEFAULT 0,
            created_at    TEXT    DEFAULT (datetime('now'))
        )''',
        '''CREATE TABLE IF NOT EXISTS playlists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            description TEXT,
            track_list  TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        )''',
        '''CREATE TABLE IF NOT EXISTS push_tokens (
            token       TEXT PRIMARY KEY,
            created_at  TEXT DEFAULT (datetime('now'))
        )''',
        'ALTER TABLE albums ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0',
    ]:
        try:
            await conn.execute(sql)
        except Exception:
            pass
    await conn.commit()


@asynccontextmanager
async def connect():
    async with aiosqlite.connect(DB_PATH) as conn:
        # WAL lets readers and writers proceed without blocking each other —
        # critical when the cycle is writing 100s of album updates while the
        # web UI is reading via /api/items + /api/stats. busy_timeout makes
        # waiters wait 5s instead of failing with SQLITE_BUSY.
        await conn.execute('PRAGMA journal_mode=WAL')
        await conn.execute('PRAGMA busy_timeout=5000')
        await conn.execute('PRAGMA foreign_keys = ON')
        yield conn


async def log(level: str, message: str):
    async with connect() as db:
        await db.execute(
            'INSERT INTO logs (level, message, created_at) VALUES (?, ?, ?)',
            (level, message, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        await db.execute(
            'DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 1000)'
        )
        await db.commit()
