import asyncio
import json
import os
import re
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
from sources import deezer, navidrome, spotiflac, spotify
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
    # First-run library import: if the user has 0 'complete' albums but the
    # DB is populated, they almost certainly have an existing collection on
    # disk that Aria hasn't matched. Run the scan once in the background so
    # the album counts populate immediately on first cycle. Subsequent boots
    # short-circuit (the count > 0 check) so this stays a one-shot.
    asyncio.create_task(_task(_first_run_import()))
    _scheduler_task = asyncio.create_task(_scheduler())
    yield
    _scheduler_task.cancel()


async def _first_run_import():
    """Auto-run the existing-library scan at startup when at least one
    monitored artist has zero complete albums. The previous global-zero
    check missed cases where a user has some Aria-downloaded artists and
    some pre-existing ones (the common situation as the library grows).

    The scan itself is idempotent — only flips 'missing' → 'complete' for
    on-disk matches — so re-running is harmless. We gate it on an "at least
    one artist needs help" signal to avoid hitting the filesystem on every
    boot when nothing has changed."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            "SELECT COUNT(DISTINCT ar.id) "
            "FROM artists ar "
            "LEFT JOIN albums al ON al.artist_id = ar.id AND al.status = 'complete' "
            "WHERE ar.monitored = 1 AND al.id IS NULL"
        )).fetchone()
    artists_with_no_downloads = row[0] or 0
    if artists_with_no_downloads == 0:
        return
    await db.log(
        'info',
        f'First-run library scan starting '
        f'({artists_with_no_downloads} monitored artists have no complete albums)',
    )
    try:
        result = await processor.scan_existing_library()
        if result.get('matched_albums', 0) > 0:
            asyncio.create_task(_task(processor.plex.scan_music_library()))
    except Exception as e:
        await db.log('error', f'First-run scan failed: {_fmt_exc(e)}')


app = FastAPI(title='Aria', lifespan=lifespan)

# Simple in-memory per-bucket sliding-window rate limiter (no external dep).
_rate_buckets: dict = {}

def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")

def _rate_limited(bucket: str, limit: int, window: float) -> bool:
    now = time.time()
    q = [t for t in _rate_buckets.get(bucket, []) if now - t < window]
    if len(q) >= limit:
        _rate_buckets[bucket] = q
        return True
    q.append(now)
    _rate_buckets[bucket] = q
    return False

# /api/auth (login) and /api/push-token (mobile registration) are reachable
# without the API key; both do their own validation + rate limiting below.
_OPEN_API_PATHS = ("/api/auth", "/api/push-token")

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    if ARIA_API_KEY and request.url.path.startswith("/api/"):
        if request.url.path in _OPEN_API_PATHS:
            return await call_next(request)
        # Accept the key via header (API/mobile clients) or session cookie (browser).
        provided = request.headers.get("X-API-Key", "") or request.cookies.get("aria_session", "")
        if not secrets.compare_digest(provided, ARIA_API_KEY):
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


async def _grounded_playlist(names: list[str], mood: str | None = None,
                             discovery: bool = False) -> dict | None:
    """Build a playlist from tracks the listener OWNS: the AI picks a theme + owned
    artists, Navidrome supplies the real tracks, the AI curates the final order.
    Every track carries its Navidrome id, so 'Add to Navidrome' is instant and
    complete. Falls back to the discovery generator when discovery is requested,
    Navidrome isn't configured, or the library can't fill the theme."""
    async def _discovery():
        return await (ai_suggest.build_mood_playlist(names, mood) if mood
                      else ai_suggest.build_playlist(names))

    if discovery or not navidrome.is_configured():
        return await _discovery()
    owned = await navidrome.library_artist_names()
    universe = owned or names
    if not universe:
        return None
    plan = await ai_suggest.plan_playlist(universe, mood)
    if not plan:
        return None
    pool = await navidrome.library_tracks_for_artists(plan['artists'])
    if not pool:
        pool = await navidrome.library_tracks_for_artists(universe[:25])
    if not pool:
        return await _discovery()   # nothing owned to draw from — better a playlist than none
    chosen = await ai_suggest.curate_from_pool(plan['name'], plan['description'], pool, 12)
    if not chosen:
        return None
    tracks = [{'artist': t['artist'], 'title': t['title'], 'nav_id': t['id']} for t in chosen]
    return {'name': plan['name'], 'description': plan['description'],
            'track_list': json.dumps(tracks)}


async def _run_ai_tasks(discovery: bool = False):
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

        playlist = await _grounded_playlist(names, discovery=discovery)
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
                   SUM(CASE WHEN al.status = 'partial'     THEN 1 ELSE 0 END) AS album_partial,
                   SUM(CASE WHEN al.record_type = 'album'  AND al.is_variant = 0 THEN 1 ELSE 0 END) AS n_albums,
                   SUM(CASE WHEN al.record_type = 'ep'     AND al.is_variant = 0 THEN 1 ELSE 0 END) AS n_eps,
                   SUM(CASE WHEN al.record_type = 'single' AND al.is_variant = 0 THEN 1 ELSE 0 END) AS n_singles,
                   SUM(CASE WHEN al.is_variant = 1 THEN 1 ELSE 0 END) AS n_variants
            FROM artists a
            LEFT JOIN albums al ON al.artist_id = a.id
            GROUP BY a.id
            ORDER BY a.name
        ''')).fetchall()
    return [{'id': r[0], 'name': r[1], 'deezer_id': r[2], 'monitored': bool(r[3]),
             'added_at': r[4], 'mb_id': r[5], 'image_url': r[6],
             'album_total': r[7], 'album_done': r[8],
             'album_missing': r[9], 'album_error': r[10],
             'album_downloading': r[11], 'album_partial': r[12],
             'n_albums': r[13], 'n_eps': r[14], 'n_singles': r[15],
             'n_variants': r[16]}
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


@app.delete('/api/artists/{artist_id}', status_code=200)
async def remove_artist(artist_id: int, purge: bool = False):
    """Remove an artist from the library. With ?purge=true, ALSO delete their
    downloaded files from disk (otherwise the DB rows go but the audio is left
    orphaned on disk). Album titles are read before the cascade delete so the
    purge knows which folders to remove."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT name FROM artists WHERE id = ?', (artist_id,)
        )).fetchone()
        titles = []
        if purge and row:
            trows = await (await conn.execute(
                'SELECT title FROM albums WHERE artist_id = ?', (artist_id,)
            )).fetchall()
            titles = [t[0] for t in trows]
        await conn.execute('DELETE FROM artists WHERE id = ?', (artist_id,))
        await conn.commit()
    purged = None
    if purge and row:
        purged = await processor.purge_artist_files(row[0], titles)
        await db.log('info', f'Removed artist + purged disk: {row[0]} '
                             f'({purged["deleted_files"]} files, '
                             f'{purged["albums_removed"]} albums)')
    return {'removed': True, 'purged': purged}


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
        if status == 'pending':
            # 'pending' is a pseudo-status: the actual download queue, matching
            # the Pending stat count (missing + wanted + monitored). Filtering
            # raw status='missing' would also surface unwanted/unmonitored
            # albums the user will never fetch, so the count and list disagreed.
            rows = await (await conn.execute(
                '''SELECT al.id, al.title, al.year, al.cover_url, al.status, al.error,
                          al.record_type, ar.id, ar.name, ar.image_url
                   FROM albums al JOIN artists ar ON ar.id = al.artist_id
                   WHERE al.status = 'missing' AND al.wanted = 1 AND ar.monitored = 1
                   ORDER BY ar.name, al.year, al.title'''
            )).fetchall()
        elif status:
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
            '''SELECT id, title, year, deezer_id, track_count, status, error, source, updated_at, cover_url, wanted, record_type, is_variant
               FROM albums WHERE artist_id = ? ORDER BY year, title''',
            (artist_id,)
        )).fetchall()
    return [{'id': r[0], 'title': r[1], 'year': r[2], 'deezer_id': r[3],
             'track_count': r[4], 'status': r[5], 'error': r[6],
             'source': r[7], 'updated_at': r[8], 'cover_url': r[9],
             'wanted': bool(r[10]), 'record_type': r[11] or 'album',
             'is_variant': bool(r[12])}
            for r in rows]


@app.post('/api/artists/{artist_id}/albums', status_code=201)
async def add_album(artist_id: int, body: AlbumIn):
    # Reject empty / whitespace-only titles at the boundary. Besides being
    # meaningless, such a title collapses to an empty on-disk folder name, which
    # the file-management guards must never let resolve to the artist root.
    if not body.title.strip():
        raise HTTPException(400, 'Album title is required')
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


# ── On-disk library management (Phase 1) ──────────────────────────────────────
# Paths are always derived server-side from the album's DB artist/album via
# processor's MUSIC_DIR-confined helpers — the client only ever sends an
# album_id (and, for single-file delete, a bare filename validated server-side).

async def _album_disk_ref(album_id: int):
    """(artist_name, album_title, folder) for an album id, or None.

    `folder` is the stored on-disk path, or None when the derived canonical path
    should be used. Critically, a folder SHARED by more than one album row (e.g.
    a "Discography (5 Releases)" dump directory that holds several albums' tracks
    in one dir) is deliberately nulled out here: it must never become a
    whole-folder delete/list target, because rmtree-ing it — or listing/deleting
    a bare filename in it — would hit sibling albums' audio. Such albums fall
    back to the derived per-album path (absent for dumps, so they simply show as
    not-on-disk) until the library is organized into clean per-album folders."""
    async with db.connect() as conn:
        row = await (await conn.execute(
            '''SELECT ar.name, al.title, al.folder FROM albums al
               JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?''',
            (album_id,)
        )).fetchone()
        if not row:
            return None
        folder = row[2]
        if folder:
            shared = await (await conn.execute(
                'SELECT count(*) FROM albums WHERE folder = ?', (folder,)
            )).fetchone()
            if shared[0] > 1:
                folder = None
    return (row[0], row[1], folder)


@app.get('/api/albums/{album_id}/files')
async def album_files(album_id: int):
    """What is actually on disk for this album — files, format, bitrate, size."""
    ref = await _album_disk_ref(album_id)
    if not ref:
        raise HTTPException(404, 'Album not found')
    return await processor.list_album_files(*ref)


@app.delete('/api/albums/{album_id}/files', status_code=200)
async def delete_album_files_endpoint(album_id: int):
    """Delete an album's downloaded files from disk. The DB row stays as
    'missing' so it can be re-downloaded."""
    ref = await _album_disk_ref(album_id)
    if not ref:
        raise HTTPException(404, 'Album not found')
    result = await processor.delete_album_files(*ref)
    async with db.connect() as conn:
        await conn.execute("UPDATE albums SET status = 'missing' WHERE id = ?", (album_id,))
        await conn.commit()
    if result.get('existed'):
        await db.log('info', f'Deleted from disk: {ref[0]} — {ref[1]} '
                             f'({result["deleted_files"]} files)')
    return result


class DeleteFileIn(BaseModel):
    filename: str


@app.post('/api/albums/{album_id}/files/delete', status_code=200)
async def delete_album_track_file(album_id: int, body: DeleteFileIn):
    """Delete a single audio file from an album's folder. `filename` is a bare
    name (no path) validated server-side against traversal."""
    ref = await _album_disk_ref(album_id)
    if not ref:
        raise HTTPException(404, 'Album not found')
    ok = await processor.delete_track_file(ref[0], ref[1], body.filename, ref[2])
    if not ok:
        raise HTTPException(400, 'File not found or invalid filename')
    await db.log('info', f'Deleted file: {ref[0]} — {ref[1]} / {body.filename}')
    return {'deleted': body.filename}


# ── Library ingestion (make Aria's DB reflect what's on disk) ──────────────────

@app.get('/api/library/ingest/preview')
async def library_ingest_preview():
    """Dry run: what a full import WOULD add/reconcile. No DB writes."""
    import ingest
    r = await asyncio.to_thread(ingest.analyze)
    if 'error' in r:
        raise HTTPException(500, r['error'])
    return {
        'total_files': r['total_files'],
        'tagged_files': r['tagged_files'],
        'unclassified': len(r['unclassified']),
        'albums_on_disk': r['group_count'],
        'artists_on_disk': r['disk_artist_count'],
        'db_artists': r['db_artist_count'],
        'new_artists': len(r['new_artist_names']),
        'new_artist_albums': len(r['new_artist_albums']),
        'new_albums_for_known_artists': len(r['known_artist_new_album']),
        'already_known': len(r['already_known']),
    }


@app.post('/api/library/ingest')
async def library_ingest():
    """Import the on-disk library: add artist/album rows for everything found,
    mark owned albums complete. ADDS rows only — never moves or deletes audio.
    Reversible via /api/library/ingest/undo."""
    import ingest
    result = await asyncio.to_thread(ingest.commit)
    if 'error' in result:
        raise HTTPException(500, result['error'])
    await db.log('info',
                 f'Library import: +{result["artists_created"]} artists, '
                 f'+{result["albums_created"]} albums, '
                 f'{result["albums_reconciled"]} reconciled')
    return result


@app.post('/api/library/ingest/undo')
async def library_ingest_undo():
    """Undo a disk import: delete imported albums + prune the artists the import
    created. Never touches audio or artists you added yourself."""
    import ingest
    result = await asyncio.to_thread(ingest.undo)
    await db.log('info',
                 f'Library import undone: -{result["albums_deleted"]} albums, '
                 f'-{result["artists_pruned"]} artists')
    return result


# ── Auto-tagger (fingerprint-based, preview-first, reversible) ─────────────────

async def _album_dir_ctx(album_id: int):
    """Resolved single-album on-disk dir + names, or None. Inherits the
    shared-folder safety from _album_disk_ref (a dump folder resolves to the
    derived, absent path so the auto-tagger can't retag a sibling album)."""
    ref = await _album_disk_ref(album_id)
    if not ref:
        return None
    return {'artist': ref[0], 'album': ref[1],
            'dir': processor._resolve_album_dir(ref[0], ref[1], ref[2])}


@app.get('/api/albums/{album_id}/tagfix/preview')
async def tagfix_preview(album_id: int):
    """READ ONLY: fingerprint each track and propose corrected artist/title."""
    ctx = await _album_dir_ctx(album_id)
    if not ctx:
        raise HTTPException(404, 'Album not found')
    import tagfix
    return await tagfix.preview(ctx['dir'], ctx['artist'], ctx['album'])


class TagFixIn(BaseModel):
    items: list


@app.post('/api/albums/{album_id}/tagfix/apply')
async def tagfix_apply(album_id: int, body: TagFixIn):
    """Apply the approved corrections. Backs up original tags first (reversible)."""
    ctx = await _album_dir_ctx(album_id)
    if not ctx:
        raise HTTPException(404, 'Album not found')
    import tagfix
    result = await tagfix.apply(ctx['dir'], body.items, ctx['artist'], ctx['album'])
    await db.log('info', f'Tag fix applied: {ctx["artist"]} — {ctx["album"]} '
                         f'({result.get("applied", 0)} files)')
    return result


@app.post('/api/albums/{album_id}/tagfix/undo')
async def tagfix_undo(album_id: int):
    """Restore original tags for this album from the pre-fix backups."""
    ctx = await _album_dir_ctx(album_id)
    if not ctx:
        raise HTTPException(404, 'Album not found')
    import tagfix
    result = await tagfix.undo(ctx['dir'])
    await db.log('info', f'Tag fix undone: {ctx["artist"]} — {ctx["album"]} '
                         f'({result.get("restored", 0)} files)')
    return result


@app.post('/api/library/enrich-imported', status_code=202)
async def enrich_imported():
    """Background: fill in imported artists' photos + full discographies from the
    catalog (confident name match required — skips soundtrack/junk 'artists').
    Synced albums are wanted=0, so nothing auto-downloads."""
    asyncio.create_task(_task(processor.enrich_all_imported()))
    return {'queued': True}


@app.get('/api/library/cleanup/preview')
async def cleanup_preview():
    """READ ONLY: what the stray-file rescue would tag/relocate. No writes."""
    import cleanup
    p = await cleanup.plan()
    return {
        'total': p['total'], 'actionable': p['actionable'],
        'unresolvable': p['unresolvable'],
        'sample': [{'path': i['path'], 'artist': i['artist'], 'title': i['title'],
                    'in_root': i['in_root'], 'score': i['score']}
                   for i in p['items'][:50]],
    }


@app.post('/api/library/cleanup', status_code=200)
async def cleanup_apply():
    """Tag stray files (originals backed up) and relocate root-level strays into
    the artist's folder. Reversible via /undo."""
    import cleanup
    result = await cleanup.apply()
    await db.log('info', f'Stray cleanup: tagged {result["tagged"]}, moved {result["moved"]}')
    return result


@app.post('/api/library/cleanup/undo', status_code=200)
async def cleanup_undo():
    """Reverse the stray cleanup: move files back + restore original tags."""
    import cleanup
    result = await cleanup.undo()
    await db.log('info', f'Stray cleanup undone: moved back {result["moved_back"]}, '
                         f'tags restored {result["tags_restored"]}')
    return result


@app.get('/api/library/fix-artists/preview')
async def fix_artists_preview():
    """READ ONLY: dump/collab artists that would be split to a real primary."""
    import fixartists
    p = await fixartists.plan()
    return {'candidates': p['candidates'], 'actionable': p['actionable'],
            'sample': [{'from': i['old_name'], 'to': i['candidate'],
                        'deezer': i['deezer'], 'files': i['files']}
                       for i in p['items'] if i['verified'] and i['files']][:50]}


@app.post('/api/library/fix-artists', status_code=200)
async def fix_artists():
    """Retag dump/collab artists to their real primary, regroup them (re-ingest),
    remove the now-stale dump artist rows, and enrich the real artists.
    Reversible via /cleanup/undo (shared tag_backups + file_moves)."""
    import fixartists
    import ingest
    result = await fixartists.apply()
    if result['files_fixed']:
        await asyncio.to_thread(ingest.commit)   # regroup under real artists
        async with db.connect() as conn:
            for aid in result['fixed_ids']:
                await conn.execute('DELETE FROM artists WHERE id = ?', (aid,))
            await conn.commit()
        asyncio.create_task(_task(processor.enrich_all_imported()))
    await db.log('info', f'Fix-artists: split {result["artists_fixed"]} artists, '
                         f'retagged {result["files_fixed"]} files')
    return result


# ── Arion app OTA (serve the mobile APK for in-app self-update) ────────────────
# These live OUTSIDE /api, so the api-key middleware doesn't gate them — the phone
# downloads the APK via a plain browser link that can't carry the key, and the
# Arion build is public anyway.

_APK_DIR = os.getenv('APK_DIR', '/apk')


def _latest_arion_apk():
    try:
        files = [f for f in os.listdir(_APK_DIR)
                 if f.startswith('arion-') and f.endswith('.apk')]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(_APK_DIR, f)), reverse=True)
        return files[0] if files else None
    except Exception:
        return None


@app.get('/app/version')
async def app_version():
    """Latest published Arion APK + its download path (for the app's OTA check)."""
    latest = _latest_arion_apk()
    return {'apkLatest': latest, 'apkUrl': f'/app/apk/{latest}' if latest else None}


@app.get('/app/apk/{filename}')
async def app_apk(filename: str):
    """Serve a published Arion APK. Whitelisted filename blocks path traversal."""
    if not re.fullmatch(r'arion-[A-Za-z0-9._-]+\.apk', filename):
        raise HTTPException(404, 'Not found')
    path = os.path.join(_APK_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, 'Not found')
    return FileResponse(path, media_type='application/vnd.android.package-archive',
                        filename=filename)


@app.patch('/api/artists/{artist_id}/albums/wanted')
async def set_all_albums_wanted(artist_id: int, wanted: bool, types: str = ''):
    """Bulk set wanted for an artist. Optional `types` (comma-separated
    record_types, e.g. 'album,ep,single') scopes it — used by the Download
    Discography picker. Scoped selection targets primary releases only
    (is_variant = 0), matching the per-type counts shown in the UI."""
    requested = [t.strip() for t in types.split(',') if t.strip() in ('album', 'ep', 'single')]
    async with db.connect() as conn:
        if requested:
            placeholders = ','.join('?' for _ in requested)
            cur = await conn.execute(
                f'''UPDATE albums SET wanted = ?
                    WHERE artist_id = ? AND is_variant = 0 AND record_type IN ({placeholders})''',
                (int(wanted), artist_id, *requested)
            )
        else:
            cur = await conn.execute('UPDATE albums SET wanted = ? WHERE artist_id = ?', (int(wanted), artist_id))
        affected = cur.rowcount
        await conn.commit()
    return {'wanted': wanted, 'types': requested, 'updated': affected}


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


@app.get('/api/downloads')
async def get_downloads(limit: int = 100):
    """Recent download activity for the Arion Downloads view. Ordered newest
    first; active jobs (queued/downloading) naturally sort to the top since
    they're the most recent. `active` is a convenience count for a tab badge."""
    limit = min(limit, 300)
    async with db.connect() as conn:
        rows = await (await conn.execute(
            '''SELECT id, kind, artist, album, title, source, state, error, updated_at
               FROM downloads ORDER BY id DESC LIMIT ?''',
            (limit,)
        )).fetchall()
    items = [{
        'id': r[0], 'kind': r[1], 'artist': r[2], 'album': r[3], 'title': r[4],
        'source': r[5], 'state': r[6], 'error': r[7], 'at': r[8],
    } for r in rows]
    # Count over the whole table, not just the returned page — counting the page
    # under-reports the badge as soon as there are more rows than `limit`.
    async with db.connect() as conn:
        active = (await (await conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE state IN ('queued', 'downloading')"
        )).fetchone())[0]
    return {'active': active, 'items': items}


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


_EXPO_TOKEN_RE = re.compile(r'^(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]+\]$')


@app.post('/api/push-token', status_code=204)
async def register_push_token(body: PushTokenIn, request: Request):
    # Unauthenticated endpoint: strictly validate + rate-limit so it can't be
    # used to flood the table or inject arbitrary strings into push delivery.
    token = body.token.strip()
    if not _EXPO_TOKEN_RE.match(token) or len(token) > 200:
        raise HTTPException(400, 'invalid push token')
    if _rate_limited('push:' + _client_ip(request), 10, 60):
        raise HTTPException(429, 'rate limited')
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


async def _run_track_download(download_id: int, body: 'TrackIn'):
    """Resolve the best source for a single track and download it, recording
    lifecycle state in the downloads feed so Arion can show live progress.

    Order: Deezer id (reliable) → Spotify id resolved to Deezer via ISRC →
    SpotiFLAC via the Spotify URL (last resort — flaky, no status of its own)."""
    dest = os.path.join(_MUSIC_DIR, _safe_name(body.artist), _safe_name(body.album))
    await db.download_update(download_id, 'downloading')

    deezer_id = body.track_id if body.track_id.isdigit() else None
    if not deezer_id:
        isrc = await spotify.get_track_isrc(body.track_id)
        if isrc:
            deezer_id = await deezer.track_by_isrc(isrc)

    if deezer_id:
        path = await deezer.download_track(
            deezer_id, dest, body.title, body.artist, body.album, body.track_num, body.year,
        )
        if path:
            await db.download_update(download_id, 'done', source='deezer')
            return

    if not body.track_id.isdigit():
        files = await spotiflac.download_track_spotify(body.track_id, dest)
        if files:
            await db.download_update(download_id, 'done', source='spotiflac')
            return

    await db.download_update(download_id, 'failed', error='No source could fetch this track')
    await db.log('warn', f'Track download failed: {body.artist} — {body.title}')


@app.post('/api/tracks/download', status_code=202)
async def download_single_track(body: TrackIn):
    download_id = await db.download_create('track', body.artist, body.album, body.title)
    await db.log('info', f'Queueing track: {body.artist} — {body.title}')

    async def _job():
        try:
            await _run_track_download(download_id, body)
        except Exception as e:
            await db.download_update(download_id, 'failed', error=_fmt_exc(e))
            raise

    asyncio.create_task(_task(_job()))
    return {'queued': True, 'download_id': download_id}


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
async def generate_ai_playlist(discovery: bool = False):
    """Grounded by default (playlist built from tracks you own). Pass
    ?discovery=true for the old suggest-anything behavior."""
    asyncio.create_task(_task(_run_ai_tasks(discovery=discovery)))
    return {'queued': True}


@app.get('/api/navidrome/status')
async def navidrome_status():
    """So the app can hide the export button when Navidrome isn't set up."""
    return {'configured': navidrome.is_configured()}


@app.post('/api/ai-playlists/{playlist_id}/export-navidrome')
async def export_ai_playlist_to_navidrome(playlist_id: int):
    """Match an AI playlist's tracks against the Navidrome library and create the
    playlist there. Returns a match report (matched N of M + the misses)."""
    if not navidrome.is_configured():
        raise HTTPException(400, 'Navidrome is not configured on the server')
    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT name, track_list FROM playlists WHERE id = ?', (playlist_id,)
        )).fetchone()
    if not row:
        raise HTTPException(404, 'playlist not found')
    tracks = json.loads(row[1] or '[]')
    if not tracks:
        raise HTTPException(400, 'playlist has no tracks')
    try:
        result = await navidrome.export_tracks(row[0], tracks)
    except Exception as e:
        await db.log('warn', f'Navidrome export failed for playlist {playlist_id}: {e}')
        raise HTTPException(502, f'Navidrome export failed: {e}')
    await db.log('info', f"Navidrome: added {result['matched']}/{result['total']} "
                         f"tracks to playlist {row[0]!r}")
    return result


@app.delete('/api/ai-playlists/{playlist_id}', status_code=204)
async def delete_ai_playlist(playlist_id: int):
    async with db.connect() as conn:
        await conn.execute('DELETE FROM playlists WHERE id = ?', (playlist_id,))
        await conn.commit()


class MoodIn(BaseModel):
    mood: str
    discovery: bool = False


@app.post('/api/ai-playlists/mood', status_code=202)
async def generate_mood_playlist(body: MoodIn):
    """Free-text mood/theme → custom playlist. Grounded in your library by default
    (set discovery=true to let it suggest anything). Returns immediately; result
    lands in the playlists table once GLM-4 responds (~5-30s)."""
    mood = (body.mood or '').strip()
    if not mood:
        raise HTTPException(400, 'mood is required')
    discovery = bool(body.discovery)

    async def _run_mood():
        async with db.connect() as conn:
            rows = await (await conn.execute(
                'SELECT name FROM artists WHERE monitored = 1'
            )).fetchall()
        names = [r[0] for r in rows]
        playlist = await _grounded_playlist(names, mood, discovery=discovery)
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

_LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Aria — Sign in</title>
<style>body{background:#0d0d0f;color:#eee;font-family:system-ui,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#17171b;padding:32px;border-radius:14px;box-shadow:0 10px 40px rgba(0,0,0,.5);width:300px}
h1{font-size:18px;margin:0 0 16px}input{width:100%;box-sizing:border-box;padding:10px;border-radius:8px;border:1px solid #333;background:#0d0d0f;color:#eee;margin-bottom:12px}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#1db954;color:#fff;font-weight:600;cursor:pointer}.err{color:#f55;font-size:13px;min-height:18px}</style></head>
<body><form id="login-form"><h1>Aria</h1><input id="k" type="password" placeholder="API key" autofocus>
<div class="err" id="e"></div><button>Sign in</button></form>
<script src="/static/login.js"></script>
</body></html>"""


@app.post('/api/auth')
async def auth_login(request: Request):
    """Validate the API key and set an HttpOnly session cookie (browser login)."""
    if not ARIA_API_KEY:
        return JSONResponse({'ok': True})
    if _rate_limited('auth:' + _client_ip(request), 5, 60):
        raise HTTPException(429, 'Too many attempts — try again shortly')
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, 'Invalid JSON')
    if not secrets.compare_digest(str(body.get('key', '')), ARIA_API_KEY):
        raise HTTPException(401, 'Invalid key')
    resp = JSONResponse({'ok': True})
    # Secure flag when served over HTTPS (tunnel); off for plain-HTTP LAN access.
    secure = request.headers.get('x-forwarded-proto', request.url.scheme) == 'https'
    resp.set_cookie('aria_session', ARIA_API_KEY, httponly=True, samesite='strict', path='/', secure=secure)
    return resp


@app.get('/')
async def index(request: Request):
    # Gate the SPA behind the key so the page never ships the credential to
    # unauthenticated visitors. Browsers authenticate via the aria_session cookie.
    if ARIA_API_KEY:
        cookie = request.cookies.get('aria_session', '')
        if not secrets.compare_digest(cookie, ARIA_API_KEY):
            return HTMLResponse(_LOGIN_HTML)
    return HTMLResponse(_index_html)
