"""Split / fix 'dump' and collab-string artists so their tracks regroup under
the real primary artist and become discoverable.

Patterns (verified against the library):
  - dump folder : albumartist "Amos Lee Discography 2005-2012 (5 Releases)",
    album "Amos Lee - 2006 - Supply And Demand"  -> artist "Amos Lee",
    album "Supply And Demand".
  - collab      : "Anthem Worship, Genavieve Linkowski, Mass Anthem"
    -> primary "Anthem Worship" (album/title already correct).
  - performer   : "Bernard Haitink & London Symph" -> "Bernard Haitink".

The primary candidate is derived by splitting on delimiters, then VERIFIED
against Deezer (accent/fuzzy match, reused from processor) before ANYTHING is
written — a name that isn't a real catalog artist is left untouched. Every tag
write is snapshotted (tag_backups) and recorded in file_moves (dst == src) so
cleanup.undo() reverses it. Only files whose current album-artist actually IS
the dump name are touched.
"""

import glob
import os
import re
import sqlite3

import mutagen

import processor
import tagfix
from sources import deezer

MUSIC_DIR = os.getenv('MUSIC_DIR', '/music')
DB_PATH = os.getenv('DB_PATH', '/data/aria.db')
AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.wav')

# "Artist - 2006 - Album Title" -> capture "Album Title"
_YEAR_ALBUM = re.compile(r'^.+?\s-\s(?:19|20)\d{2}\s-\s(.+)$')


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute('PRAGMA busy_timeout=10000')
    return c


def _first(m, *keys):
    for k in keys:
        v = m.get(k) if m else None
        if v:
            return str(v[0] if isinstance(v, (list, tuple)) else v)
    return None


def _derive_primary(name: str) -> str:
    """Best guess at the real primary artist buried in a dump/collab name."""
    n = re.split(r'\s*\bdiscography\b', name, flags=re.I)[0]
    for delim in (',', ' / ', '/', ' & ', ' feat', ' featuring', ' with ', ' x '):
        idx = n.lower().find(delim.lower())
        if idx > 0:
            n = n[:idx]
            break
    n = re.sub(r'\s*[\(\[].*$', '', n)  # drop trailing (…) / […]
    return n.strip(' -')


def _clean_album(album: str) -> str:
    if not album:
        return album
    mo = _YEAR_ALBUM.match(album)
    return mo.group(1).strip() if mo else album


def _title_from_filename(fn: str) -> str:
    stem = os.path.splitext(fn)[0]
    stem = re.sub(r'^\s*\d+\s*[-.\s]+', '', stem)           # leading track number
    stem = re.sub(r'^[^-]{1,40}-\s*', '', stem) if '-' in stem else stem  # "artist-title"
    return stem.strip() or os.path.splitext(fn)[0]


def _artist_folders(conn, artist_id: int) -> list:
    return [f for (f,) in conn.execute(
        "SELECT DISTINCT folder FROM albums WHERE artist_id=? "
        "AND folder IS NOT NULL AND folder!=''", (artist_id,)).fetchall()
        if os.path.isdir(f)]


async def plan() -> dict:
    """READ ONLY: for each still-skipped imported artist, derive + verify a real
    primary artist. No writes."""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, name FROM artists WHERE imported=1 "
        "AND (deezer_id IS NULL OR deezer_id='') ORDER BY name").fetchall()
    proposals = []
    for aid, name in rows:
        # A genuine "X & Y" duo (its FULL name is in the catalog) must never be
        # split — e.g. "Shane & Shane" is the actual artist, not a collab.
        full = await deezer.search_artist(name)
        if full and processor._name_match(full.get('name'), name):
            continue
        # Soundtracks derive to a game/label token that coincidentally matches a
        # real band (e.g. "Final Fantasy" the indie act) — never split those.
        if re.search(r'\b(ost|soundtrack)\b', name, re.I):
            continue
        # Reduplicated duo ("Shane & Shane") — the name IS the artist; the first
        # segment ("Shane") is a different, real artist. Never split these.
        if re.match(r'^(.+?)\s*(?:&|and)\s*\1\b', name, re.I):
            continue
        cand = _derive_primary(name)
        if not cand or cand.lower() == name.lower():
            continue  # nothing to split
        # Splitting reassigns the whole artist, so require an EXACT (accent/case
        # folded) catalog match — no fuzzy — to avoid landing on a coincidental
        # near-name (e.g. "Scott B. Morton" -> a different "Scott Morton").
        dz = await deezer.search_artist(cand)
        verified = bool(dz and processor._name_key(dz.get('name', '')) == processor._name_key(cand))
        folders = _artist_folders(conn, aid)
        nfiles = sum(len([p for p in glob.glob(f + '/*')
                          if p.lower().endswith(AUDIO_EXTS)]) for f in folders)
        proposals.append({'artist_id': aid, 'old_name': name, 'candidate': cand,
                          'deezer': dz.get('name') if dz else None,
                          'verified': verified, 'files': nfiles})
    conn.close()
    actionable = [p for p in proposals if p['verified'] and p['files']]
    return {'candidates': len(proposals), 'actionable': len(actionable),
            'items': proposals}


async def apply() -> dict:
    """Retag every actionable dump/collab artist's files to the verified primary
    artist (album cleaned, title filled from filename when missing). Returns the
    artist ids that were fixed so the caller can re-ingest + prune them."""
    import asyncio
    p = await plan()
    conn = _conn()
    fixed_ids, recorded = [], []
    files_fixed = 0
    for it in p['items']:
        if not (it['verified'] and it['files']):
            continue
        primary = it['candidate']
        touched = False
        for folder in _artist_folders(conn, it['artist_id']):
            for path in glob.glob(folder + '/*'):
                if not path.lower().endswith(AUDIO_EXTS) or not os.path.isfile(path):
                    continue
                try:
                    m = mutagen.File(path, easy=True)
                except Exception:
                    m = None
                if m is None:
                    continue
                # Only retag files that actually belong to this dump artist.
                if _first(m, 'albumartist', 'artist') != it['old_name']:
                    continue
                album = _clean_album(_first(m, 'album'))
                title = _first(m, 'title') or _title_from_filename(os.path.basename(path))
                await asyncio.to_thread(tagfix._apply_one_sync, path,
                                        primary, title, primary, album)
                recorded.append(path)
                files_fixed += 1
                touched = True
        if touched:
            fixed_ids.append(it['artist_id'])
    conn.close()
    if recorded:
        conn = _conn()
        try:
            conn.execute('''CREATE TABLE IF NOT EXISTS file_moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT NOT NULL,
                dst TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')))''')
        except Exception:
            pass
        conn.executemany('INSERT INTO file_moves (src, dst) VALUES (?, ?)',
                         [(pth, pth) for pth in recorded])
        conn.commit()
        conn.close()
    return {'artists_fixed': len(fixed_ids), 'files_fixed': files_fixed,
            'fixed_ids': fixed_ids}
