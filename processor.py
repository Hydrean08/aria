import asyncio
import os
import re
import shutil
import subprocess

import httpx

import db
from tagger import safe_name, fix_album_artist, enrich_file
from sources import acoustid_lookup, deezer, discogs, musicbrainz, plex, soulseek, spotiflac, spotify, ytmusic


async def send_push(title: str, body: str) -> None:
    """Best-effort push to all registered Expo tokens. Never raises."""
    try:
        async with db.connect() as conn:
            rows = await (await conn.execute('SELECT token FROM push_tokens')).fetchall()
        tokens = [r[0] for r in rows]
        if not tokens:
            return
        messages = [{'to': t, 'title': title, 'body': body} for t in tokens]
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                'https://exp.host/--/api/v2/push/send',
                json=messages,
                headers={'Content-Type': 'application/json'},
            )
    except Exception:
        pass

MUSIC_DIR     = os.getenv('MUSIC_DIR', '/music')
DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', '/downloads')
MAX_ALBUM_RETRIES = 5


def _dest_dir(artist: str, album: str) -> str:
    return os.path.join(MUSIC_DIR, safe_name(artist), safe_name(album))


def _validate_files_sync(files: list[str]) -> bool:
    """Decode-test the first file to catch Blowfish decryption failures."""
    if not files:
        return False
    result = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', files[0], '-f', 'null', '-'],
        capture_output=True, text=True
    )
    bad = 'Header missing' in result.stderr or 'Invalid data found' in result.stderr
    return not bad


async def _validate_files(files: list[str]) -> bool:
    """Async wrapper — runs the blocking ffmpeg check in a thread."""
    return await asyncio.to_thread(_validate_files_sync, files)


def _cleanup_files_sync(files: list[str]) -> None:
    """Remove files from a failed download attempt and prune empty dirs."""
    for f in files:
        try:
            os.unlink(f)
        except OSError:
            pass
    for d in {os.path.dirname(f) for f in files}:
        try:
            os.rmdir(d)
        except OSError:
            pass


async def _cleanup_files(files: list[str]) -> None:
    """Async wrapper — runs blocking file removal in a thread."""
    await asyncio.to_thread(_cleanup_files_sync, files)


async def _update_album(album_id: int, **kwargs):
    cols = ', '.join(f'{k} = ?' for k in kwargs)
    vals = list(kwargs.values()) + [album_id]
    async with db.connect() as conn:
        await conn.execute(
            f"UPDATE OR IGNORE albums SET {cols}, updated_at = datetime('now') WHERE id = ?",
            vals
        )
        await conn.commit()


async def sync_artist(artist_name: str, deezer_id: str | None):
    await db.log('info', f'Syncing catalog for {artist_name}')

    # Spotify is the primary catalog source
    sp_artist    = await spotify.search_artist(artist_name)
    sp_albums    = await spotify.get_artist_albums(sp_artist['spotify_id']) if sp_artist else []

    # MusicBrainz fallback when Spotify yields nothing
    mbid      = None
    mb_albums = []
    if not sp_albums:
        mbid      = await musicbrainz.search_artist(artist_name)
        mb_albums = await musicbrainz.get_discography(mbid) if mbid else []

    # Deezer cross-reference for IDs/covers regardless of primary source
    deezer_albums  = await deezer.get_artist_albums(deezer_id) if deezer_id else []
    artist_l       = artist_name.lower()
    deezer_by_title: dict = {}
    for a in deezer_albums:
        key = a['title'].lower().strip()
        deezer_by_title[key] = a
        for sep in (': ', ' - '):
            prefix = artist_l + sep
            if key.startswith(prefix):
                deezer_by_title[key[len(prefix):]] = a

    if sp_albums:
        to_insert = []
        for sa in sp_albums:
            dz = deezer_by_title.get(sa['title'].lower().strip())
            to_insert.append({
                'title':       sa['title'],
                'year':        sa['year'],
                'spotify_id':  sa['spotify_id'],
                'deezer_id':   str(dz['id']) if dz else None,
                'track_count': sa['track_count'],
                'cover_url':   sa['cover_url'] or (dz.get('cover_medium') if dz else None),
                'record_type': sa['record_type'],
            })
    elif mb_albums:
        to_insert = []
        for mb in mb_albums:
            dz = deezer_by_title.get(mb['title'].lower().strip())
            to_insert.append({
                'title':       mb['title'],
                'year':        mb['year'],
                'spotify_id':  None,
                'deezer_id':   str(dz['id']) if dz else None,
                'track_count': dz.get('nb_tracks', 0) if dz else 0,
                'cover_url':   dz.get('cover_medium') if dz else None,
                'record_type': dz.get('record_type', 'album') if dz else 'album',
            })
    elif deezer_albums:
        to_insert = [{
            'title':       a['title'],
            'year':        (a.get('release_date') or '')[:4],
            'spotify_id':  None,
            'deezer_id':   str(a['id']),
            'track_count': a.get('nb_tracks', 0),
            'cover_url':   a.get('cover_medium'),
            'record_type': a.get('record_type', 'album'),
        } for a in deezer_albums]
    else:
        await db.log('warn', f'No catalog found for {artist_name}')
        return

    async with db.connect() as conn:
        row = await (await conn.execute(
            'SELECT id FROM artists WHERE name = ?', (artist_name,)
        )).fetchone()
        if not row:
            return
        artist_id = row[0]

        if sp_artist:
            await conn.execute(
                'UPDATE artists SET spotify_id=?, image_url=coalesce(?,image_url) WHERE id=?',
                (sp_artist['spotify_id'], sp_artist.get('image_url'), artist_id)
            )
        if mbid:
            await conn.execute('UPDATE artists SET mb_id=? WHERE id=?', (mbid, artist_id))

        for album in to_insert:
            if album['deezer_id']:
                await conn.execute(
                    '''UPDATE OR IGNORE albums
                       SET deezer_id=?, spotify_id=coalesce(?,spotify_id),
                           year=?, track_count=?, cover_url=?, record_type=?
                       WHERE artist_id=? AND title=? AND (deezer_id IS NULL OR deezer_id=?)''',
                    (album['deezer_id'], album['spotify_id'], album['year'],
                     album['track_count'], album['cover_url'], album['record_type'],
                     artist_id, album['title'], album['deezer_id'])
                )
                await conn.execute(
                    '''INSERT OR IGNORE INTO albums
                       (artist_id, title, year, deezer_id, spotify_id, track_count, cover_url, record_type, wanted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)''',
                    (artist_id, album['title'], album['year'], album['deezer_id'],
                     album['spotify_id'], album['track_count'], album['cover_url'], album['record_type'])
                )
            else:
                await conn.execute(
                    '''INSERT INTO albums
                       (artist_id, title, year, spotify_id, track_count, cover_url, record_type, wanted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                       ON CONFLICT(artist_id, title) DO UPDATE SET
                           year        = excluded.year,
                           spotify_id  = coalesce(excluded.spotify_id, albums.spotify_id),
                           track_count = excluded.track_count,
                           cover_url   = coalesce(excluded.cover_url, albums.cover_url),
                           record_type = excluded.record_type''',
                    (artist_id, album['title'], album['year'], album['spotify_id'],
                     album['track_count'], album['cover_url'], album['record_type'])
                )
        try:
            await conn.commit()
        except Exception as e:
            if 'UNIQUE constraint' not in str(e):
                raise
            # Two concurrent syncs raced — the other commit won and the data is
            # already correct, so this is safe to ignore.
            await conn.rollback()

    sp_matched = sum(1 for a in to_insert if a['spotify_id'])
    dz_matched = sum(1 for a in to_insert if a['deezer_id'])
    await db.log('info', f'Synced {len(to_insert)} albums for {artist_name} ({sp_matched} on Spotify, {dz_matched} on Deezer)')


def _collect_audio_sync(artist: str, album: str) -> list[str]:
    found = []
    exts = {'.mp3', '.flac', '.m4a', '.ogg', '.opus'}
    artist_l = artist.lower()
    album_l  = album.lower()
    for root, _, files in os.walk(DOWNLOADS_DIR):
        rel = root.lower()
        if artist_l not in rel or album_l not in rel:
            continue
        for f in files:
            if os.path.splitext(f)[1].lower() in exts:
                found.append(os.path.join(root, f))
    return sorted(found)


async def _collect_audio(artist: str, album: str) -> list[str]:
    """Async wrapper — os.walk on a network/FUSE mount can block."""
    return await asyncio.to_thread(_collect_audio_sync, artist, album)


def _move_to_library_sync(files: list[str], artist: str, album: str) -> list[str]:
    dest = _dest_dir(artist, album)
    os.makedirs(dest, exist_ok=True)
    moved = []
    for src in files:
        dst = os.path.join(dest, os.path.basename(src))
        if not os.path.exists(dst):
            try:
                shutil.move(src, dst)
            except Exception:
                continue
        fix_album_artist(dst, artist)
        moved.append(dst)
    return moved


async def _move_to_library(files: list[str], artist: str, album: str) -> list[str]:
    """Async wrapper — shutil.move and fix_album_artist both do blocking I/O."""
    return await asyncio.to_thread(_move_to_library_sync, files, artist, album)


async def _process_album(album_id: int, artist_name: str, album_title: str,
                         album_deezer_id: str | None, album_spotify_id: str | None,
                         track_count: int, year: str) -> bool:
    await db.log('info', f'Processing: {artist_name} — {album_title}')
    await _update_album(album_id, status='downloading')

    dest      = _dest_dir(artist_name, album_title)
    files: list[str] = []
    source    = None

    spotify_id = album_spotify_id
    deezer_id  = album_deezer_id

    if not spotify_id:
        spotify_id = await spotify.search_album(artist_name, album_title)
        if spotify_id:
            await _update_album(album_id, spotify_id=spotify_id)

    if not deezer_id:
        deezer_id = await deezer.search_album(artist_name, album_title)
        if deezer_id:
            await _update_album(album_id, deezer_id=deezer_id)

    # 1. SpotiFLAC via Spotify URL (tries Tidal/Qobuz/Amazon/Apple/Deezer internally)
    if spotify_id:
        files = await spotiflac.download_album_spotify(spotify_id, dest)
        if files and await _validate_files(files):
            source = 'spotiflac'
        else:
            await _cleanup_files(files)
            files = []

    # 2. SpotiFLAC via Deezer URL
    if not files and deezer_id:
        files = await spotiflac.download_album(deezer_id, dest)
        if files and await _validate_files(files):
            source = 'spotiflac'
        else:
            await _cleanup_files(files)
            files = []

    # 3. Soulseek
    if not files:
        result = await soulseek.find_best(artist_name, album_title, track_count)
        if result:
            queued = await soulseek.queue_download(result, artist_name, album_title)
            if queued:
                await soulseek.wait_for_downloads(queued, timeout=150)
                collected = await _collect_audio(artist_name, album_title)
                files = await _move_to_library(collected, artist_name, album_title)
                if files and await _validate_files(files):
                    source = 'soulseek'
                else:
                    await _cleanup_files(files)
                    files = []

    # 4. YouTube Music (no account — ~128kbps m4a, broad catalog)
    if not files:
        browse_id = await ytmusic.search_album(artist_name, album_title)
        if browse_id:
            files = await ytmusic.download_album(browse_id, dest, artist_name, album_title)
            if files and await _validate_files(files):
                source = 'ytmusic'
            else:
                await _cleanup_files(files)
                files = []

    if not files:
        async with db.connect() as conn:
            row = await (await conn.execute(
                'SELECT retry_count FROM albums WHERE id = ?', (album_id,)
            )).fetchone()
        new_count = (row[0] if row else 0) + 1
        if new_count >= MAX_ALBUM_RETRIES:
            await _update_album(album_id, status='error', retry_count=new_count,
                                error=f'Not found after {new_count} attempts')
            await db.log('warn', f'Giving up after {new_count} attempts: {artist_name} — {album_title}')
            asyncio.create_task(send_push('❌ Download failed', f'{artist_name} — {album_title} (gave up after {new_count} tries)'))
        else:
            await _update_album(album_id, status='missing', retry_count=new_count,
                                error='Not found on SpotiFLAC, Soulseek, or YouTube Music')
            await db.log('warn', f'No source found (attempt {new_count}/{MAX_ALBUM_RETRIES}): {artist_name} — {album_title}')
            asyncio.create_task(send_push('❌ Download failed', f'{artist_name} — {album_title}'))
        return False

    await _enrich(files, artist_name, album_title, source)

    actual  = len(files)
    status  = 'complete' if (track_count == 0 or actual >= track_count) else 'partial'
    updates = {'status': status, 'source': source, 'error': None}
    if track_count == 0:
        updates['track_count'] = actual
    await _update_album(album_id, **updates)
    await db.log('info', f'Done: {artist_name} — {album_title} ({len(files)} tracks via {source})')
    asyncio.create_task(send_push('🎵 Downloaded', f'{artist_name} — {album_title}'))
    return True


async def _enrich(files: list[str], artist: str, album: str, source: str):
    disc_meta = await discogs.get_album_metadata(artist, album)

    for path in files:
        mb_recording_id = ''
        if source != 'spotiflac':
            result = await acoustid_lookup.identify_file(path)
            mb_recording_id = result.get('mb_recording_id', '')

        enrich_file(
            path,
            genres=disc_meta.get('genres', []),
            label=disc_meta.get('label', ''),
            catno=disc_meta.get('catno', ''),
            country=disc_meta.get('country', ''),
            mb_recording_id=mb_recording_id,
        )


_download_sem = asyncio.Semaphore(3)


async def retry_album(album_id: int):
    async with db.connect() as conn:
        row = await (await conn.execute('''
            SELECT ar.name, al.title, al.deezer_id, al.spotify_id, al.track_count, al.year
            FROM albums al JOIN artists ar ON ar.id = al.artist_id
            WHERE al.id = ?
        ''', (album_id,))).fetchone()
        if row:
            await conn.execute(
                "UPDATE albums SET retry_count = 0, status = 'missing' WHERE id = ?", (album_id,)
            )
            await conn.commit()
    if not row:
        return
    async with _download_sem:
        downloaded = await _process_album(album_id, row[0], row[1], row[2], row[3], row[4], row[5])
    if downloaded:
        await plex.scan_music_library()


async def scan_existing_library() -> dict:
    """Walk MUSIC_DIR, match {Artist}/{Album}/ folders against the albums
    table, and mark matches as 'complete' so users with a pre-existing
    library aren't shown 0/N when they actually have content on disk.

    Only ever flips albums FROM 'missing' to 'complete' — never overwrites
    a 'partial', 'failed', or already-complete record (those represent real
    Aria-managed state). Idempotent — safe to re-run.

    Returns {'scanned_artists', 'matched_albums', 'unmatched_dirs'} for the
    caller to surface.
    """
    if not os.path.isdir(MUSIC_DIR):
        return {'scanned_artists': 0, 'matched_albums': 0, 'unmatched_dirs': 0,
                'error': f'MUSIC_DIR not found: {MUSIC_DIR}'}

    audio_exts = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus')

    def _norm(s: str) -> str:
        """Lossy normalization for fuzzy matching — strips punctuation,
        lowercases, collapses whitespace. Lets "Awake (Deluxe Edition)"
        match "Awake-Deluxe-Edition" or "Awake_DeluxeEdition" if needed."""
        if not s:
            return ''
        return re.sub(r'[^a-z0-9]+', '', s.lower())

    async with db.connect() as conn:
        artist_rows = await (await conn.execute(
            'SELECT id, name FROM artists'
        )).fetchall()
        # Pull track_count so we can compare actual-on-disk against expected.
        # Only consider rows we're allowed to flip — 'complete' and 'partial'
        # rows are already in a known state and shouldn't be overwritten by
        # the scan (the user may have curated them).
        album_rows = await (await conn.execute(
            "SELECT id, artist_id, title, track_count "
            "FROM albums WHERE status = 'missing'"
        )).fetchall()

    # Group missing albums by artist for cheap per-artist lookup.
    albums_by_artist: dict[int, list[tuple[int, str, str, int]]] = {}
    for album_id, artist_id, title, track_count in album_rows:
        albums_by_artist.setdefault(artist_id, []).append(
            (album_id, title, _norm(title), track_count or 0)
        )

    # Three buckets: complete (actual >= expected), partial (1..expected-1),
    # skipped (the folder has 0 audio files — we leave the DB row alone).
    complete_ids: list[int] = []
    partial_ids: list[int] = []
    unmatched_dirs: list[str] = []
    scanned_artists = 0

    for artist_id, artist_name in artist_rows:
        # Aria writes Artist/ as safe_name(artist_name) — match that first,
        # then fall back to the raw artist name in case the user organized
        # things slightly differently before Aria existed.
        candidate_dirs = [
            os.path.join(MUSIC_DIR, safe_name(artist_name)),
            os.path.join(MUSIC_DIR, artist_name),
        ]
        artist_dir = next((d for d in candidate_dirs if os.path.isdir(d)), None)
        if not artist_dir:
            continue
        scanned_artists += 1

        try:
            subdirs = [d for d in os.listdir(artist_dir)
                       if os.path.isdir(os.path.join(artist_dir, d))]
        except OSError:
            continue

        artist_albums = albums_by_artist.get(artist_id, [])
        if not artist_albums:
            # Artist exists on disk + DB, but no missing albums to match.
            continue

        for subdir in subdirs:
            full = os.path.join(artist_dir, subdir)
            # Count the actual audio files at the top of the album folder.
            # Skip recursion — album dirs are flat and recursing would
            # double-count anything in a /Disc 1/ subfolder.
            try:
                actual_tracks = sum(
                    1 for f in os.listdir(full)
                    if os.path.isfile(os.path.join(full, f))
                    and f.lower().endswith(audio_exts)
                )
            except OSError:
                actual_tracks = 0
            if actual_tracks == 0:
                # Empty folder — don't even consider this a match.
                continue

            subdir_norm = _norm(subdir)
            # Exact-normalized match first (Skillet/Alien Youth → "Alien Youth").
            # Then substring fallback so "Awake-Deluxe" matches "Awake (Deluxe Edition)".
            picked = None
            for album_id, title, title_norm, track_count in artist_albums:
                if subdir_norm == title_norm:
                    picked = (album_id, track_count)
                    break
            if picked is None:
                for album_id, title, title_norm, track_count in artist_albums:
                    if title_norm and (title_norm in subdir_norm or subdir_norm in title_norm):
                        picked = (album_id, track_count)
                        break
            if picked is None:
                unmatched_dirs.append(f'{artist_name}/{subdir}')
                continue

            album_id, expected = picked
            # No expected count → can't classify safely; treat any presence
            # as complete (matches Aria's behavior for albums it downloaded
            # itself, which also have track_count=0 for some sources).
            if expected <= 0 or actual_tracks >= expected:
                complete_ids.append(album_id)
            else:
                partial_ids.append(album_id)

    if complete_ids:
        async with db.connect() as conn:
            placeholders = ','.join('?' * len(complete_ids))
            await conn.execute(
                f"UPDATE albums SET status='complete', source='existing', "
                f"updated_at=datetime('now') WHERE id IN ({placeholders})",
                complete_ids,
            )
            await conn.commit()
    if partial_ids:
        async with db.connect() as conn:
            placeholders = ','.join('?' * len(partial_ids))
            await conn.execute(
                f"UPDATE albums SET status='partial', source='existing', "
                f"updated_at=datetime('now') WHERE id IN ({placeholders})",
                partial_ids,
            )
            await conn.commit()

    await db.log(
        'info',
        f'Library scan: {len(complete_ids)} complete, {len(partial_ids)} partial '
        f'across {scanned_artists} artists ({len(unmatched_dirs)} unmatched dirs)',
    )
    return {
        'scanned_artists': scanned_artists,
        'matched_albums':  len(complete_ids),  # kept for backward-compat
        'complete':        len(complete_ids),
        'partial':         len(partial_ids),
        'unmatched_dirs':  len(unmatched_dirs),
    }


async def run_cycle():
    await db.log('info', 'Cycle started')

    async with db.connect() as conn:
        rows = await (await conn.execute('''
            SELECT al.id, ar.name, al.title, al.deezer_id, al.spotify_id, al.track_count, al.year
            FROM albums al
            JOIN artists ar ON ar.id = al.artist_id
            WHERE al.status = 'missing' AND al.wanted = 1 AND ar.monitored = 1
            ORDER BY ar.name, al.year
        ''')).fetchall()

    async def _bounded(row):
        async with _download_sem:
            return await _process_album(row[0], row[1], row[2], row[3], row[4], row[5], row[6])

    # return_exceptions=True so one bad album (Deezer 5xx, tag failure,
    # filesystem hiccup) doesn't abort the whole gather and leave every later
    # album waiting until the next cycle. Each failure becomes a logged
    # warning; the cycle still finishes and scans Plex for whatever succeeded.
    download_results = await asyncio.gather(
        *[_bounded(row) for row in rows], return_exceptions=True
    )
    succeeded = []
    for row, result in zip(rows, download_results):
        if isinstance(result, BaseException):
            await db.log(
                'warn',
                f'Album sync failed for {row[1]} — {row[2]}: '
                f'{type(result).__name__}: {result!r}',
            )
        else:
            succeeded.append(result)

    if any(succeeded):
        await plex.scan_music_library()
    await db.log('info', 'Cycle complete')
