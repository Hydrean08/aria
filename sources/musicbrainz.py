import asyncio

import httpx

MB_API = 'https://musicbrainz.org/ws/2'

_session: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(
            timeout=15,
            headers={
                'User-Agent': 'Aria/1.0',
                'Accept': 'application/json',
            }
        )
    return _session


async def search_artist(name: str) -> str | None:
    try:
        await asyncio.sleep(1)
        r = await _client().get(
            f'{MB_API}/artist',
            params={'query': name, 'limit': 5, 'fmt': 'json'}
        )
        artists = r.json().get('artists', [])
        for a in artists:
            if a.get('name', '').lower() == name.lower():
                return a['id']
        return artists[0]['id'] if artists else None
    except Exception:
        return None


async def get_discography(mbid: str) -> list[dict]:
    results = []
    offset = 0
    while True:
        try:
            await asyncio.sleep(1)
            r = await _client().get(
                f'{MB_API}/release-group',
                params={
                    'artist': mbid,
                    'type': 'album|ep|single',
                    'limit': 100,
                    'offset': offset,
                    'fmt': 'json',
                }
            )
            data = r.json()
            groups = data.get('release-groups', [])
            if not groups:
                break
            for g in groups:
                year = (g.get('first-release-date') or '')[:4]
                results.append({'title': g['title'], 'year': year})
            offset += len(groups)
            if offset >= data.get('release-group-count', 0):
                break
        except Exception:
            break
    return results
