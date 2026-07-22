"""Library ingestion + reconciliation.

Makes Aria's DB reflect what is physically on disk, so:
  1. Every artist/album you already own becomes known to Aria (not just the
     ones you explicitly added), and
  2. When you later add an artist, the albums you already have show as
     'complete' instead of 'missing' — no needless re-downloads.

Grouping is TAG-DRIVEN, not folder-driven: each audio file's albumartist/album
tags decide which album it belongs to. That survives the messy real-world
layout (loose tracks under an artist folder, "Discography (5 Releases)" dumps,
stray files in the music root) that a strict Artist/Album/ walk would choke on.

Safety:
  - analyze() is READ ONLY (no writes) — powers the dry-run/preview.
  - commit() only ADDS DB rows; it NEVER moves or deletes any audio file.
  - Imported artists are flagged (artists.imported=1) and imported albums carry
    source='imported', so undo() removes exactly what an import added and leaves
    artists you added yourself untouched.

CLI:  python3 ingest.py [--dry-run | --commit | --undo]   (default: --dry-run)
"""

import os
import re
import sqlite3
import sys

import mutagen

MUSIC_DIR = os.getenv('MUSIC_DIR', '/music')
DB_PATH = os.getenv('DB_PATH', '/data/aria.db')

AUDIO_EXTS = ('.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.wav')


def _norm(s: str) -> str:
    """Lossy key for fuzzy equality — lowercase, alphanumerics only."""
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


def _read_tags(path: str):
    """Best-effort (album_artist, album, year) from a file's tags."""
    try:
        m = mutagen.File(path, easy=True)
    except Exception:
        m = None
    if m is None:
        return None, None, None

    def first(*keys):
        for k in keys:
            v = m.get(k)
            if v:
                return (str(v[0]) if isinstance(v, (list, tuple)) else str(v)).strip()
        return None

    album_artist = first('albumartist', 'artist')
    album = first('album')
    year = first('date', 'originaldate', 'year')
    if year:
        mo = re.search(r'(\d{4})', year)
        year = mo.group(1) if mo else None
    return album_artist, album, year


def _pick_folder(folder_counts: dict) -> str:
    """The folder holding the most of this album's tracks (handles albums whose
    tracks are split across dirs)."""
    return max(folder_counts, key=folder_counts.get) if folder_counts else ''


def analyze() -> dict:
    """Walk MUSIC_DIR, group audio files into albums by tag, and reconcile
    against the Aria DB. Returns a report dict. READ ONLY — no writes."""
    total_files = 0
    unclassified = []  # audio files with no usable artist/album tag
    raw = {}           # (norm_artist, norm_album) -> aggregate

    for root, _dirs, files in os.walk(MUSIC_DIR):
        for fn in files:
            if not fn.lower().endswith(AUDIO_EXTS):
                continue
            total_files += 1
            full = os.path.join(root, fn)
            artist, album, year = _read_tags(full)
            if not artist or not album:
                unclassified.append(full)
                continue
            key = (_norm(artist), _norm(album))
            g = raw.get(key)
            if g is None:
                g = {'artist': artist, 'album': album, 'year': year,
                     'tracks': 0, 'folders': {}, 'formats': {}}
                raw[key] = g
            g['tracks'] += 1
            g['folders'][root] = g['folders'].get(root, 0) + 1
            ext = os.path.splitext(fn)[1].lower().lstrip('.')
            g['formats'][ext] = g['formats'].get(ext, 0) + 1
            if not g['year'] and year:
                g['year'] = year

    # Flatten into a stable list, each with a single chosen folder.
    groups = []
    for (na, nal), g in raw.items():
        groups.append({
            'na': na, 'nal': nal,
            'artist': g['artist'], 'album': g['album'], 'year': g['year'],
            'tracks': g['tracks'], 'formats': g['formats'],
            'folder': _pick_folder(g['folders']),
            'folder_count': len(g['folders']),
        })

    # Reconcile against the current DB.
    db_artists = set()
    db_albums = set()
    try:
        conn = sqlite3.connect(DB_PATH)
        db_artists = {_norm(r[0]) for r in conn.execute('SELECT name FROM artists')}
        for name, title in conn.execute(
            'SELECT ar.name, al.title FROM albums al '
            'JOIN artists ar ON ar.id = al.artist_id'
        ):
            db_albums.add((_norm(name), _norm(title)))
        conn.close()
    except Exception as e:
        return {'error': f'DB read failed: {e}'}

    new_artist_albums, known_artist_new_album, already_known = [], [], []
    disk_artists = {}
    for g in groups:
        disk_artists.setdefault(g['na'], g['artist'])
        if g['na'] not in db_artists:
            new_artist_albums.append(g)
        elif (g['na'], g['nal']) not in db_albums:
            known_artist_new_album.append(g)
        else:
            already_known.append(g)

    new_artist_names = sorted(
        {g['na']: g['artist'] for g in new_artist_albums}.values(), key=str.lower)

    return {
        'total_files': total_files,
        'tagged_files': total_files - len(unclassified),
        'unclassified': unclassified,
        'groups': groups,
        'group_count': len(groups),
        'disk_artist_count': len(disk_artists),
        'db_artist_count': len(db_artists),
        'new_artist_albums': new_artist_albums,
        'known_artist_new_album': known_artist_new_album,
        'already_known': already_known,
        'new_artist_names': new_artist_names,
        'multi_folder': [g for g in groups if g['folder_count'] > 1],
    }


def _ensure_columns(conn) -> None:
    """Defensive — the app's migration adds these, but the CLI can run first."""
    for sql in ('ALTER TABLE albums ADD COLUMN folder TEXT',
                'ALTER TABLE artists ADD COLUMN imported INTEGER NOT NULL DEFAULT 0'):
        try:
            conn.execute(sql)
        except Exception:
            pass


def commit() -> dict:
    """Write ingestion to the DB. ADDS rows only — never touches audio files.
    Idempotent: re-running reconciles existing rows instead of duplicating."""
    report = analyze()
    if 'error' in report:
        return report

    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    _ensure_columns(conn)
    cur = conn.cursor()

    art_ids = {_norm(n): i for i, n in cur.execute('SELECT id, name FROM artists')}
    existing = {}  # artist_id -> {norm_title: album_id}
    for alid, aid, title in cur.execute('SELECT id, artist_id, title FROM albums'):
        existing.setdefault(aid, {})[_norm(title)] = alid

    created_artists = created_albums = reconciled = 0
    for g in report['groups']:
        aid = art_ids.get(g['na'])
        if aid is None:
            cur.execute('INSERT INTO artists (name, imported) VALUES (?, 1)', (g['artist'],))
            aid = cur.lastrowid
            art_ids[g['na']] = aid
            existing[aid] = {}
            created_artists += 1
        alid = existing.get(aid, {}).get(g['nal'])
        if alid:
            # Reconcile an album Aria already knows — mark complete + record its
            # real folder. Leave `source` untouched so undo won't delete it.
            cur.execute(
                "UPDATE albums SET status='complete', "
                "folder=COALESCE(NULLIF(folder,''), ?), "
                "track_count=max(track_count, ?) WHERE id=?",
                (g['folder'], g['tracks'], alid))
            reconciled += 1
        else:
            cur.execute(
                "INSERT INTO albums (artist_id, title, year, status, source, "
                "folder, track_count, wanted) VALUES (?,?,?,'complete','imported',?,?,0)",
                (aid, g['album'], g['year'], g['folder'], g['tracks']))
            existing.setdefault(aid, {})[g['nal']] = cur.lastrowid
            created_albums += 1

    conn.commit()
    conn.close()
    return {'artists_created': created_artists,
            'albums_created': created_albums,
            'albums_reconciled': reconciled}


def undo() -> dict:
    """Reverse an import: delete imported albums, then prune the artists the
    import created that now have no albums. Never touches audio; never removes
    artists you added yourself or their albums."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute('PRAGMA foreign_keys = ON')
    _ensure_columns(conn)
    cur = conn.cursor()
    n_alb = cur.execute("DELETE FROM albums WHERE source='imported'").rowcount
    n_art = cur.execute(
        "DELETE FROM artists WHERE imported=1 "
        "AND id NOT IN (SELECT DISTINCT artist_id FROM albums)").rowcount
    conn.commit()
    conn.close()
    return {'albums_deleted': n_alb, 'artists_pruned': n_art}


def _fmt_group(g) -> str:
    fmts = ','.join(f'{k}×{v}' for k, v in sorted(g['formats'].items()))
    yr = f" ({g['year']})" if g['year'] else ''
    return f"{g['artist']} — {g['album']}{yr}  [{g['tracks']} trk, {fmts}]"


def _print_report(r: dict) -> None:
    if 'error' in r:
        print('ERROR:', r['error'])
        return
    line = '=' * 64
    print(line)
    print('ARIA LIBRARY INGEST — DRY RUN (no database writes)')
    print(line)
    print(f'Music dir: {MUSIC_DIR}')
    print(f'Audio files found:        {r["total_files"]}')
    print(f'  tagged (artist+album):  {r["tagged_files"]}')
    print(f'  unclassified (no tags): {len(r["unclassified"])}')
    print()
    print(f'Distinct albums on disk (by tag):  {r["group_count"]}')
    print(f'Distinct artists on disk (by tag): {r["disk_artist_count"]}')
    print(f'Artists currently in Aria DB:      {r["db_artist_count"]}')
    print()
    na_albums = len(r['new_artist_albums'])
    na_artists = len(r['new_artist_names'])
    print('WOULD INGEST:')
    print(f'  NEW artists (unknown to Aria):        {na_albums} albums / {na_artists} artists')
    print(f'  NEW albums for already-added artists: {len(r["known_artist_new_album"])} albums')
    print(f'  Already known (no change):            {len(r["already_known"])} albums')
    print()
    print(f'--- sample NEW artists (first 30 of {na_artists}) ---')
    for name in r['new_artist_names'][:30]:
        print(f'  + {name}')
    print()
    print('--- sample album groups (first 15 new) ---')
    for g in r['new_artist_albums'][:15]:
        print(f'  {_fmt_group(g)}')
    if r['multi_folder']:
        print(f'\nNOTE: {len(r["multi_folder"])} albums have tracks spread across >1 folder.')
    if r['unclassified']:
        print(f'--- unclassified sample (first 12 of {len(r["unclassified"])}) ---')
        for p in r['unclassified'][:12]:
            print(f'  ? {p}')
    print(line)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else '--dry-run'
    if mode == '--commit':
        print('COMMIT:', commit())
    elif mode == '--undo':
        print('UNDO:', undo())
    else:
        _print_report(analyze())
