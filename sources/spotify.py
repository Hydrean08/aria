"""
Spotify catalog source — uses the client_credentials grant (no user account).

Credentials come from SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET env vars.
There are no embedded fallbacks — if the env vars are missing, every public
function returns an empty result and logs a warning. Previously this module
shipped base64-encoded credentials in source, which leaked into anyone with
repo access; that pattern is gone.
"""
import asyncio
import base64
import logging
import os
import time

import httpx

_log = logging.getLogger(__name__)

_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
_TOKEN_URL     = "https://accounts.spotify.com/api/token"
_API_BASE      = "https://api.spotify.com/v1"

_session: httpx.AsyncClient | None = None
_token: str = ""
_token_exp: float = 0.0
_token_lock: asyncio.Lock | None = None
_creds_warned: bool = False


def _client() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(timeout=10)
    return _session


def _get_lock() -> asyncio.Lock:
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()
    return _token_lock


def _have_creds() -> bool:
    global _creds_warned
    if _CLIENT_ID and _CLIENT_SECRET:
        return True
    if not _creds_warned:
        _log.warning(
            "Spotify credentials missing — set SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET to enable catalog + discovery features."
        )
        _creds_warned = True
    return False


async def _ensure_token() -> str:
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    async with _get_lock():
        # Re-check under the lock to avoid a thundering-herd refresh when many
        # concurrent calls arrive after the token expires.
        if _token and time.time() < _token_exp - 60:
            return _token
        auth = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
        r = await _client().post(
            _TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        r.raise_for_status()
        body = r.json()
        _token = body["access_token"]
        _token_exp = time.time() + body.get("expires_in", 3600)
        return _token


async def _get(path: str, *, params: dict | None = None) -> dict:
    """Authenticated GET against the Spotify Web API. Retries 429 once with
    the server-supplied Retry-After delay."""
    token = await _ensure_token()
    r = await _client().get(
        f"{_API_BASE}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    if r.status_code == 429:
        delay = int(r.headers.get("Retry-After", 5)) + 1
        await asyncio.sleep(delay)
        token = await _ensure_token()
        r = await _client().get(
            f"{_API_BASE}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    r.raise_for_status()
    return r.json()


def _artist_card(a: dict) -> dict:
    """Shape an artist JSON object into Aria's standard dict."""
    return {
        "spotify_id": a["id"],
        "name":       a["name"],
        "image_url":  a["images"][0]["url"] if a.get("images") else None,
        "genres":     a.get("genres", []),
    }


async def search_artists(query: str, limit: int = 12) -> list[dict]:
    if not _have_creds():
        return []
    try:
        data = await _get("/search", params={"q": query, "type": "artist", "limit": limit})
        return [_artist_card(a) for a in data.get("artists", {}).get("items", [])]
    except Exception:
        return []


async def genre_artists(genre_slug: str, limit: int = 10) -> list[dict]:
    if not _have_creds():
        return []
    try:
        data  = await _get("/search", params={"q": f"genre:{genre_slug}", "type": "artist", "limit": 50})
        items = [a for a in data.get("artists", {}).get("items", []) if a.get("images")]
        ranked = sorted(items, key=lambda a: a.get("popularity", 0), reverse=True)
        return [_artist_card(a) for a in ranked[:limit]]
    except Exception:
        return []


async def search_artist(name: str) -> dict | None:
    """Exact-name preferred, falls back to the first result."""
    if not _have_creds():
        return None
    try:
        results = await search_artists(name, 5)
        if not results:
            return None
        for r in results:
            if r["name"].lower() == name.lower():
                return r
        return results[0]
    except Exception:
        return None


async def get_artist_albums(spotify_id: str) -> list[dict]:
    if not _have_creds():
        return []
    try:
        albums: list[dict] = []
        seen: set[str] = set()
        path = f"/artists/{spotify_id}/albums?include_groups=album,single&limit=50"
        while path:
            data = await _get(path)
            for item in data.get("items", []):
                sid = item["id"]
                if sid in seen:
                    continue
                seen.add(sid)
                albums.append({
                    "spotify_id":  sid,
                    "title":       item["name"],
                    "year":        (item.get("release_date") or "")[:4],
                    "track_count": item.get("total_tracks", 0),
                    "cover_url":   item["images"][0]["url"] if item.get("images") else None,
                    "record_type": item.get("album_type", "album"),
                })
            nxt = data.get("next") or ""
            path = nxt.replace(_API_BASE + "/", "") if nxt else ""
        return albums
    except Exception:
        return []


async def get_top_tracks(spotify_id: str) -> list[dict]:
    if not _have_creds():
        return []
    try:
        data = await _get(f"/artists/{spotify_id}/top-tracks", params={"market": "US"})
        return [
            {
                "id":           t["id"],
                "title":        t["name"],
                "album":        t.get("album", {}).get("name", ""),
                "track_number": t.get("track_number", i + 1),
                "duration_ms":  t.get("duration_ms", 0),
            }
            for i, t in enumerate(data.get("tracks", []))
        ]
    except Exception:
        return []


async def get_related_artists(spotify_id: str) -> list[dict]:
    if not _have_creds():
        return []
    try:
        data = await _get(f"/artists/{spotify_id}/related-artists")
        return [_artist_card(a) for a in data.get("artists", [])]
    except Exception:
        return []


async def get_album_tracks(spotify_id: str) -> list[dict]:
    if not _have_creds():
        return []
    try:
        data = await _get(f"/albums/{spotify_id}/tracks", params={"limit": 50})
        return [
            {
                "id":           t["id"],
                "title":        t["name"],
                "track_number": t.get("track_number", i + 1),
                "duration_ms":  t.get("duration_ms", 0),
                "artist":       t.get("artists", [{}])[0].get("name", ""),
            }
            for i, t in enumerate(data.get("items", []))
        ]
    except Exception:
        return []


async def get_track_isrc(spotify_id: str) -> str | None:
    """ISRC for a Spotify track — lets us resolve it to the same recording on
    Deezer (the reliable download source) instead of the flaky SpotiFLAC path."""
    if not _have_creds():
        return None
    try:
        data = await _get(f"/tracks/{spotify_id}")
        return (data.get("external_ids") or {}).get("isrc")
    except Exception:
        return None


async def search_album(artist_name: str, album_title: str) -> str | None:
    if not _have_creds():
        return None
    try:
        data = await _get("/search", params={
            "q": f"artist:{artist_name} album:{album_title}",
            "type": "album",
            "limit": 1,
        })
        items = data.get("albums", {}).get("items", [])
        return items[0]["id"] if items else None
    except Exception:
        return None
