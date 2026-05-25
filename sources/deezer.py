import asyncio
import hashlib
import os

import httpx
from Crypto.Cipher import Blowfish

from tagger import safe_name, tag_file

DEEZER_API   = 'https://api.deezer.com'
DEEZER_GW    = 'https://www.deezer.com/ajax/gw-light.php'
DEEZER_MEDIA = 'https://media.deezer.com'
DEEMIX_ARL   = os.getenv('DEEMIX_ARL', '')

_BF_SECRET = 'g4el58wc0zvf9na1'

_session: httpx.AsyncClient | None = None
_check_form    = ''
_license_token = ''


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(
            timeout=30,
            headers={'Accept': 'application/json'},
            cookies={'arl': DEEMIX_ARL},
        )
    return _session


async def login() -> bool:
    global _check_form, _license_token
    if not DEEMIX_ARL:
        return False
    try:
        r = await _client().post(
            DEEZER_GW,
            params={'method': 'deezer.getUserData', 'api_version': '1.0', 'api_token': 'null'}
        )
        data = r.json().get('results', {})
        _check_form    = data.get('checkForm', '')
        _license_token = data.get('USER', {}).get('OPTIONS', {}).get('license_token', '')
        return bool(_check_form)
    except Exception:
        return False


async def ready() -> bool:
    if not _check_form:
        return await login()
    return True


def _bf_key(track_id: str) -> bytes:
    h = hashlib.md5(track_id.encode()).hexdigest()
    return bytes(ord(h[i]) ^ ord(h[i + 16]) ^ ord(_BF_SECRET[i]) for i in range(16))


def _decrypt(data: bytes, track_id: str) -> bytes:
    key = _bf_key(track_id)
    out = bytearray()
    for i, offset in enumerate(range(0, len(data), 2048)):
        chunk = data[offset:offset + 2048]
        if i % 2 == 0 and len(chunk) == 2048:
            cipher = Blowfish.new(key, Blowfish.MODE_CBC, b'\x00' * 8)
            chunk = cipher.decrypt(chunk)
        out.extend(chunk)
    return bytes(out)


async def _gw(method: str, params: dict) -> dict:
    r = await _client().post(
        DEEZER_GW,
        params={'method': method, 'api_version': '1.0', 'api_token': _check_form},
        json=params,
    )
    return r.json().get('results', {})


async def _track_url(track_token: str) -> str | None:
    try:
        r = await _client().post(
            f'{DEEZER_MEDIA}/v1/get_url',
            json={
                'license_token': _license_token,
                'media': [{'type': 'FULL', 'formats': [
                    {'cipher': 'BF_CBC_STRIPE', 'format': 'MP3_128'}
                ]}],
                'track_tokens': [track_token],
            }
        )
        return r.json()['data'][0]['media'][0]['sources'][0]['url']
    except Exception:
        return None


async def search_artists(query: str, limit: int = 12) -> list[dict]:
    try:
        r = await _client().get(f'{DEEZER_API}/search/artist', params={'q': query, 'limit': limit})
        return [
            {'name': a['name'], 'deezer_id': str(a['id']), 'image_url': a.get('picture_medium')}
            for a in r.json().get('data', [])
        ]
    except Exception:
        return []


async def get_related_artists(deezer_artist_id: str) -> list[dict]:
    try:
        r = await _client().get(f'{DEEZER_API}/artist/{deezer_artist_id}/related', params={'limit': 20})
        return [
            {'name': a['name'], 'deezer_id': str(a['id']), 'image_url': a.get('picture_medium')}
            for a in r.json().get('data', [])
        ]
    except Exception:
        return []


async def search_artist(name: str) -> dict | None:
    try:
        r = await _client().get(f'{DEEZER_API}/search/artist', params={'q': name, 'limit': 5})
        results = r.json().get('data', [])
        for result in results:
            if result['name'].lower() == name.lower():
                return result
        return results[0] if results else None
    except Exception:
        return None


async def get_artist_albums(deezer_artist_id: str) -> list[dict]:
    albums = []
    url = f'{DEEZER_API}/artist/{deezer_artist_id}/albums'
    try:
        while url:
            r = await _client().get(url, params={'limit': 100})
            data = r.json()
            for a in data.get('data', []):
                if a.get('record_type') in ('album', 'ep', 'single'):
                    albums.append(a)
            url = data.get('next')
    except Exception:
        pass
    return albums


async def search_album(artist: str, title: str) -> str | None:
    try:
        r = await _client().get(
            f'{DEEZER_API}/search/album',
            params={'q': f'{artist} {title}', 'limit': 10}
        )
        for result in r.json().get('data', []):
            result_artist = result.get('artist', {}).get('name', '').lower()
            result_title  = result.get('title', '').lower()
            if artist.lower() in result_artist and title.lower() in result_title:
                return str(result['id'])
            if result_artist in artist.lower() and result_title in title.lower():
                return str(result['id'])
    except Exception:
        pass
    return None


async def get_charts() -> dict:
    try:
        r = await _client().get(f'{DEEZER_API}/chart/0/artists', params={'limit': 12})
        artists = [
            {'name': a['name'], 'deezer_id': str(a['id']), 'image_url': a.get('picture_medium')}
            for a in r.json().get('data', [])
        ]
    except Exception:
        artists = []
    try:
        r = await _client().get(f'{DEEZER_API}/editorial/0/releases', params={'limit': 12})
        releases = [
            {
                'title': a['title'],
                'deezer_id': str(a['id']),
                'cover_url': a.get('cover_medium'),
                'artist': a.get('artist', {}).get('name', ''),
                'year': (a.get('release_date') or '')[:4],
            }
            for a in r.json().get('data', [])
        ]
    except Exception:
        releases = []
    return {'artists': artists, 'releases': releases}


async def get_top_tracks(deezer_artist_id: str, limit: int = 5) -> list[dict]:
    try:
        r = await _client().get(f'{DEEZER_API}/artist/{deezer_artist_id}/top', params={'limit': limit})
        result = []
        for t in r.json().get('data', []):
            album = t.get('album', {})
            result.append({
                'id': str(t['id']),
                'title': t.get('title', ''),
                'duration': t.get('duration', 0),
                'album_title': album.get('title', ''),
                'album_cover': album.get('cover_medium', ''),
                'year': (album.get('release_date') or '')[:4],
            })
        return result
    except Exception:
        return []


async def get_album_tracks(deezer_album_id: str) -> list[dict]:
    try:
        r = await _client().get(
            f'{DEEZER_API}/album/{deezer_album_id}/tracks',
            params={'limit': 100}
        )
        return [
            {
                'id': str(t['id']),
                'title': t.get('title', ''),
                'track_number': t.get('track_position', i + 1),
                'duration': t.get('duration', 0),
                'artist': t.get('artist', {}).get('name', ''),
            }
            for i, t in enumerate(r.json().get('data', []))
        ]
    except Exception:
        return []


async def download_track(
    track_id: str,
    dest_dir: str,
    title: str,
    artist: str,
    album: str,
    track_num: int = 1,
    year: str = '',
) -> str | None:
    if not await ready():
        return None
    try:
        gw = await _gw('song.getData', {'sng_id': int(track_id)})
        track_token = gw.get('TRACK_TOKEN', '')
        title = gw.get('SNG_TITLE', title)
        track_num = int(gw.get('TRACK_NUMBER', track_num) or track_num)
        if not track_token:
            return None
    except Exception:
        return None

    url = await _track_url(track_token)
    if not url:
        return None

    try:
        r = await _client().get(url, timeout=120)
        raw = r.content
    except Exception:
        return None

    decrypted = _decrypt(raw, track_id)
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f'{track_num:02d} - {safe_name(title)}.mp3')
    with open(path, 'wb') as f:
        f.write(decrypted)

    tag_file(
        path, title=title, artist=artist, album_artist=artist,
        album=album, track_number=track_num, track_total=0, year=year,
    )
    return path


async def download_album(
    deezer_album_id: str,
    dest_dir: str,
    artist: str,
    album: str,
    year: str = '',
) -> list[str]:
    if not await ready():
        return []

    try:
        r = await _client().get(
            f'{DEEZER_API}/album/{deezer_album_id}/tracks',
            params={'limit': 100}
        )
        tracks = r.json().get('data', [])
    except Exception:
        return []

    if not tracks:
        return []

    os.makedirs(dest_dir, exist_ok=True)
    written = []
    total = len(tracks)

    for i, track in enumerate(tracks, 1):
        track_id    = str(track['id'])
        title       = track.get('title', f'Track {i}')
        track_artist = track.get('artist', {}).get('name', artist)

        try:
            gw = await _gw('song.getData', {'sng_id': int(track_id)})
            track_token = gw.get('TRACK_TOKEN', '')
            if not track_token:
                continue
        except Exception:
            continue

        url = await _track_url(track_token)
        if not url:
            continue

        try:
            r = await _client().get(url, timeout=120)
            raw = r.content
        except Exception:
            continue

        decrypted = _decrypt(raw, track_id)

        path = os.path.join(dest_dir, f'{i:02d} - {safe_name(title)}.mp3')
        with open(path, 'wb') as f:
            f.write(decrypted)

        tag_file(
            path,
            title=title,
            artist=track_artist,
            album_artist=artist,
            album=album,
            track_number=i,
            track_total=total,
            year=year,
        )

        written.append(path)
        await asyncio.sleep(0.3)

    return written
