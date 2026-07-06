"""Navidrome (Subsonic API) export — turn an AI playlist of "artist + title"
strings into a real playlist in Navidrome, matched against the user's library.

Configured via env (set in the compose): NAVIDROME_URL, NAVIDROME_USER,
NAVIDROME_PASS. Uses Subsonic token auth (salted md5) so the password never
travels in a query string. Read-only against the library except createPlaylist.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets

import httpx

_API_VERSION = "1.16.1"
_CLIENT_NAME = "aria"

_session: httpx.AsyncClient | None = None


def _cfg() -> tuple[str, str, str]:
    return (os.getenv("NAVIDROME_URL", "").rstrip("/"),
            os.getenv("NAVIDROME_USER", ""),
            os.getenv("NAVIDROME_PASS", ""))


def is_configured() -> bool:
    url, user, pw = _cfg()
    return bool(url and user and pw)


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None:
        _session = httpx.AsyncClient(timeout=15.0)
    return _session


def _auth_params() -> dict:
    """Fresh salted token per call (Subsonic auth) so the password is never sent."""
    _, user, pw = _cfg()
    salt = secrets.token_hex(8)
    token = hashlib.md5(f"{pw}{salt}".encode()).hexdigest()
    return {"u": user, "t": token, "s": salt,
            "v": _API_VERSION, "c": _CLIENT_NAME, "f": "json"}


async def _get(view: str, params: dict) -> dict:
    url, _, _ = _cfg()
    r = await _client().get(f"{url}/rest/{view}", params={**_auth_params(), **params})
    r.raise_for_status()
    body = r.json().get("subsonic-response", {})
    if body.get("status") != "ok":
        raise RuntimeError(body.get("error", {}).get("message", "subsonic error"))
    return body


_STRIP = re.compile(r"\(feat[^)]*\)|\bfeat\.?\b.*|[\[(].*?[\])]|[^a-z0-9]+", re.IGNORECASE)


def _norm(s: str) -> str:
    """Lowercase, drop feat/parens/punctuation, collapse — for fuzzy equality."""
    return re.sub(r"\s+", " ", _STRIP.sub(" ", (s or "").lower())).strip()


def _artist_overlap(want_artist: str, song_artist: str) -> bool:
    """Lenient artist match — compilations/features vary, but names must overlap."""
    wa, sa = _norm(want_artist), _norm(song_artist)
    if not wa:
        return True
    return wa in sa or sa in wa or bool(set(wa.split()) & set(sa.split()))


def _is_match(want_artist: str, want_title: str, song: dict) -> bool:
    wt, st = _norm(want_title), _norm(song.get("title", ""))
    if not wt or not st:
        return False
    title_ok = wt == st or wt in st or st in wt
    return title_ok and _artist_overlap(want_artist, song.get("artist", ""))


async def search_song_id(artist: str, title: str) -> str | None:
    """Best library match for an artist+title, or None if not present."""
    try:
        body = await _get("search3.view",
                          {"query": f"{artist} {title}".strip(), "songCount": 10,
                           "artistCount": 0, "albumCount": 0})
    except (httpx.HTTPError, RuntimeError):
        return None
    songs = body.get("searchResult3", {}).get("song", []) or []
    # Prefer an exact-ish match; fall back to the first song that passes _is_match.
    for want_exact in (True, False):
        for s in songs:
            if want_exact and _norm(s.get("title", "")) != _norm(title):
                continue
            if _is_match(artist, title, s):
                return s.get("id")
    return None


async def library_artist_names(limit: int = 2000) -> list[str]:
    """Every artist actually present in the Navidrome library (ground truth for
    'from my own music' — a monitored artist with nothing downloaded won't appear)."""
    try:
        body = await _get("getArtists.view", {})
    except (httpx.HTTPError, RuntimeError):
        return []
    out: list[str] = []
    for idx in body.get("artists", {}).get("index", []) or []:
        for a in idx.get("artist", []) or []:
            if a.get("name"):
                out.append(a["name"])
    return out[:limit]


async def library_tracks_for_artists(artist_names: list[str], per_artist: int = 10,
                                     total_cap: int = 80) -> list[dict]:
    """Real OWNED tracks for these artists — the candidate pool the AI curates from.
    Returns [{id, artist, title, album}]. Every entry is a song the user has."""
    pool: list[dict] = []
    seen: set[str] = set()
    for name in artist_names:
        if len(pool) >= total_cap:
            break
        try:
            body = await _get("search3.view", {"query": name, "songCount": per_artist * 3,
                                               "artistCount": 0, "albumCount": 0})
        except (httpx.HTTPError, RuntimeError):
            continue
        got = 0
        for s in body.get("searchResult3", {}).get("song", []) or []:
            if got >= per_artist or len(pool) >= total_cap:
                break
            sid = s.get("id")
            if not sid or sid in seen or not _artist_overlap(name, s.get("artist", "")):
                continue
            seen.add(sid)
            pool.append({"id": sid, "artist": s.get("artist", ""),
                         "title": s.get("title", ""), "album": s.get("album", "")})
            got += 1
    return pool


async def create_playlist(name: str, song_ids: list[str]) -> str | None:
    """Create a Navidrome playlist; returns its id (or None)."""
    params = {"name": name}
    body = await _get("createPlaylist.view", {**params, "songId": song_ids})
    return (body.get("playlist") or {}).get("id")


async def export_tracks(name: str, tracks: list[dict]) -> dict:
    """Match each {artist,title} to the library and create the playlist from the
    hits. Returns a report the UI can show verbatim."""
    if not is_configured():
        raise RuntimeError("Navidrome is not configured (set NAVIDROME_URL/USER/PASS)")
    matched_ids: list[str] = []
    missing: list[dict] = []
    for t in tracks:
        artist, title = (t.get("artist") or "").strip(), (t.get("title") or "").strip()
        if not title:
            continue
        # Grounded playlists already carry the library song id — no re-matching needed.
        sid = t.get("nav_id") or await search_song_id(artist, title)
        if sid:
            matched_ids.append(sid)
        else:
            missing.append({"artist": artist, "title": title})
    playlist_id = None
    if matched_ids:
        playlist_id = await create_playlist(name, matched_ids)
    return {
        "playlist_name": name,
        "navidrome_playlist_id": playlist_id,
        "matched": len(matched_ids),
        "total": len([t for t in tracks if (t.get("title") or "").strip()]),
        "missing": missing,
    }
