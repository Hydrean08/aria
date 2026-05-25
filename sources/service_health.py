import asyncio
import logging
import time

import httpx

_log = logging.getLogger(__name__)

_TIMEOUT = 4.0
_CACHE_TTL = 300  # 5 minutes

ALL_SERVICES = ['tidal', 'qobuz', 'amazon', 'apple', 'deezer']

_PROBES: dict[str, str] = {
    'tidal':  'https://listen.tidal.com/',
    'qobuz':  'https://www.qobuz.com/api.json/0.2/',
    'amazon': 'https://music.amazon.com/',
    'apple':  'https://music.apple.com/',
    'deezer': 'https://api.deezer.com/',
}

_cache: dict[str, bool] = {}
_cache_ts: float = 0.0
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _probe(client: httpx.AsyncClient, service: str) -> tuple[str, bool]:
    try:
        r = await client.head(_PROBES[service], timeout=_TIMEOUT, follow_redirects=True)
        return service, r.status_code < 500
    except Exception:
        return service, False


async def _refresh() -> dict[str, bool]:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_probe(client, s) for s in ALL_SERVICES])
    return dict(results)


async def get_healthy_services() -> list[str]:
    """Return ALL_SERVICES ordered: reachable first, unreachable last.

    Result is cached for 5 minutes. On probe failure, returns the full list
    unchanged so SpotiFLAC can still attempt all services.
    """
    global _cache, _cache_ts
    async with _get_lock():
        if time.monotonic() - _cache_ts > _CACHE_TTL:
            try:
                _cache = await _refresh()
                _cache_ts = time.monotonic()
                down = [s for s, ok in _cache.items() if not ok]
                if down:
                    _log.warning('Music services unreachable: %s', ', '.join(sorted(down)))
                else:
                    _log.debug('All music services reachable')
            except Exception as e:
                _log.warning('Service health check failed, using full list: %s', e)
                return list(ALL_SERVICES)
    return sorted(ALL_SERVICES, key=lambda s: (0 if _cache.get(s, True) else 1))
