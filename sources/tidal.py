import asyncio
import base64
import json
import os
import xml.etree.ElementTree as ET

import httpx

from tagger import safe_name, tag_file

_INSTANCES = [
    'https://us-west.monochrome.tf',
    'https://api.monochrome.tf',
    'https://monochrome-api.samidy.com',
]

_session: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(
            timeout=30,
            headers={
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
                'Origin': 'https://monochrome.tf',
                'Referer': 'https://monochrome.tf/',
            },
        )
    return _session


async def _get(path: str, params: dict | None = None) -> dict:
    for base in _INSTANCES:
        try:
            r = await _client().get(f'{base}{path}', params=params or {}, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if 'detail' not in data:
                    return data
        except Exception:
            continue
    return {}


async def search_album(artist: str, title: str) -> str | None:
    # Primary: album-level search
    try:
        data = await _get('/search/', {'al': f'{artist} {title}', 'limit': 10})
        for item in data.get('data', {}).get('items', []):
            item_title = item.get('title', '').lower()
            item_artist = (item.get('artist') or {}).get('name', '').lower()
            if title.lower() in item_title and (
                artist.lower() in item_artist or item_artist in artist.lower()
            ):
                return str(item['id'])
    except Exception:
        pass

    # Fallback: track search, extract album ID from matching results
    try:
        data = await _get('/search/', {'s': f'{artist} {title}', 'limit': 20})
        for item in data.get('data', {}).get('items', []):
            item_artist = (item.get('artist') or {}).get('name', '').lower()
            album = item.get('album') or {}
            album_title = album.get('title', '').lower()
            if title.lower() in album_title and artist.lower() in item_artist:
                return str(album['id'])
    except Exception:
        pass

    return None


async def download_album(
    tidal_album_id: str,
    dest_dir: str,
    artist: str,
    album: str,
    year: str = '',
) -> list[str]:
    try:
        data = await _get('/album/', {'id': tidal_album_id})
        items = (data.get('data') or {}).get('items', [])
    except Exception:
        return []

    if not items:
        return []

    os.makedirs(dest_dir, exist_ok=True)
    written = []
    total = len(items)

    for i, track in enumerate(items, 1):
        track_id = str(track['id'])
        title = track.get('title', f'Track {i}')
        track_artist = (track.get('artist') or {}).get('name', artist)

        path = await _download_track(
            track_id, dest_dir, i, title, artist, track_artist, album, year, total
        )
        if path:
            written.append(path)
        await asyncio.sleep(0.5)

    return written


async def _download_track(
    track_id: str,
    dest_dir: str,
    track_num: int,
    title: str,
    album_artist: str,
    track_artist: str,
    album: str,
    year: str,
    total: int,
) -> str | None:
    for quality in ('LOSSLESS', 'HIGH'):
        try:
            result = await _get('/track/', {'id': track_id, 'quality': quality})
            data = result.get('data') or {}
            mime = data.get('manifestMimeType', '')
            manifest_b64 = data.get('manifest', '')
            if not manifest_b64:
                continue

            decoded = base64.b64decode(manifest_b64)

            if mime == 'application/vnd.tidal.bts':
                manifest = json.loads(decoded)
                if manifest.get('encryptionType', 'NONE') != 'NONE':
                    continue
                urls = manifest.get('urls', [])
                if not urls:
                    continue
                ext = '.flac' if 'flac' in manifest.get('mimeType', '') else '.m4a'
                path = os.path.join(dest_dir, f'{track_num:02d} - {safe_name(title)}{ext}')
                r = await _client().get(urls[0], timeout=120)
                if r.status_code != 200:
                    continue
                with open(path, 'wb') as f:
                    f.write(r.content)
                tag_file(
                    path, title=title, artist=track_artist, album_artist=album_artist,
                    album=album, track_number=track_num, track_total=total, year=year,
                )
                return path

            elif mime == 'application/dash+xml':
                path = await _download_dash(
                    decoded, dest_dir, track_num, title,
                    album_artist, track_artist, album, year, total,
                )
                if path:
                    return path

        except Exception:
            continue

    return None


async def _download_dash(
    mpd_bytes: bytes,
    dest_dir: str,
    track_num: int,
    title: str,
    album_artist: str,
    track_artist: str,
    album: str,
    year: str,
    total: int,
) -> str | None:
    path = None
    try:
        ns = {'m': 'urn:mpeg:dash:schema:mpd:2011'}
        root = ET.fromstring(mpd_bytes)

        tmpl = root.find('.//m:SegmentTemplate', ns)
        if tmpl is None:
            return None

        init_url = tmpl.get('initialization', '')
        media_url = tmpl.get('media', '')
        start = int(tmpl.get('startNumber', '1'))

        count = 0
        timeline = tmpl.find('m:SegmentTimeline', ns)
        if timeline is not None:
            for s in timeline:
                count += int(s.get('r', '0')) + 1

        if not count or not init_url or not media_url:
            return None

        # Always .m4a — DASH output is fMP4 regardless of inner codec
        path = os.path.join(dest_dir, f'{track_num:02d} - {safe_name(title)}.m4a')

        client = _client()
        with open(path, 'wb') as f:
            r = await client.get(init_url, timeout=30)
            if r.status_code != 200:
                raise OSError(f'init segment HTTP {r.status_code}')
            f.write(r.content)

            for n in range(start, start + count):
                seg_url = media_url.replace('$Number$', str(n))
                r = await client.get(seg_url, timeout=30)
                if r.status_code != 200:
                    raise OSError(f'segment {n} HTTP {r.status_code}')
                f.write(r.content)

        tag_file(
            path, title=title, artist=track_artist, album_artist=album_artist,
            album=album, track_number=track_num, track_total=total, year=year,
        )
        return path

    except Exception:
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
        return None
