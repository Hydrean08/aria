import os
import httpx

_PLEX_URL   = os.getenv('PLEX_URL', '')
_PLEX_TOKEN = os.getenv('PLEX_TOKEN', '')
_MUSIC_SECTION_KEY: str | None = None


async def _music_section_key() -> str | None:
    global _MUSIC_SECTION_KEY
    if _MUSIC_SECTION_KEY:
        return _MUSIC_SECTION_KEY
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f'{_PLEX_URL}/library/sections',
                params={'X-Plex-Token': _PLEX_TOKEN},
                headers={'Accept': 'application/json'},
            )
            for section in r.json().get('MediaContainer', {}).get('Directory', []):
                if section.get('type') == 'artist':
                    _MUSIC_SECTION_KEY = str(section['key'])
                    return _MUSIC_SECTION_KEY
    except Exception:
        pass
    return None


async def scan_music_library() -> bool:
    if not _PLEX_URL or not _PLEX_TOKEN:
        return False
    key = await _music_section_key()
    if not key:
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f'{_PLEX_URL}/library/sections/{key}/refresh',
                params={'X-Plex-Token': _PLEX_TOKEN},
            )
            return r.status_code < 400
    except Exception:
        return False
