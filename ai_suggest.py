import asyncio
import json
import logging

import httpx

OLLAMA_URL = "http://ollama-embed:11434/api/chat"
MODEL = "huihui_ai/qwen3-abliterated:8b-v2"
TIMEOUT = 60.0

_log = logging.getLogger(__name__)

_SUGGEST_PROMPT = """\
You are a music discovery assistant. Given this library of artists, suggest 8 artists the listener would likely enjoy that are NOT already in the library.
For each suggestion include the artist name and a one-sentence reason tied to a specific artist in the library.
Respond with ONLY a JSON array, no other text.
Format: [{{"name": "Artist Name", "reason": "Brief reason", "source_artist": "Library artist they resemble"}}]

Library artists:
{artists}"""

_PLAYLIST_PROMPT = """\
You are a music curator. Given this library of artists, create one themed playlist the listener would enjoy.
Pick a mood or theme that fits the collection and suggest 12 tracks (artist + title) from artists in or similar to the library.
Respond with ONLY a JSON object, no other text.
Format: {{"name": "Playlist Name", "description": "One sentence vibe description", "tracks": [{{"artist": "...", "title": "..."}}]}}

Library artists:
{artists}"""


_RETRY_DELAYS = (2.0, 6.0)  # 3 attempts total: 0s, 2s, 6s backoff
_RETRY_EXC = (httpx.TimeoutException, httpx.RemoteProtocolError, httpx.ReadError)


async def _call_ollama(prompt: str) -> str | None:
    """Retry transient Ollama failures — model load, busy queue, brief
    container restart. Without retry one bad moment silently disabled all
    AI suggestions until the next weekly cycle."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.7},
    }
    last_exc: Exception | None = None
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                r = await client.post(OLLAMA_URL, json=payload)
            if 500 <= r.status_code < 600 and attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            r.raise_for_status()
            return r.json()["message"]["content"].strip()
        except _RETRY_EXC as exc:
            last_exc = exc
            if attempt < len(_RETRY_DELAYS):
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            _log.warning("Ollama unavailable after retries: %s", exc)
            return None
        except Exception as exc:
            _log.warning("Ollama call failed (non-retryable): %s", exc)
            return None
    if last_exc:
        _log.warning("Ollama exhausted retries: %s", last_exc)
    return None


def _extract_json(text: str, opener: str) -> str | None:
    start = text.find(opener)
    if start == -1:
        return None
    closer = "]" if opener == "[" else "}"
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


async def suggest_artists(library_artists: list[str]) -> list[dict]:
    """Ask AI to suggest artists similar to the library. Returns list of {name, reason, source_artist}."""
    if not library_artists:
        return []

    sample = library_artists[:60]
    content = await _call_ollama(_SUGGEST_PROMPT.format(artists=", ".join(sample)))
    if not content:
        return []

    raw = _extract_json(content, "[")
    if not raw:
        return []

    try:
        results = json.loads(raw)
        return [
            {
                "artist_name": str(r.get("name", "")),
                "reason": str(r.get("reason", "")),
                "source_artist": str(r.get("source_artist", "")),
            }
            for r in results
            if isinstance(r, dict) and r.get("name")
        ]
    except Exception:
        return []


async def build_playlist(library_artists: list[str]) -> dict | None:
    """Ask AI to create a themed playlist from the library. Returns {name, description, track_list}."""
    if not library_artists:
        return None

    sample = library_artists[:60]
    content = await _call_ollama(_PLAYLIST_PROMPT.format(artists=", ".join(sample)))
    if not content:
        return None

    raw = _extract_json(content, "{")
    if not raw:
        return None

    try:
        result = json.loads(raw)
        return {
            "name": str(result.get("name", "AI Playlist")),
            "description": str(result.get("description", "")),
            "track_list": json.dumps(result.get("tracks", [])),
        }
    except Exception:
        return None
