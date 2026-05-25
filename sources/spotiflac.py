import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from sources.service_health import get_healthy_services

_QOBUZ_TOKEN  = os.getenv('QOBUZ_TOKEN', '')
_AUDIO_EXTS   = {'.mp3', '.flac', '.m4a', '.ogg', '.opus'}
_executor     = ThreadPoolExecutor(max_workers=1)


def _scan_audio(directory: str) -> set[str]:
    found = set()
    for root, _, files in os.walk(directory):
        for f in files:
            if os.path.splitext(f)[1].lower() in _AUDIO_EXTS:
                found.add(os.path.join(root, f))
    return found


def _do_download(url: str, dest_dir: str, services: list[str]):
    from SpotiFLAC import SpotiFLAC as _SpotiFLAC
    os.makedirs(dest_dir, exist_ok=True)
    _SpotiFLAC(
        url=url,
        output_dir=dest_dir,
        services=services,
        quality='LOSSLESS',
        use_track_numbers=True,
        use_album_track_numbers=True,
        use_artist_subfolders=False,
        use_album_subfolders=False,
        first_artist_only=True,
        embed_lyrics=True,
        enrich_metadata=True,
        qobuz_token=_QOBUZ_TOKEN or None,
        log_level=logging.WARNING,
    )


async def _download(url: str, dest_dir: str) -> list[str]:
    for f in _scan_audio(dest_dir):
        try:
            os.unlink(f)
        except OSError:
            pass
    services = await get_healthy_services()
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_executor, _do_download, url, dest_dir, services)
    except Exception as e:
        logging.getLogger(__name__).warning('SpotiFLAC failed for %s: %s', url, e)
        return []
    return sorted(_scan_audio(dest_dir))


async def download_album(deezer_id: str, dest_dir: str) -> list[str]:
    return await _download(f'https://www.deezer.com/album/{deezer_id}', dest_dir)


async def download_album_tidal(tidal_id: str, dest_dir: str) -> list[str]:
    return await _download(f'https://tidal.com/album/{tidal_id}', dest_dir)


async def download_album_spotify(spotify_id: str, dest_dir: str) -> list[str]:
    return await _download(f'https://open.spotify.com/album/{spotify_id}', dest_dir)


async def download_track_spotify(spotify_id: str, dest_dir: str) -> list[str]:
    return await _download(f'https://open.spotify.com/track/{spotify_id}', dest_dir)
