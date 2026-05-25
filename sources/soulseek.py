import asyncio
import os
import time
import uuid
import httpx

SLSKD_URL     = os.getenv('SLSKD_URL', 'http://slskd:5030')
SLSKD_API_KEY = os.getenv('SLSKD_API_KEY', 'slskd-soularr-api-key')

_session: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(
            headers={'X-API-Key': SLSKD_API_KEY},
            timeout=15
        )
    return _session


async def ready() -> bool:
    try:
        r = await _client().get(f'{SLSKD_URL}/api/v0/application', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


async def search(artist: str, title: str, timeout: int = 30) -> list[dict]:
    search_id = str(uuid.uuid4())
    query = f'{artist} {title}'
    try:
        await _client().post(
            f'{SLSKD_URL}/api/v0/searches',
            json={'id': search_id, 'searchText': query}
        )
    except Exception:
        return []

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(2)
        try:
            r = await _client().get(f'{SLSKD_URL}/api/v0/searches/{search_id}')
            state = r.json().get('state', '')
            if 'Completed' in state or 'TimedOut' in state:
                break
        except Exception:
            pass

    try:
        r = await _client().get(f'{SLSKD_URL}/api/v0/searches/{search_id}/responses')
        data = r.json()
        results = data if isinstance(data, list) else []
        await _delete_search(search_id)
        return results
    except Exception:
        await _delete_search(search_id)
        return []


async def _delete_search(search_id: str):
    try:
        await _client().delete(f'{SLSKD_URL}/api/v0/searches/{search_id}')
    except Exception:
        pass


def _score_result(result: dict, artist: str, title: str, track_count: int) -> float:
    files = result.get('files', [])
    if not files:
        return 0.0

    artist_l = artist.lower()
    title_l = title.lower()

    audio_files = [
        f for f in files
        if f.get('filename', '').lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.opus'))
    ]
    if not audio_files:
        return 0.0

    score = 0.0

    # Track count match
    if track_count > 0:
        ratio = min(len(audio_files), track_count) / max(len(audio_files), track_count)
        score += ratio * 50

    # Folder name contains artist + title
    first_path = audio_files[0].get('filename', '').lower().replace('\\', '/')
    parts = first_path.split('/')
    folder = parts[-2] if len(parts) >= 2 else ''
    if artist_l in folder and title_l in folder:
        score += 30
    elif artist_l in folder or title_l in folder:
        score += 15

    # Quality bonus for FLAC
    if any(f.get('filename', '').lower().endswith('.flac') for f in audio_files):
        score += 10

    # Penalize very low-speed users
    upload_speed = result.get('uploadSpeed', 0)
    if upload_speed > 500_000:
        score += 5
    elif upload_speed < 50_000:
        score -= 10

    return score


async def find_best(artist: str, title: str, track_count: int = 0) -> dict | None:
    results = await search(artist, title)
    if not results:
        return None

    scored = [
        (r, _score_result(r, artist, title, track_count))
        for r in results
    ]
    scored = [(r, s) for r, s in scored if s > 10]
    if not scored:
        return None

    best, _ = max(scored, key=lambda x: x[1])
    return best


async def queue_download(result: dict, artist: str, title: str) -> list[str]:
    username = result.get('username', '')
    files = result.get('files', [])
    audio_files = [
        f for f in files
        if f.get('filename', '').lower().endswith(('.mp3', '.flac', '.m4a', '.ogg', '.opus'))
    ]
    if not audio_files:
        return []

    payload = [
        {'filename': f['filename'], 'size': f.get('size', 0), 'token': f.get('token', 0)}
        for f in audio_files
    ]
    try:
        r = await _client().post(
            f'{SLSKD_URL}/api/v0/transfers/downloads/{username}',
            json=payload
        )
        if r.status_code in (200, 201, 204):
            return [f['filename'] for f in audio_files]
    except Exception:
        pass
    return []


async def wait_for_downloads(filenames: list[str], timeout: int = 600) -> bool:
    if not filenames:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(10)
        try:
            r = await _client().get(f'{SLSKD_URL}/api/v0/transfers/downloads')
            transfers = r.json()
            pending = set(filenames)
            for user_group in (transfers if isinstance(transfers, list) else []):
                for d in user_group.get('directories', []):
                    for t in d.get('files', []):
                        if t.get('filename') in pending:
                            if 'Succeeded' in t.get('state', ''):
                                pending.discard(t['filename'])
            if not pending:
                return True
        except Exception:
            pass
    return False
