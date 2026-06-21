import asyncio
import json
import os
import secrets
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime


def _fmt_exc(e: BaseException) -> str:
    """Format an exception for logging — type + repr + traceback.
    f"{e}" silently logs "" for exceptions with empty str() (timeouts,
    CancelledError, some httpx errors); this never blackholes a failure."""
    return f"{type(e).__name__}: {e!r}\n{traceback.format_exc()}"


# Updated at the end of each successful cycle so /health can flag silent
# stalls (e.g. scheduler wedged on a hung await).
_last_cycle_end: float | None = None

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import ai_suggest
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
_ai_running = False
_index_html: str = ''

_INDEX_PATH = Path(__file__).parent / 'static' / 'index.html'


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler_task, _index_html
    await db.init(DB_PATH)
    # Deezer login is critical — without it every Deezer-sourced album fails.
    # The function returns False (rather than raising) so startup can continue
    # if e.g. Deezer is briefly unreachable, but the failure has to be loud or
    # an expired ARL silently disables half the sync pipeline.
    if not await deezer.login():
        await db.log(
            'error',
            'Deezer login FAILED at startup — check DEEMIX_ARL '
            '(it expires every few months). Deezer downloads will not work '
            'until this is fixed.',
        )
    _index_html = await asyncio.to_thread(_INDEX_PATH.read_text)
    _scheduler_task = asyncio.create_task(_scheduler())
    yield
    _scheduler_task.cancel()


app = FastAPI(title='Aria', lifespan=lifespan)

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if ARIA_API_KEY and request.url.path.startswith("/api/"):
        if request.url.path == "/api/push-token":
            return await call_next(request)
        key = request.headers.get("X-API-Key", "")
        if not secrets.compare_digest(key, ARIA_API_KEY):
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Tight CSP: only same-origin scripts and styles, allow https images
        # for album/artist art from Spotify/Deezer/Tidal/MusicBrainz CDNs.
        # 'unsafe-inline' is needed for style attributes used in the SPA.
        # X-XSS-Protection was dropped — it's deprecated; Chrome removed
        # support in 2019 and modern browsers ignore it.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' https: data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

app.add_middleware(SecurityHeadersMiddleware)

app.mount('/static', StaticFiles(directory='static'), name='static')


_AI_INTERVAL_SECONDS = 7 * 24 * 3600


async def _ai_due() -> bool:
    """Return True if no AI suggestions have been created in the last 7 days."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT created_at FROM suggestions ORDER BY id DESC LIMIT 1"
        )).fetchone()
    if not row:
        return True
    last = datetime.fromisoformat(row[0])
    age = (datetime.now() - last).total_seconds()
    return age >= _AI_INTERVAL_SECONDS


async def _releases_due() -> bool:
    """Same cadence as suggestions — fire weekly."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT created_at FROM releases_feed ORDER BY id DESC LIMIT 1"
        )).fetchone()
    if not row:
        return True
    last = datetime.fromisoformat(row[0])
    return (datetime.now() - last).total_seconds() >= _AI_INTERVAL_SECONDS


_releases_running = False


async def _run_releases_watch():
    """For each monitored artist, fetch their recent Spotify albums and find
    ones not already in the library. AI-filter the result to the 5 most
    interesting, store in releases_feed."""
    global _releases_running
    if _releases_running:
        return
    _releases_running = True
    try:
        async with db.connect() as conn:
            artist_rows = await (await conn.execute(
                'SELECT name, spotify_id FROM artists '
                'WHERE monitored = 1 AND spotify_id IS NOT NULL'
            )).fetchall()
            existing = {(r[0], r[1]) for r in await (await conn.execute(
                'SELECT ar.name, al.title FROM albums al '
                'JOIN artists ar ON ar.id = al.artist_id'
            )).fetchall()}

        # Parallel fetch — these are read-only Spotify calls.
        all_albums = await asyncio.gather(*[
            spotify.get_artist_albums(spotify_id)
            for (_, spotify_id) in artist_rows
        ], return_exceptions=True)

        candidates: list[dict] = []
        cutoff_year = str(datetime.now().year - 1)  # this year + last year only
        for (artist_name, _), albums in zip(artist_rows, all_albums):
            if isinstance(albums, BaseException):
                continue
            for a in albums:
                year = (a.get('year') or '')
                if year < cutoff_year:
                    continue
                if (artist_name, a.get('title', '')) in existing:
                    continue
                candidates.append({
                    'artist': artist_name,
                    'title':  a.get('title', ''),
                    'year':   year,
                    'spotify_id': a.get('spotify_id', ''),
                })

        if not candidates:
            await db.log('info', 'New-release watch: no new candidates')
            return

        picks = await ai_suggest.filter_new_releases(candidates)
        if not picks:
            await db.log('warn', f'New-release watch: AI filter empty (had {len(candidates)} candidates)')
            return

        # Resolve back to spotify_id by matching artist+title.
        by_key = {(c['artist'], c['title']): c for c in candidates}
        async with db.connect() as conn:
            for p in picks:
                cand = by_key.get((p['artist'], p['title']), {})
                await conn.execute(
                    'INSERT INTO releases_feed '
                    '(artist_name, album_title, spotify_id, year, reason) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (p['artist'], p['title'], cand.get('spotify_id', ''),
                     cand.get('year', ''), p['reason']),
                )
            await conn.commit()
        await db.log('info', f'New-release watch: stored {len(picks)} highlights')
    finally:
        _releases_running = False


async def _run_ai_tasks():
    global _ai_running
    if _ai_running:
        return
    _ai_running = True
    try:
        async with db.connect() as conn:
            rows = await (await conn.execute(
                'SELECT name FROM artists WHERE monitored = 1 ORDER BY name'
            )).fetchall()
        names = [r[0] for r in rows]
        if not names:
            return

        suggestions = await ai_suggest.suggest_artists(names)
        if suggestions:
            async with db.connect() as conn:
                await conn.executemany(
                    'INSERT INTO suggestions (artist_name, reason, source_artist) VALUES (?, ?, ?)',
                    [(s['artist_name'], s['reason'], s['source_artist']) for s in suggestions],
                )
                await conn.commit()
            await db.log('info', f'AI suggested {len(suggestions)} artists')

        playlist = await ai_suggest.build_playlist(names)
        if playlist:
            async with db.connect() as conn:
                await conn.execute(
                    'INSERT INTO playlists (name, description, track_list) VALUES (?, ?, ?)',
                    (playlist['name'], playlist['description'], playlist['track_list']),
                )
                await conn.commit()
            await db.log('info', f'AI generated playlist: {playlist["name"]}')
    finally:
        _ai_running = False


async def _scheduler():
    while True:
        try:
            await _run_cycle_once()
        except Exception as e:
            await db.log('error', f'Scheduler error: {_fmt_exc(e)}')
        try:
            if not _ai_running and await _ai_due():
                asyncio.create_task(_task(_run_ai_tasks()))
        except Exception as e:
            await db.log('error', f'AI task check failed: {_fmt_exc(e)}')
        try:
            if not _releases_running and await _releases_due():
                asyncio.create_task(_task(_run_releases_watch()))
        except Exception as e:
            await db.log('error', f'Releases-watch check failed: {_fmt_exc(e)}')
        await asyncio.sleep(INTERVAL)


async def _run_cycle_once():
    global _cycle_running, _last_cycle_end
    if _cycle_running:
        return
    _cycle_running = True
    try:
        await processor.run_cycle()
    finally:
        _cycle_running = False
        _last_cycle_end = time.time()


async def _task(coro):
    try:
        await coro
    except Exception as e:
        await db.log('error', f'Background task failed: {_fmt_exc(e)}')


# ── Artists ──────────────────────────────────────────────────────────────────

class ArtistIn(BaseModel):
    name: str


@app.get('/health')
async def health():
    """Real dependency check — returns 200 when Aria is healthy enough to
    keep processing music, 503 otherwise. Designed for uptime probes —
    fast, bounded, meaningful enough to act on.

    Checks:
      - cycle: stalled if >2 * INTERVAL since last completion
      - db: must be readable
      - ollama: informational (AI suggestions break if unreachable, but the
                music sync keeps working)
    """
    checks: dict[str, dict] = {}
    ok = True
    now = time.time()

    # Cycle freshness — the single most important signal. The 3x multiplier
    # gives headroom for the natural cycle duration on top of the INTERVAL
    # sleep; a tighter threshold false-flags healthy steady-state operation.
    if _last_cycle_end is None:
        checks['cycle'] = {'status': 'warming', 'age_seconds': None}
    else:
        age = now - _last_cycle_end
        stale = age > (3 * INTERVAL)
        checks['cycle'] = {
            'status': 'stale' if stale else 'ok',
            'age_seconds': round(age, 1),
            'interval': INTERVAL,
        }
        if stale:
            ok = False

    # DB readability — if this fails every endpoint is broken anyway, so
    # treating it as fatal is appropriate.
    try:
        async with db.connect() as conn:
            await (await conn.execute('SELECT 1')).fetchone()
        checks['db'] = {'status': 'ok'}
    except Exception as e:
        checks['db'] = {'status': 'fail', 'error': f'{type(e).__name__}: {e!r}'}
        ok = False

    # Ollama probe — informational. The music sync doesn't depend on it,
    # so a down Ollama is just "AI suggestions paused" not "Aria is down".
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f'{ai_suggest.OLLAMA_URL.rsplit("/api", 1)[0]}/api/tags')
        checks['ollama'] = {'status': 'ok' if r.status_code < 500 else 'unreachable'}
    except Exception:
        checks['ollama'] = {'status': 'unreachable'}

    body = {'ok': ok, 'checks': checks, 'ts': now}
    return JSONResponse(body, status_code=200 if ok else 503)


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
    limit = min(limit, 500)
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT level, message, created_at FROM logs ORDER BY id DESC LIMIT ?',
            (limit,)
        )).fetchall()
    return [{'level': r[0], 'message': r[1], 'at': r[2]} for r in rows]


# ── Cycle control ─────────────────────────────────────────────────────────────

@app.post('/api/scan-existing', status_code=200)
async def scan_existing():
    """Walk MUSIC_DIR + match {Artist}/{Album}/ folders against the albums
    table. Marks matched albums as 'complete' so users with a pre-existing
    library aren't shown 0/N. Synchronous — fast even on large libraries
    (filesystem walk + DB updates, no network).

    Returns counts so the caller can show a result like "matched 2 albums
    across 6 artists (15 unmatched dirs)"."""
    result = await processor.scan_existing_library()
    if 'error' in result:
        raise HTTPException(400, result['error'])
    # If anything changed, trigger a Plex scan so the library shows the
    # newly-marked-complete content immediately.
    if result['matched_albums'] > 0:
        asyncio.create_task(_task(processor.plex.scan_music_library()))
    return result


@app.post('/api/cycle/run', status_code=202)
async def trigger_cycle():
    asyncio.create_task(_task(_run_cycle_once()))
    return {'queued': True}


# ── Push notifications ────────────────────────────────────────────────────────

class PushTokenIn(BaseModel):
    token: str


@app.post('/api/push-token', status_code=204)
async def register_push_token(body: PushTokenIn):
    token = body.token.strip()
    if not token:
        raise HTTPException(400, 'token required')
    async with db.connect() as conn:
        await conn.execute(
            'INSERT INTO push_tokens(token) VALUES(?) ON CONFLICT(token) DO NOTHING',
            (token,))
        await conn.commit()


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

    # Fetch related artists for all seeds in parallel — was 5 sequential
    # HTTP calls (~2-5s); now ~1 round-trip. The early "break at 24" exit
    # is gone (we always do all 5 calls), but the savings on the slow path
    # outweigh occasionally fetching a few extra results.
    related_lists = await asyncio.gather(
        *[spotify.get_related_artists(spotify_id) for (spotify_id,) in seed_rows],
        return_exceptions=True,
    )

    seen: set[str] = set()
    results = []
    for related in related_lists:
        if isinstance(related, BaseException):
            continue
        for artist in related:
            rid = artist['spotify_id']
            if rid not in known and rid not in seen:
                seen.add(rid)
                results.append(artist)
        if len(results) >= 24:
            break
    return results[:24]


# ── AI Suggestions & Playlists ────────────────────────────────────────────────

@app.get('/api/ai-suggestions')
async def list_ai_suggestions():
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT id, artist_name, reason, source_artist, created_at '
            'FROM suggestions WHERE dismissed = 0 ORDER BY id DESC'
        )).fetchall()
    return [{'id': r[0], 'artist_name': r[1], 'reason': r[2],
             'source_artist': r[3], 'created_at': r[4]} for r in rows]


@app.delete('/api/ai-suggestions/{suggestion_id}', status_code=204)
async def dismiss_ai_suggestion(suggestion_id: int):
    async with db.connect() as conn:
        await conn.execute('UPDATE suggestions SET dismissed = 1 WHERE id = ?', (suggestion_id,))
        await conn.commit()


@app.get('/api/ai-playlists')
async def list_ai_playlists():
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT id, name, description, track_list, created_at FROM playlists ORDER BY id DESC'
        )).fetchall()
    return [{'id': r[0], 'name': r[1], 'description': r[2],
             'tracks': json.loads(r[3] or '[]'), 'created_at': r[4]} for r in rows]


@app.post('/api/ai-playlists/generate', status_code=202)
async def generate_ai_playlist():
    asyncio.create_task(_task(_run_ai_tasks()))
    return {'queued': True}


@app.delete('/api/ai-playlists/{playlist_id}', status_code=204)
async def delete_ai_playlist(playlist_id: int):
    async with db.connect() as conn:
        await conn.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        await conn.commit()


class MoodIn(BaseModel):
    mood: str


@app.post('/api/ai-playlists/mood', status_code=202)
async def generate_mood_playlist(body: MoodIn):
    """Free-text mood/theme → custom playlist. Returns immediately; result
    lands in the playlists table once GLM-4 responds (~5-30s)."""
    mood = (body.mood or '').strip()
    if not mood:
        raise HTTPException(400, 'mood is required')

    async def _run_mood():
        async with db.connect() as conn:
            rows = await (await conn.execute(
                'SELECT name FROM artists WHERE monitored = 1'
            )).fetchall()
        names = [r[0] for r in rows]
        playlist = await ai_suggest.build_mood_playlist(names, mood)
        if not playlist:
            await db.log('warn', f'AI mood playlist failed: {mood!r}')
            return
        async with db.connect() as conn:
            await conn.execute(
                'INSERT INTO playlists (name, description, track_list) VALUES (?, ?, ?)',
                (playlist['name'], playlist['description'], playlist['track_list']),
            )
            await conn.commit()
        await db.log('info', f'AI mood playlist: {playlist["name"]!r}')

    asyncio.create_task(_task(_run_mood()))
    return {'queued': True, 'mood': mood}


@app.get('/api/ai-digest')
async def ai_digest():
    """Returns a fresh narrative about the library. Synchronous — caller
    waits for GLM-4 (~5-15s). Cache on the client if needed."""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT name FROM artists WHERE monitored = 1'
        )).fetchall()
    names = [r[0] for r in rows]
    narrative = await ai_suggest.library_digest(names)
    if not narrative:
        return {'digest': None, 'error': 'AI unavailable'}
    return {'digest': narrative}


class LyricSearchIn(BaseModel):
    query: str


@app.post('/api/ai-lyric-search')
async def ai_lyric_search(body: LyricSearchIn):
    """Free-text → up to 10 track suggestions with reasons."""
    q = (body.query or '').strip()
    if not q:
        raise HTTPException(400, 'query is required')
    results = await ai_suggest.lyric_search(q)
    return {'query': q, 'results': results}


@app.get('/api/ai-releases')
async def list_ai_releases():
    """AI-filtered new releases from monitored artists (last 12 months)."""
    async with db.connect() as conn:
        rows = await (await conn.execute(
            'SELECT id, artist_name, album_title, spotify_id, year, reason, created_at '
            'FROM releases_feed WHERE dismissed = 0 ORDER BY id DESC'
        )).fetchall()
    return [{
        'id': r[0], 'artist_name': r[1], 'album_title': r[2],
        'spotify_id': r[3], 'year': r[4], 'reason': r[5], 'created_at': r[6],
    } for r in rows]


@app.delete('/api/ai-releases/{release_id}', status_code=204)
async def dismiss_ai_release(release_id: int):
    async with db.connect() as conn:
        await conn.execute('UPDATE releases_feed SET dismissed = 1 WHERE id = ?', (release_id,))
        await conn.commit()


@app.post('/api/ai-releases/refresh', status_code=202)
async def refresh_ai_releases():
    """Manual trigger for the new-release watch — runs the same task the
    weekly scheduler does. Useful from the UI for an on-demand refresh."""
    asyncio.create_task(_task(_run_releases_watch()))
    return {'queued': True}


@app.post('/api/artists/{artist_id}/auto-genres')
async def artist_auto_genres(artist_id: int):
    """AI-inferred canonical genre tags for an artist. Returns the list; does
    NOT persist (no genres column yet — surface to caller for filtering UI)."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT name FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
    if not row:
        raise HTTPException(404, 'artist not found')
    tags = await ai_suggest.auto_genres(row[0])
    return {'artist': row[0], 'genres': tags}


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get('/')
async def index():
    injection = f'<script>window._apiKey={json.dumps(ARIA_API_KEY)};</script>'
    return HTMLResponse(_index_html.replace('</head>', injection + '</head>', 1))
