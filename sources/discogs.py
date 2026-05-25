import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

_TOKEN    = os.getenv('DISCOGS_TOKEN', '')
_executor = ThreadPoolExecutor(max_workers=1)
_client   = None


def _get_client():
    global _client
    if _client is None:
        import discogs_client
        _client = discogs_client.Client('Aria/1.0', user_token=_TOKEN)
    return _client


def _do_search(artist: str, title: str) -> dict:
    try:
        results = _get_client().search(f'{artist} {title}', type='release')
        if not results or not results.count:
            return {}
        r = results[0]
        d = r.data
        label_list = d.get('label', [])
        return {
            'genres':  d.get('genre', []),
            'styles':  d.get('style', []),
            'label':   label_list[0] if label_list else '',
            'catno':   d.get('catno', ''),
            'country': d.get('country', ''),
        }
    except Exception:
        return {}


async def get_album_metadata(artist: str, title: str) -> dict:
    if not _TOKEN:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _do_search, artist, title)
