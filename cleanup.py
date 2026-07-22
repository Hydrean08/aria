"""Stray-file rescue.

Loose / untagged audio (files with no artist or album tag, or sitting in the
music root) is invisible to Aria's tag-driven ingestion. This gives each stray a
real artist + title + album so it groups under the right artist and becomes
discoverable, and files a root-level stray into the artist's folder.

Identification order:
  - ARTIST: the file's parent folder name (the user already sorted by artist),
    falling back to the AcoustID fingerprint for files in the music root.
  - TITLE:  AcoustID fingerprint, falling back to the cleaned filename.
  - ALBUM:  "Singles" — an honest bucket for loose tracks whose album is unknown.

Safety: plan() is READ ONLY. apply() snapshots original tags (tag_backups) before
writing and records any relocation (file_moves); files already in an artist
folder are tagged in place (never moved). undo() reverses both.
"""

import glob
import os
import re
import shutil
import sqlite3

import mutagen

import tagfix
from sources import acoustid_lookup

MUSIC_DIR = os.getenv('MUSIC_DIR', '/music')
DB_PATH = os.getenv('DB_PATH', '/data/aria.db')
AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.wav')


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.execute('PRAGMA busy_timeout=10000')
    return c


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', (name or '')).strip() or 'Unknown'


def _get(m, *keys):
    for k in keys:
        v = m.get(k) if m else None
        if v:
            return str(v[0] if isinstance(v, (list, tuple)) else v).strip()
    return None


def _find_strays() -> list:
    root = os.path.realpath(MUSIC_DIR)
    out = []
    for p in glob.iglob(MUSIC_DIR + '/**/*', recursive=True):
        if not p.lower().endswith(AUDIO_EXTS) or not os.path.isfile(p):
            continue
        try:
            m = mutagen.File(p, easy=True)
        except Exception:
            m = None
        artist = _get(m, 'artist', 'albumartist')
        album = _get(m, 'album')
        in_root = os.path.dirname(os.path.realpath(p)) == root
        if in_root or not artist or not album:
            out.append(p)
    return out


async def plan() -> dict:
    """READ ONLY: fingerprint every stray and propose artist/title/album."""
    root = os.path.realpath(MUSIC_DIR)
    strays = _find_strays()
    items = []
    for p in strays:
        folder = os.path.dirname(os.path.realpath(p))
        folder_artist = None if folder == root else os.path.basename(folder)
        ident = await acoustid_lookup.identify_file(p)
        fp_artist = ident.get('artist')
        fp_title = ident.get('title')
        score = ident.get('score')
        artist = folder_artist or fp_artist
        title = fp_title or os.path.splitext(os.path.basename(p))[0]
        items.append({
            'path': p,
            'in_root': folder == root,
            'folder_artist': folder_artist,
            'fp_artist': fp_artist, 'fp_title': fp_title,
            'score': round(score, 2) if score else None,
            'artist': artist, 'title': title, 'album': 'Singles',
            'actionable': bool(artist),
        })
    actionable = [i for i in items if i['actionable']]
    return {'total': len(items), 'actionable': len(actionable),
            'unresolvable': len(items) - len(actionable), 'items': items}


def _ensure_moves(conn) -> None:
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS file_moves (
            id INTEGER PRIMARY KEY AUTOINCREMENT, src TEXT NOT NULL,
            dst TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')))''')
    except Exception:
        pass


async def apply() -> dict:
    """Tag every actionable stray (originals backed up) and relocate root-level
    strays into the artist's folder. Files already in an artist folder are tagged
    in place. Never overwrites an existing destination file. EVERY tagged file is
    recorded in file_moves (dst == src when tagged in place) so undo() can reverse
    both the move and the tag write."""
    import asyncio
    p = await plan()
    tagged = moved = 0
    recorded = []  # (src, dst) for every tagged file — written once, at the end
    for it in p['items']:
        if not it['actionable']:
            continue
        src = it['path']
        if not os.path.isfile(src):
            continue
        # _apply_one_sync opens+commits+closes its own connection per file, so
        # only one writer is ever active. We deliberately do NOT hold a second
        # (file_moves) connection open across this loop — that caused SQLite
        # 'database is locked'. Record the pairs and write them once below.
        await asyncio.to_thread(tagfix._apply_one_sync, src,
                                it['artist'], it['title'], it['artist'], it['album'])
        tagged += 1
        dst = src
        # Relocate only if the stray sits in the music root — files already under
        # an artist folder stay put (they're already "in the artist's folder").
        if it['in_root']:
            dest_dir = os.path.join(MUSIC_DIR, _safe(it['artist']), _safe(it['album']))
            candidate = os.path.join(dest_dir, os.path.basename(src))
            if os.path.realpath(candidate) != os.path.realpath(src) and not os.path.exists(candidate):
                os.makedirs(dest_dir, exist_ok=True)
                shutil.move(src, candidate)
                dst = candidate
                moved += 1
        recorded.append((src, dst))
    conn = _conn()
    _ensure_moves(conn)
    conn.executemany('INSERT INTO file_moves (src, dst) VALUES (?, ?)', recorded)
    conn.commit()
    conn.close()
    return {'tagged': tagged, 'moved': moved}


async def undo() -> dict:
    """Reverse the cleanup: move relocated files back, then restore original tags
    for EVERY file the cleanup touched (moved or tagged in place). Restores the
    newest snapshot per path (the pre-cleanup state) and clears it, leaving any
    older auto-tagger backups intact."""
    import asyncio
    import json
    conn = _conn()
    _ensure_moves(conn)
    rows = conn.execute('SELECT id, src, dst FROM file_moves ORDER BY id DESC').fetchall()
    moved_back = 0
    touched, done_ids = [], []
    for mid, src, dst in rows:
        if dst != src and os.path.isfile(dst) and not os.path.exists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dst, src)
            moved_back += 1
        touched.append(src)
        done_ids.append(mid)
    if done_ids:
        conn.execute('DELETE FROM file_moves WHERE id IN (%s)'
                     % ','.join('?' * len(done_ids)), done_ids)
        conn.commit()
    restored, del_ids = 0, []
    for path in dict.fromkeys(touched):  # unique, order-preserving
        if not os.path.isfile(path):
            continue
        row = conn.execute(
            'SELECT id, original FROM tag_backups WHERE path = ? ORDER BY id DESC LIMIT 1',
            (path,)).fetchone()
        if row:
            await asyncio.to_thread(tagfix._restore_one_sync, path, json.loads(row[1]))
            del_ids.append(row[0])
            restored += 1
    if del_ids:
        conn.execute('DELETE FROM tag_backups WHERE id IN (%s)'
                     % ','.join('?' * len(del_ids)), del_ids)
        conn.commit()
    conn.close()
    return {'moved_back': moved_back, 'tags_restored': restored}
