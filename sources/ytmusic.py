import asyncio
import os

import yt_dlp
from mutagen.mp4 import MP4
from ytmusicapi import YTMusic

DOWNLOADS_DIR = os.getenv('DOWNLOADS_DIR', '/downloads')

_ytm = YTMusic()  # no auth — public search only


def _search_browse_id(artist: str, album: str) -> str | None:
    artist_l = artist.lower()
    album_l  = album.lower()
    results  = _ytm.search(f'{artist} {album}', filter='albums', limit=8)
    if not results:
        return None
    for r in results:
        r_title  = r.get('title', '').lower()
        r_artist = ' '.join(a.get('name', '') for a in r.get('artists', [])).lower()
        if album_l in r_title and (artist_l in r_artist or artist_l in r_title):
            return r.get('browseId')
    # Looser fallback: album title substring match only
    for r in results:
        if album_l in r.get('title', '').lower():
            return r.get('browseId')
    return None


async def search_album(artist: str, album: str) -> str | None:
    """Return a YouTube Music album browseId, or None if not found."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _search_browse_id, artist, album)
    except Exception:
        return None


def _search_song_video_id(artist: str, title: str) -> str | None:
    """Find a single track on YouTube Music as a song, then a video.

    Singles (e.g. AI-music releases) are often published only as a song/video and
    never surface under an 'albums' search, so album-only lookup misses them even
    though a human finds them instantly. Returns a videoId, or None.
    """
    artist_l = artist.lower()
    title_l  = title.lower()
    for filt in ('songs', 'videos'):
        results = _ytm.search(f'{artist} {title}', filter=filt, limit=8)
        if not results:
            continue
        # Strict: title matches and the artist appears in the artist list or title.
        for r in results:
            r_title  = r.get('title', '').lower()
            r_artist = ' '.join(a.get('name', '') for a in r.get('artists', [])).lower()
            if title_l in r_title and (artist_l in r_artist or artist_l in r_title) and r.get('videoId'):
                return r['videoId']
        # Looser: title substring only (top result wins).
        for r in results:
            if title_l in r.get('title', '').lower() and r.get('videoId'):
                return r['videoId']
    return None


async def search_song(artist: str, title: str) -> str | None:
    """Return a YouTube Music videoId for a single track, or None if not found."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _search_song_video_id, artist, title)
    except Exception:
        return None


def _fix_tags(files: list[str], artist: str, album: str) -> None:
    """Override album/artist tags — YouTube Music often sets 'Various Artists' on compilations."""
    for path in files:
        if not path.endswith('.m4a'):
            continue
        try:
            audio = MP4(path)
            audio['\xa9alb'] = [album]   # album title
            audio['aART']    = [artist]  # album artist
            # Only overwrite track artist if it's the generic placeholder
            current_artist = (audio.get('\xa9ART') or [''])[0]
            if not current_artist or current_artist.lower() == 'various artists':
                audio['\xa9ART'] = [artist]
            audio.save()
        except Exception:
            pass


def _download_browse_id(browse_id: str, dest: str, artist: str, album: str) -> list[str]:
    audio_exts = {'.m4a', '.mp3', '.opus', '.ogg', '.webm'}
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if os.path.splitext(f)[1].lower() in audio_exts:
            try:
                os.unlink(os.path.join(dest, f))
            except OSError:
                pass
    url = f'https://music.youtube.com/browse/{browse_id}'
    ydl_opts = {
        'format':       'bestaudio[ext=m4a][abr>200]/bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl':      os.path.join(dest, '%(playlist_index)02d - %(title)s.%(ext)s'),
        'quiet':        True,
        'no_warnings':  True,
        'ignoreerrors': True,
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = sorted(
        os.path.join(dest, f)
        for f in os.listdir(dest)
        if os.path.splitext(f)[1].lower() in audio_exts
    )
    _fix_tags(files, artist, album)
    return files


async def download_album(browse_id: str, dest: str, artist: str, album: str) -> list[str]:
    """Download all tracks for a browseId into dest. Returns list of file paths."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _download_browse_id, browse_id, dest, artist, album)
    except Exception:
        return []


def _download_video_id(video_id: str, dest: str, artist: str, album: str) -> list[str]:
    audio_exts = {'.m4a', '.mp3', '.opus', '.ogg', '.webm'}
    os.makedirs(dest, exist_ok=True)
    for f in os.listdir(dest):
        if os.path.splitext(f)[1].lower() in audio_exts:
            try:
                os.unlink(os.path.join(dest, f))
            except OSError:
                pass
    url = f'https://music.youtube.com/watch?v={video_id}'
    ydl_opts = {
        'format':       'bestaudio[ext=m4a][abr>200]/bestaudio[ext=m4a]/bestaudio/best',
        'outtmpl':      os.path.join(dest, '%(title)s.%(ext)s'),
        'quiet':        True,
        'no_warnings':  True,
        'ignoreerrors': True,
        'postprocessors': [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = sorted(
        os.path.join(dest, f)
        for f in os.listdir(dest)
        if os.path.splitext(f)[1].lower() in audio_exts
    )
    _fix_tags(files, artist, album)
    return files


async def download_song(video_id: str, dest: str, artist: str, album: str) -> list[str]:
    """Download a single track by videoId into dest. Returns list of file paths."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _download_video_id, video_id, dest, artist, album)
    except Exception:
        return []
