import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

_API_KEY  = os.getenv('ACOUSTID_API_KEY', '')
_executor = ThreadPoolExecutor(max_workers=2)


def _do_lookup(path: str) -> dict:
    import acoustid
    try:
        for score, recording_id, title, artist in acoustid.match(
            _API_KEY, path, meta=['recordings'], parse=True, force_fpcalc=False
        ):
            if score >= 0.6 and recording_id:
                return {'mb_recording_id': recording_id, 'title': title,
                        'artist': artist, 'score': score}
    except Exception:
        pass
    return {}


async def identify_file(path: str) -> dict:
    if not _API_KEY:
        return {}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _do_lookup, path)
