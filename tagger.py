import os
import re

from mutagen.id3 import (
    ID3, ID3NoHeaderError,
    TIT2, TPE1, TPE2, TALB, TRCK, TDRC, TPOS
)
from mutagen.flac import FLAC
from mutagen.mp4 import MP4


def safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


def tag_file(
    path: str,
    *,
    title: str = '',
    artist: str = '',
    album_artist: str = '',
    album: str = '',
    track_number: int = 0,
    track_total: int = 0,
    disc_number: int = 0,
    year: str = '',
) -> bool:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.mp3':
            return _tag_mp3(path, title=title, artist=artist, album_artist=album_artist,
                            album=album, track_number=track_number, track_total=track_total,
                            disc_number=disc_number, year=year)
        elif ext == '.flac':
            return _tag_flac(path, title=title, artist=artist, album_artist=album_artist,
                             album=album, track_number=track_number, track_total=track_total,
                             disc_number=disc_number, year=year)
        elif ext in ('.m4a', '.aac', '.mp4'):
            return _tag_mp4(path, title=title, artist=artist, album_artist=album_artist,
                            album=album, track_number=track_number, track_total=track_total,
                            disc_number=disc_number, year=year)
    except Exception:
        return False
    return False


def fix_album_artist(path: str, album_artist: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == '.mp3':
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            tags['TPE2'] = TPE2(encoding=3, text=album_artist)
            tags.save(path)
            return True
        elif ext == '.flac':
            audio = FLAC(path)
            audio['albumartist'] = album_artist
            audio.save()
            return True
        elif ext in ('.m4a', '.aac', '.mp4'):
            audio = MP4(path)
            audio['aART'] = album_artist
            audio.save()
            return True
    except Exception:
        return False
    return False


def _tag_mp3(path, *, title, artist, album_artist, album, track_number, track_total, disc_number, year):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()

    if title:
        tags['TIT2'] = TIT2(encoding=3, text=title)
    if artist:
        tags['TPE1'] = TPE1(encoding=3, text=artist)
    if album_artist:
        tags['TPE2'] = TPE2(encoding=3, text=album_artist)
    if album:
        tags['TALB'] = TALB(encoding=3, text=album)
    if track_number:
        track_str = f'{track_number}/{track_total}' if track_total else str(track_number)
        tags['TRCK'] = TRCK(encoding=3, text=track_str)
    if disc_number:
        tags['TPOS'] = TPOS(encoding=3, text=str(disc_number))
    if year:
        tags['TDRC'] = TDRC(encoding=3, text=year)

    tags.save(path)
    return True


def _tag_flac(path, *, title, artist, album_artist, album, track_number, track_total, disc_number, year):
    audio = FLAC(path)
    if title:
        audio['title'] = title
    if artist:
        audio['artist'] = artist
    if album_artist:
        audio['albumartist'] = album_artist
    if album:
        audio['album'] = album
    if track_number:
        audio['tracknumber'] = str(track_number)
        if track_total:
            audio['tracktotal'] = str(track_total)
    if disc_number:
        audio['discnumber'] = str(disc_number)
    if year:
        audio['date'] = year
    audio.save()
    return True


def enrich_file(path: str, *, genres: list[str] = (), label: str = '',
                catno: str = '', mb_recording_id: str = '', country: str = '') -> bool:
    if not any([genres, label, catno, mb_recording_id, country]):
        return False
    try:
        import mediafile as mf
        f = mf.MediaFile(path)
        if genres:
            f.genres = list(genres)
            f.genre = genres[0]
        if label:
            f.label = label
        if catno:
            f.catalognum = catno
        if mb_recording_id:
            f.mb_trackid = mb_recording_id
        if country:
            f.country = country
        f.save()
        return True
    except Exception:
        return False


def _tag_mp4(path, *, title, artist, album_artist, album, track_number, track_total, disc_number, year):
    audio = MP4(path)
    if title:
        audio['\xa9nam'] = title
    if artist:
        audio['\xa9ART'] = artist
    if album_artist:
        audio['aART'] = album_artist
    if album:
        audio['\xa9alb'] = album
    if track_number:
        audio['trkn'] = [(track_number, track_total or 0)]
    if disc_number:
        audio['disk'] = [(disc_number, 0)]
    if year:
        audio['\xa9day'] = year
    audio.save()
    return True
