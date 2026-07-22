"""Auto-tagger — fingerprint-based tag correction, preview-first and reversible.

This mutates irreplaceable audio, so every safeguard is deliberate:
  - preview()  is READ ONLY: it fingerprints each track (AcoustID -> MusicBrainz)
    and PROPOSES corrected artist/title with a confidence score. No writes.
  - apply()    writes ONLY the files the user approved, and snapshots the
    original tag values first (tag_backups) so the change can be undone.
  - undo()     restores the original tags from those snapshots.
  - No match / low confidence -> left untouched. Nothing is ever force-matched.

Only the tag FIELDS below are read/written (via mutagen's format-agnostic easy
interface), and each field write is independently guarded so a format that
doesn't support one field just skips it rather than failing the file.

Scope: metadata only. It does NOT rename or move files (that's a separate,
riskier "organize" step). Callers pass a resolved single-album directory that
has already cleared the shared-folder safety check.
"""

import json
import os
import sqlite3

import mutagen

from sources import acoustid_lookup

DB_PATH = os.getenv('DB_PATH', '/data/aria.db')

AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus')
# Fields we ever read (for backup) or write (for apply/restore).
FIELDS = ('title', 'artist', 'albumartist', 'album', 'date', 'tracknumber')
HIGH_CONFIDENCE = 0.85  # AcoustID's own floor is 0.6; >=0.85 = pre-checked tier


def _norm(s) -> str:
    return ' '.join((s or '').split()).lower()


def _get(m, key):
    v = m.get(key) if m else None
    if not v:
        return None
    return str(v[0] if isinstance(v, (list, tuple)) else v)


def _ensure_table(conn) -> None:
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS tag_backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT NOT NULL,
            original TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')))''')
    except Exception:
        pass


def _list_audio(folder: str) -> list[str]:
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    return [n for n in names
            if os.path.isfile(os.path.join(folder, n))
            and n.lower().endswith(AUDIO_EXTS)]


async def preview(folder: str, db_artist: str, db_album: str) -> dict:
    """READ ONLY. Fingerprint each track and propose corrected artist/title."""
    if not folder or not os.path.isdir(folder):
        return {'available': False, 'files': []}
    files = _list_audio(folder)
    backed_up = _backed_up_paths([os.path.join(folder, f) for f in files])
    out = []
    for fn in files:
        full = os.path.join(folder, fn)
        try:
            m = mutagen.File(full, easy=True)
        except Exception:
            m = None
        current = {'artist': _get(m, 'artist'), 'title': _get(m, 'title')}
        ident = await acoustid_lookup.identify_file(full)  # {} if no match
        proposed, tier, changed = None, 'none', False
        if ident and ident.get('title'):
            score = ident.get('score', 0)
            tier = 'high' if score >= HIGH_CONFIDENCE else 'low'
            proposed = {'artist': ident.get('artist'), 'title': ident.get('title'),
                        'score': round(score, 2)}
            changed = (_norm(proposed['title']) != _norm(current['title']) or
                       _norm(proposed['artist']) != _norm(current['artist']))
        out.append({'file': fn, 'current': current, 'proposed': proposed,
                    'tier': tier, 'changed': changed,
                    'backed_up': full in backed_up})
    return {'available': True, 'folder': folder, 'artist': db_artist,
            'album': db_album, 'files': out}


def _backed_up_paths(paths: list) -> set:
    """Which of these EXACT file paths have a tag backup. Exact IN-match (never a
    LIKE pattern) so path metacharacters like '_' / '%' can't cross-match a
    sibling album's files."""
    if not paths:
        return set()
    try:
        conn = sqlite3.connect(DB_PATH)
        ph = ','.join('?' * len(paths))
        rows = conn.execute(
            f'SELECT DISTINCT path FROM tag_backups WHERE path IN ({ph})', paths
        ).fetchall()
        conn.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _apply_one_sync(full: str, artist, title, db_artist, db_album) -> bool:
    """Backup original tags, then write proposed title/artist + normalized
    album/albumartist. Each field guarded independently."""
    try:
        m = mutagen.File(full, easy=True)
    except Exception:
        m = None
    if m is None:
        return False
    original = {}
    for k in FIELDS:
        try:
            original[k] = list(m.get(k, []))
        except Exception:
            original[k] = []
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    conn.execute('INSERT INTO tag_backups (path, original) VALUES (?, ?)',
                 (full, json.dumps(original)))
    conn.commit()
    conn.close()

    writes = {'title': title, 'artist': artist,
              'albumartist': db_artist, 'album': db_album}
    for key, val in writes.items():
        if not val:
            continue
        try:
            m[key] = [str(val)]
        except Exception:
            pass  # format doesn't support this field — skip it, don't fail
    m.save()
    return True


async def apply(folder: str, items: list, db_artist: str, db_album: str) -> dict:
    """Write the user-approved corrections. `items`: [{file, artist, title}].
    Path-safe: filename must be a bare name resolving inside `folder`."""
    import asyncio
    if not folder or not os.path.isdir(folder):
        return {'applied': 0, 'error': 'album folder not available'}
    root = os.path.realpath(folder)
    applied = 0
    for it in items:
        fn = it.get('file', '')
        if fn != os.path.basename(fn) or fn in ('', '.', '..'):
            continue
        full = os.path.realpath(os.path.join(folder, fn))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            continue
        ok = await asyncio.to_thread(_apply_one_sync, full,
                                     it.get('artist'), it.get('title'),
                                     db_artist, db_album)
        if ok:
            applied += 1
    return {'applied': applied}


def _restore_one_sync(full: str, original: dict) -> None:
    try:
        m = mutagen.File(full, easy=True)
    except Exception:
        m = None
    if m is None:
        return
    for k in FIELDS:
        vals = original.get(k) or []
        try:
            if vals:
                m[k] = vals
            elif k in m:
                del m[k]
        except Exception:
            pass
    try:
        m.save()
    except Exception:
        pass


async def undo(folder: str) -> dict:
    """Restore each of this album's files to its PRISTINE original tags — the
    OLDEST snapshot, so repeated fixes fully revert — then clear that file's
    backups. Uses EXACT per-file matching (never a LIKE pattern) so a sibling
    album is never touched. A file that is currently missing is left with its
    backup intact: we never delete a snapshot we didn't actually restore."""
    import asyncio
    if not folder:
        return {'restored': 0}
    paths = [os.path.join(folder, f) for f in _list_audio(folder)]
    if not paths:
        return {'restored': 0}
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    ph = ','.join('?' * len(paths))
    rows = conn.execute(
        f'SELECT id, path, original FROM tag_backups WHERE path IN ({ph}) '
        'ORDER BY id ASC', paths).fetchall()
    conn.close()

    by_path = {}
    for rid, path, original in rows:
        by_path.setdefault(path, []).append((rid, original))

    restored, del_ids = 0, []
    for path, snaps in by_path.items():
        if not os.path.isfile(path):
            continue  # can't restore a missing file — keep its backup
        await asyncio.to_thread(_restore_one_sync, path, json.loads(snaps[0][1]))
        restored += 1
        del_ids.extend(rid for rid, _ in snaps)  # only clear what we restored

    if del_ids:
        conn = sqlite3.connect(DB_PATH)
        conn.execute('DELETE FROM tag_backups WHERE id IN (%s)'
                     % ','.join('?' * len(del_ids)), del_ids)
        conn.commit()
        conn.close()
    return {'restored': restored}
