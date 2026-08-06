"""AudioMuse client — audio-content ("sounds like") similarity over the library.

AudioMuse analyses every track's actual audio (CLAP/MusiCNN embeddings + lyrics)
and exposes nearest-neighbour search over that space. Aria's own playlist
intelligence is an LLM reasoning over track TEXT, which can pick thematically
plausible tracks that don't actually sound alike; this fills that gap.

Auth: the AudioMuse UI is session-based (AUTH_ENABLED=true), so we log in once
via POST /auth and reuse the cookie. The session is cached and re-established
transparently on a 401.

Every call fails soft (returns [] / None) — sonic similarity is an enhancement,
so AudioMuse being down must never break playlist generation or the UI.
"""

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

BASE = os.getenv('AUDIOMUSE_URL', '').rstrip('/')
USER = os.getenv('AUDIOMUSE_USER', '')
PASSWORD = os.getenv('AUDIOMUSE_PASSWORD', '')
TIMEOUT = float(os.getenv('AUDIOMUSE_TIMEOUT', '20'))

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()


def configured() -> bool:
    return bool(BASE and USER and PASSWORD)


async def _login(client: httpx.AsyncClient) -> bool:
    try:
        r = await client.post('/auth', data={'user': USER, 'password': PASSWORD})
        # A successful form login redirects (302); the cookie lands on the client.
        return r.status_code in (200, 302)
    except Exception as e:
        logger.warning('AudioMuse login failed: %s', e)
        return False


async def _session() -> httpx.AsyncClient | None:
    """Cached authenticated client, created once."""
    global _client
    if not configured():
        return None
    async with _lock:
        if _client is None:
            client = httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT,
                                       follow_redirects=False)
            if not await _login(client):
                await client.aclose()
                return None
            _client = client
    return _client


async def _get(path: str, params: dict) -> list | dict | None:
    """GET with one transparent re-login if the session expired."""
    client = await _session()
    if client is None:
        return None
    for attempt in (1, 2):
        try:
            r = await client.get(path, params=params)
        except Exception as e:
            logger.warning('AudioMuse %s failed: %s', path, e)
            return None
        if r.status_code == 401 and attempt == 1:
            if not await _login(client):
                return None
            continue
        if r.status_code != 200:
            logger.warning('AudioMuse %s -> HTTP %s', path, r.status_code)
            return None
        try:
            return r.json()
        except Exception:
            return None
    return None


def _norm_track(t: dict) -> dict:
    """Flatten AudioMuse's neighbour shape into what Aria's UI/LLM consume."""
    return {
        'title':     t.get('title'),
        'artist':    t.get('author') or t.get('album_artist'),
        'album':     t.get('album'),
        'nav_id':    t.get('item_id'),      # Navidrome id — enables direct export
        'distance':  t.get('distance'),
        'genre':     t.get('top_genre'),
        'mood':      t.get('mood_vector'),
    }


async def similar_tracks(title: str, artist: str, n: int = 10,
                         eliminate_duplicates: bool = True) -> list[dict]:
    """Tracks that SOUND like this one. Empty list when unavailable/no match."""
    if not (title and artist):
        return []
    data = await _get('/api/similar_tracks', {
        'title': title, 'artist': artist, 'n': n,
        'eliminate_duplicates': str(bool(eliminate_duplicates)).lower(),
    })
    if not isinstance(data, list):
        return []
    return [_norm_track(t) for t in data if t.get('title')]


async def expand_seeds(seeds: list[dict], target: int = 12,
                       per_seed: int = 6) -> list[dict]:
    """Grow a few seed tracks into a sonically coherent set.

    Queries each seed's neighbourhood and interleaves the results, so the final
    playlist reflects every seed rather than being dominated by the first one.
    Seeds are kept (they're the user's actual intent) and de-duplicated against
    the expansion by (artist, title).
    """
    if not seeds:
        return []
    # Defence in depth: these bound a pure-Python round-robin below, so even if a
    # caller forgets to clamp its query param this can't spin the event loop.
    target = max(1, min(int(target), 200))
    per_seed = max(1, min(int(per_seed), 50))
    neighbour_lists = await asyncio.gather(
        *(similar_tracks(s.get('title', ''), s.get('artist', ''), n=per_seed)
          for s in seeds),
        return_exceptions=True,
    )
    out, seen = [], set()

    def _key(t):
        return ((t.get('artist') or '').strip().lower(),
                (t.get('title') or '').strip().lower())

    for s in seeds:
        if s.get('title') and _key(s) not in seen:
            seen.add(_key(s))
            out.append(s)
    # Round-robin across seeds so no single seed dominates.
    lists = [nl for nl in neighbour_lists if isinstance(nl, list)]
    for i in range(per_seed):
        for nl in lists:
            if len(out) >= target:
                return out[:target]
            if i < len(nl):
                t = nl[i]
                if _key(t) not in seen:
                    seen.add(_key(t))
                    out.append(t)
    return out[:target]


async def search_by_sound(query: str, n: int = 12) -> list[dict]:
    """CLAP text->audio search: 'dreamy shoegaze with reverb' -> real tracks.
    Matches how the music SOUNDS, not how it's tagged."""
    client = await _session()
    if client is None or not query:
        return []
    try:
        r = await client.post('/api/clap/search', json={'query': query, 'n': n})
        if r.status_code == 401:
            if not await _login(client):
                return []
            r = await client.post('/api/clap/search', json={'query': query, 'n': n})
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        logger.warning('AudioMuse clap search failed: %s', e)
        return []
    items = data.get('results', data) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    return [_norm_track(t) for t in items if isinstance(t, dict) and t.get('title')]


async def status() -> dict:
    """Reachability + auth, for the UI to gate sonic features on.

    Deliberately does NOT probe /api/similar_tracks: that returns non-200 for a
    track it doesn't know, which is a normal miss, not an outage — probing with a
    dummy title reports a perfectly healthy server as unavailable.
    """
    if not configured():
        return {'configured': False, 'available': False}
    reachable = False
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=TIMEOUT) as probe:
            r = await probe.get('/api/health')
            reachable = r.status_code == 200
    except Exception as e:
        logger.warning('AudioMuse unreachable: %s', e)
    authed = reachable and (await _session()) is not None
    return {'configured': True, 'available': bool(authed),
            'reachable': reachable}
