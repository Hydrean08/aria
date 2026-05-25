import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

import db
import processor
from sources import deezer, spotiflac, spotify
from tagger import safe_name as _safe_name

_MUSIC_DIR = os.getenv('MUSIC_DIR', '/music')

DB_PATH      = os.getenv('DB_PATH', '/data/aria.db')
INTERVAL     = int(os.getenv('INTERVAL', '3600'))
ARIA_API_KEY = os.getenv('ARIA_API_KEY', '')

_scheduler_task: asyncio.Task | None = None
_cycle_running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task
    await db.init(DB_PATH)
    await deezer.login()
    _scheduler_task = asyncio.create_task(_scheduler())
    yield
    _scheduler_task.cancel()


app = FastAPI(title='Aria', lifespan=lifespan)

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if ARIA_API_KEY and request.url.path.startswith("/api/"):
        if request.headers.get("X-API-Key") != ARIA_API_KEY:
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)

app.mount('/static', StaticFiles(directory='static'), name='static')


async def _scheduler():
    while True:
        try:
            await _run_cycle_once()
        except Exception as e:
            await db.log('error', f'Scheduler error: {e}')
        await asyncio.sleep(INTERVAL)


async def _run_cycle_once():
    global _cycle_running
    if _cycle_running:
        return
    _cycle_running = True
    try:
        await processor.run_cycle()
    finally:
        _cycle_running = False


async def _task(coro):
    try:
        await coro
    except Exception as e:
        await db.log('error', f'Background task failed: {e}')


# ── Artists ──────────────────────────────────────────────────────────────────

class ArtistIn(BaseModel):
    name: str


@app.get('/api/artists')
async def list_artists():
    async with db.connect() as conn:
        rows = await (await conn.execute('''
            SELECT a.id, a.name, a.deezer_id, a.monitored, a.added_at, a.mb_id, a.image_url,
                   COUNT(al.id) AS album_total,
                   SUM(CASE WHEN al.status = 'complete'    THEN 1 ELSE 0 END) AS album_done,
                   SUM(CASE WHEN al.status = 'missing'     THEN 1 ELSE 0 END) AS album_missing,
                   SUM(CASE WHEN al.status = 'error'       THEN 1 ELSE 0 END) AS album_error,
                   SUM(CASE WHEN al.status = 'downloading' THEN 1 ELSE 0 END) AS album_downloading,
                   SUM(CASE WHEN al.status = 'partial'     THEN 1 ELSE 0 END) AS album_partial
            FROM artists a
            LEFT JOIN albums al ON al.artist_id = a.id
            GROUP BY a.id
            ORDER BY a.name
        ''')).fetchall()
    return [{'id': r[0], 'name': r[1], 'deezer_id': r[2], 'monitored': bool(r[3]),
             'added_at': r[4], 'mb_id': r[5], 'image_url': r[6],
             'album_total': r[7], 'album_done': r[8],
             'album_missing': r[9], 'album_error': r[10],
             'album_downloading': r[11], 'album_partial': r[12]}
            for r in rows]


@app.post('/api/artists', status_code=201)
async def add_artist(body: ArtistIn):
    artist = await deezer.search_artist(body.name)
    deezer_id = str(artist['id']) if artist else None
    resolved_name = artist['name'] if artist else body.name
    image_url = artist.get('picture_medium') if artist else None

    async with db.connect() as conn:
        try:
            cur = await conn.execute(
                'INSERT INTO artists (name, deezer_id, image_url) VALUES (?, ?, ?)',
                (resolved_name, deezer_id, image_url)
            )
            artist_id = cur.lastrowid
            await conn.commit()
        except Exception:
            raise HTTPException(409, 'Artist already exists')

    asyncio.create_task(_task(processor.sync_artist(resolved_name, deezer_id)))

    return {'id': artist_id, 'name': resolved_name, 'deezer_id': deezer_id, 'image_url': image_url}


@app.post('/api/artists/{artist_id}/sync', status_code=202)
async def sync_artist(artist_id: int):
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT name, deezer_id FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
    if not row:
        raise HTTPException(404, 'Artist not found')
    asyncio.create_task(_task(processor.sync_artist(row[0], row[1])))
    return {'queued': True}


@app.delete('/api/artists/{artist_id}', status_code=204)
async def remove_artist(artist_id: int):
    async with db.connect() as conn:
        await conn.execute('DELETE FROM artists WHERE id = ?', (artist_id,))
        await conn.commit()


@app.patch('/api/artists/{artist_id}/monitor')
async def set_monitored(artist_id: int, monitored: bool):
    async with db.connect() as conn:
        await conn.execute('UPDATE artists SET monitored = ? WHERE id = ?', (int(monitored), artist_id))
        await conn.commit()
    return {'monitored': monitored}


# ── Albums ────────────────────────────────────────────────────────────────────

@app.get('/api/albums')
async def list_albums_by_status(status: str = ''):
    async with db.connect() as conn:
        if status:
            rows = await (await conn.execute(
                '''SELECT al.id, al.title, al.year, al.cover_url, al.status, al.error,
                          al.record_type, ar.id, ar.name, ar.image_url
                   FROM albums al JOIN artists ar ON ar.id = al.artist_id
                   WHERE al.status = ?
                   ORDER BY ar.name, al.year, al.title''',
                (status,)
            )).fetchall()
        else:
            rows = await (await conn.execute(
                '''SELECT al.id, al.title, al.year, al.cover_url, al.status, al.error,
                          al.record_type, ar.id, ar.name, ar.image_url
                   FROM albums al JOIN artists ar ON ar.id = al.artist_id
                   ORDER BY ar.name, al.year, al.title'''
            )).fetchall()
    return [{'id': r[0], 'title': r[1], 'year': r[2], 'cover_url': r[3],
             'status': r[4], 'error': r[5], 'record_type': r[6] or 'album',
             'artist_id': r[7], 'artist_name': r[8], 'artist_image': r[9]}
            for r in rows]


class AlbumIn(BaseModel):
    title: str
    year: str = ''


@app.get('/api/artists/{artist_id}/albums')
async def list_albums(artist_id: int):
    async with db.connect() as conn:
        rows = await (await conn.execute(
            '''SELECT id, title, year, deezer_id, track_count, status, error, source, updated_at, cover_url, wanted, record_type
               FROM albums WHERE artist_id = ? ORDER BY year, title''',
            (artist_id,)
        )).fetchall()
    return [{'id': r[0], 'title': r[1], 'year': r[2], 'deezer_id': r[3],
             'track_count': r[4], 'status': r[5], 'error': r[6],
             'source': r[7], 'updated_at': r[8], 'cover_url': r[9],
             'wanted': bool(r[10]), 'record_type': r[11] or 'album'}
            for r in rows]


@app.post('/api/artists/{artist_id}/albums', status_code=201)
async def add_album(artist_id: int, body: AlbumIn):
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT name FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
        if not row:
            raise HTTPException(404, 'Artist not found')
        try:
            cur = await conn.execute(
                'INSERT INTO albums (artist_id, title, year) VALUES (?, ?, ?)',
                (artist_id, body.title, body.year)
            )
            album_id = cur.lastrowid
            await conn.commit()
        except Exception:
            raise HTTPException(409, 'Album already exists')
    return {'id': album_id, 'title': body.title, 'year': body.year, 'status': 'missing'}


@app.get('/api/albums/{album_id}/tracks')
async def album_tracks(album_id: int):
    async with db.connect() as conn:
        row = await (await conn.execute(
            '''SELECT al.deezer_id, al.title, ar.name
               FROM albums al JOIN artists ar ON ar.id = al.artist_id
               WHERE al.id = ?''', (album_id,)
        )).fetchone()
    if not row:
        return []
    deezer_id = row[0]
    if not deezer_id:
        deezer_id = await deezer.search_album(row[2], row[1])
        if deezer_id:
            async with db.connect() as conn:
                await conn.execute('UPDATE albums SET deezer_id = ? WHERE id = ?', (deezer_id, album_id))
                await conn.commit()
    if not deezer_id:
        return []
    return await deezer.get_album_tracks(deezer_id)


@app.patch('/api/albums/{album_id}/wanted')
async def set_album_wanted(album_id: int, wanted: bool):
    async with db.connect() as conn:
        await conn.execute('UPDATE albums SET wanted = ? WHERE id = ?', (int(wanted), album_id))
        await conn.commit()
    return {'wanted': wanted}


@app.patch('/api/artists/{artist_id}/albums/wanted')
async def set_all_albums_wanted(artist_id: int, wanted: bool):
    async with db.connect() as conn:
        await conn.execute('UPDATE albums SET wanted = ? WHERE artist_id = ?', (int(wanted), artist_id))
        await conn.commit()
    return {'wanted': wanted}


@app.post('/api/albums/{album_id}/retry', status_code=202)
async def retry_album_endpoint(album_id: int):
    async with db.connect() as conn:
        await conn.execute(
            "UPDATE albums SET status = 'missing', error = NULL WHERE id = ?",
            (album_id,)
        )
        await conn.commit()
    asyncio.create_task(_task(processor.retry_album(album_id)))
    return {'queued': True}


# ── Dashboard stats ────────────────────────────────────────────────────────────

@app.get('/api/stats')
async def stats():
    async with db.connect() as conn:
        row = await (await conn.execute('''
            SELECT
                COUNT(*) FILTER (WHERE status = 'missing' AND wanted = 1
                    AND artist_id IN (SELECT id FROM artists WHERE monitored = 1)) AS pending,
                COUNT(*) FILTER (WHERE status = 'downloading') AS downloading,
                COUNT(*) FILTER (WHERE status = 'partial')    AS partial,
                COUNT(*) FILTER (WHERE status = 'complete')   AS complete,
                COUNT(*) FILTER (WHERE status = 'error')      AS error
            FROM albums
        ''')).fetchone()
    return {
        'pending': row[0], 'downloading': row[1],
        'partial': row[2], 'complete': row[3], 'error': row[4],
        'cycle_running': _cycle_running,
    }


# ── Logs ──────────────────────────────────────────────────────────────────────

@app.get('/api/logs')
async def get_logs(limit: int = 200):
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT level, message, created_at FROM logs ORDER BY id DESC LIMIT ?',
            (limit,)
        )).fetchall()
    return [{'level': r[0], 'message': r[1], 'at': r[2]} for r in rows]


# ── Cycle control ─────────────────────────────────────────────────────────────

@app.post('/api/cycle/run', status_code=202)
async def trigger_cycle():
    asyncio.create_task(_task(_run_cycle_once()))
    return {'queued': True}


# ── Charts / Recent / Top Tracks ──────────────────────────────────────────────

@app.get('/api/charts')
async def charts():
    return await deezer.get_charts()


_GENRE_SLUGS = [
    ('pop',       'Pop'),
    ('hip hop',   'Rap / Hip-Hop'),
    ('rock',      'Rock'),
    ('r&b',       'R&B'),
    ('dance',     'Dance'),
    ('country',   'Country'),
    ('christian', 'Christian'),
    ('soul',      'Soul'),
    ('folk',      'Folk'),
    ('jazz',      'Jazz'),
    ('reggae',    'Reggae'),
    ('latin',     'Latin'),
]

@app.get('/api/charts/genres')
async def genre_charts():
    async def fetch(slug, label):
        artists = await spotify.genre_artists(slug, 20)
        return {'genre': label, 'artists': artists} if artists else None
    results = await asyncio.gather(*[fetch(s, l) for s, l in _GENRE_SLUGS])
    return [r for r in results if r is not None]


@app.get('/api/recent')
async def recent():
    async with db.connect() as conn:
        rows = await (await conn.execute('''
            SELECT al.title, al.cover_url, al.year, ar.name, al.id, ar.id
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            WHERE al.status = 'complete'
            ORDER BY al.updated_at DESC LIMIT 8
        ''')).fetchall()
    return [{'title': r[0], 'cover_url': r[1], 'year': r[2],
             'artist': r[3], 'album_id': r[4], 'artist_id': r[5]}
            for r in rows]


@app.get('/api/artists/{artist_id}/top-tracks')
async def artist_top_tracks(artist_id: int):
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT spotify_id, deezer_id FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
    if not row:
        return []
    if row[0]:
        return await spotify.get_top_tracks(row[0])
    if row[1]:
        return await deezer.get_top_tracks(row[1])
    return []


@app.get('/api/artists/{artist_id}/related')
async def artist_related(artist_id: int):
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT spotify_id, deezer_id FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
    if not row:
        return []
    if row[0]:
        return await spotify.get_related_artists(row[0])
    if row[1]:
        return await deezer.get_related_artists(row[1])
    return []


@app.get('/api/spotify/{spotify_id}/top-tracks')
async def spotify_top_tracks(spotify_id: str):
    return await spotify.get_top_tracks(spotify_id)


@app.get('/api/spotify/{spotify_id}/related')
async def spotify_related(spotify_id: str):
    return await spotify.get_related_artists(spotify_id)


@app.get('/api/spotify/{spotify_id}/albums')
async def spotify_albums(spotify_id: str):
    return await spotify.get_artist_albums(spotify_id)


@app.get('/api/spotify/album/{spotify_album_id}/tracks')
async def spotify_album_tracks(spotify_album_id: str):
    return await spotify.get_album_tracks(spotify_album_id)


class TrackIn(BaseModel):
    track_id: str
    title: str
    artist: str
    album: str
    track_num: int = 1
    year: str = ''


@app.post('/api/tracks/download', status_code=202)
async def download_single_track(body: TrackIn):
    dest = os.path.join(_MUSIC_DIR, _safe_name(body.artist), _safe_name(body.album))
    await db.log('info', f'Queueing track: {body.artist} — {body.title}')
    if body.track_id.isdigit():
        asyncio.create_task(_task(deezer.download_track(
            body.track_id, dest, body.title, body.artist, body.album, body.track_num, body.year,
        )))
    else:
        asyncio.create_task(_task(spotiflac.download_track_spotify(body.track_id, dest)))
    return {'queued': True}


# ── Discovery ─────────────────────────────────────────────────────────────────

@app.get('/api/search/artists')
async def search_artists(q: str = ''):
    if not q.strip():
        return []
    return await spotify.search_artists(q.strip())


@app.get('/api/discover')
async def discover():
    async with db.connect() as conn:
        known = {r[0] for r in await (await conn.execute(
            'SELECT spotify_id FROM artists WHERE spotify_id IS NOT NULL'
        )).fetchall()}
        seed_rows = await (await conn.execute(
            'SELECT spotify_id FROM artists WHERE monitored = 1 AND spotify_id IS NOT NULL ORDER BY RANDOM() LIMIT 5'
        )).fetchall()

    seen: set[str] = set()
    results = []
    for (spotify_id,) in seed_rows:
        for artist in await spotify.get_related_artists(spotify_id):
            rid = artist['spotify_id']
            if rid not in known and rid not in seen:
                seen.add(rid)
                results.append(artist)
        if len(results) >= 24:
            break
    return results[:24]


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get('/')
async def index():
    html = Path('static/index.html').read_text()
    injection = f'<script>window._apiKey="{ARIA_API_KEY}";</script>'
    return HTMLResponse(html.replace('</head>', injection + '</head>', 1))
