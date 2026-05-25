"""
Spotify catalog source — uses SpotiFLAC's embedded client credentials.
No user account or sp_dc required; the client_credentials grant auto-refreshes.
"""
import asyncio
import base64
import time
from concurrent.futures import ThreadPoolExecutor

import requests

_CLIENT_ID     = base64.b64decode("ODNlNDQzMGI0NzAwNDM0YmFhMjEyMjhhOWM3ZDExYzU=").decode()
_CLIENT_SECRET = base64.b64decode("OWJiOWUxMzFmZjI4NDI0Y2I2YTQyMGFmZGY0MWQ0NGE=").decode()
_TOKEN_URL     = "https://accounts.spotify.com/api/token"
_API_BASE      = "https://api.spotify.com/v1"

_executor  = ThreadPoolExecutor(max_workers=2)
_session   = requests.Session()
_token     = ""
_token_exp = 0.0


def _ensure_token() -> str:
    global _token, _token_exp
    if _token and time.time() < _token_exp - 60:
        return _token
    auth = base64.b64encode(f"{_CLIENT_ID}:{_CLIENT_SECRET}".encode()).decode()
    resp = _session.post(
        _TOKEN_URL,
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials"},
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    _token = body["access_token"]
    _token_exp = time.time() + body.get("expires_in", 3600)
    return _token


def _get(path: str, **kwargs) -> dict:
    token = _ensure_token()
    resp = _session.get(
        f"{_API_BASE}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
        **kwargs,
    )
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", 5)) + 1)
        return _get(path, **kwargs)
    resp.raise_for_status()
    return resp.json()


def _sync_search_artists(query: str, limit: int) -> list[dict]:
    data  = _get("/search", params={"q": query, "type": "artist", "limit": limit})
    items = data.get("artists", {}).get("items", [])
    return [
        {
            "spotify_id": a["id"],
            "name":       a["name"],
            "image_url":  a["images"][0]["url"] if a.get("images") else None,
            "genres":     a.get("genres", []),
        }
        for a in items
    ]


def _sync_genre_artists(genre_slug: str, limit: int = 10) -> list[dict]:
    data  = _get("/search", params={"q": f"genre:{genre_slug}", "type": "artist", "limit": 50})
    items = data.get("artists", {}).get("items", [])
    artists = sorted(
        [
            {
                "spotify_id": a["id"],
                "name":       a["name"],
                "image_url":  a["images"][0]["url"] if a.get("images") else None,
                "genres":     a.get("genres", []),
                "_p":         a.get("popularity", 0),
            }
            for a in items if a.get("images")
        ],
        key=lambda x: x["_p"],
        reverse=True,
    )
    return [{k: v for k, v in a.items() if k != "_p"} for a in artists[:limit]]


def _sync_search_artist(name: str) -> dict | None:
    results = _sync_search_artists(name, 5)
    if not results:
        return None
    for r in results:
        if r["name"].lower() == name.lower():
            return r
    return results[0]


def _sync_get_artist_albums(artist_id: str) -> list[dict]:
    albums = []
    seen   = set()
    path   = f"/artists/{artist_id}/albums?include_groups=album,single&limit=50"
    while path:
        data = _get(path)
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
        nxt  = data.get("next") or ""
        path = nxt.replace(_API_BASE + "/", "") if nxt else ""
    return albums


def _sync_get_top_tracks(artist_id: str) -> list[dict]:
    data = _get(f"/artists/{artist_id}/top-tracks", params={"market": "US"})
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


def _sync_get_related_artists(artist_id: str) -> list[dict]:
    data = _get(f"/artists/{artist_id}/related-artists")
    return [
        {
            "spotify_id": a["id"],
            "name":       a["name"],
            "image_url":  a["images"][0]["url"] if a.get("images") else None,
            "genres":     a.get("genres", []),
        }
        for a in data.get("artists", [])
    ]


def _sync_get_album_tracks(album_id: str) -> list[dict]:
    data = _get(f"/albums/{album_id}/tracks", params={"limit": 50})
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


def _sync_search_album(artist_name: str, album_title: str) -> str | None:
    data  = _get("/search", params={
        "q": f"artist:{artist_name} album:{album_title}",
        "type": "album", "limit": 1,
    })
    items = data.get("albums", {}).get("items", [])
    return items[0]["id"] if items else None


async def genre_artists(genre_slug: str, limit: int = 10) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_genre_artists, genre_slug, limit)
    except Exception:
        return []


async def search_artists(query: str, limit: int = 12) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_search_artists, query, limit)
    except Exception:
        return []


async def search_artist(name: str) -> dict | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_search_artist, name)
    except Exception:
        return None


async def get_artist_albums(spotify_id: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_get_artist_albums, spotify_id)
    except Exception:
        return []


async def get_top_tracks(spotify_id: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_get_top_tracks, spotify_id)
    except Exception:
        return []


async def get_related_artists(spotify_id: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_get_related_artists, spotify_id)
    except Exception:
        return []


async def get_album_tracks(spotify_id: str) -> list[dict]:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_get_album_tracks, spotify_id)
    except Exception:
        return []


async def search_album(artist_name: str, album_title: str) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_executor, _sync_search_album, artist_name, album_title)
    except Exception:
        return None
