import asyncio
import json
import logging

import httpx

OLLAMA_URL = "http://ollama-embed:11434/api/chat"
# GLM-4-9B-Chat — Tsinghua/Zhipu open-weights model. Best open-source
# performer for music recommendation/playlist understanding in the Nov 2025
# arxiv benchmark (16.85% HR@1 on Music Era, beating Llama-3-8B and other
# open models). Fits in ~6 GB on Q4_K_M so inference is fast on the 3060.
# Previous value pointed at "huihui_ai/qwen3-abliterated:8b-v2" which was
# never actually installed on ollama-embed — calls silently failed.
MODEL = "glm4:9b"
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

_MOOD_PLAYLIST_PROMPT = """\
You are a music curator. The listener wants a playlist for this mood/theme: "{mood}"
Pick 12 tracks (artist + title) that fit the mood — prefer artists from the library when they fit, but include adjacent artists when the library doesn't cover the mood well.
Respond with ONLY a JSON object, no other text.
Format: {{"name": "Playlist Name", "description": "One sentence about why these fit the mood", "tracks": [{{"artist": "...", "title": "..."}}]}}

Library artists (for taste reference):
{artists}"""

_DIGEST_PROMPT = """\
You are a music critic writing a personal note to the listener about their library. In 3-5 sentences:
- Identify the dominant style/genre/era of the collection
- Call out one interesting pattern or thread
- Suggest one direction they might want to explore next
Be specific — reference actual artists from the library.
Respond with ONLY the prose, no JSON, no markdown.

Library artists:
{artists}"""

_LYRIC_SEARCH_PROMPT = """\
You are a music search assistant. The listener is looking for tracks that match this description: "{query}"
Suggest up to 10 tracks (artist + title) that fit, preferring tracks well-known for that exact theme or feeling.
Respond with ONLY a JSON array, no other text.
Format: [{{"artist": "Artist Name", "title": "Track Title", "reason": "Why this matches"}}]"""

_GENRE_TAG_PROMPT = """\
You are a music classifier. For the artist "{artist}", give 3-6 canonical genre tags in order from most to least specific.
Use lowercase, hyphenated tags from the standard music taxonomy (e.g. "alternative-rock", "post-grunge", "synth-pop", "contemporary-r-and-b").
Respond with ONLY a JSON array of strings, no other text.
Format: ["tag1", "tag2", "tag3"]"""

_ALBUM_PICK_PROMPT = """\
You are a music librarian. The listener has an album on disk titled "{album_title}" by {artist}, and the library DB has these candidate entries that MIGHT be the same album. Decide which DB entry, if any, is the same release.

Rules:
- Prefer EXACT name matches over variants (e.g. "Album" beats "Album (Live)" or "Album (Deluxe)").
- If the disk title is just the base name and a candidate matches exactly, pick it even if other variants exist.
- If NONE of the candidates is clearly the same release as the disk album, abstain with pick_index = -1. Do NOT guess. Variants like "Deluxe Edition", "Live", "Anniversary Remaster", "Our Version" are DIFFERENT releases — only pick them if the disk title also indicates that variant.

Respond with ONLY a JSON object: {{"pick_index": N, "reason": "..."}}
N is the 0-based index from the list, or -1 to abstain.

Candidates:
{candidates}"""

_FILENAME_RANK_PROMPT = """\
You are matching SoulSeek search results to a target track. The target is "{artist} — {title}".
Rank these filenames from BEST to WORST match. Prefer: exact artist+title match, FLAC over MP3, higher bitrate, no "live"/"karaoke"/"cover" tags unless target asks for them, no extra prefixes/suffixes.
Respond with ONLY a JSON array of indices (0-based), best first.
Format: [3, 1, 0, 2]

Filenames:
{filenames}"""

_NEW_RELEASE_FILTER_PROMPT = """\
You are a release curator. From these new albums by artists the listener follows, pick the 5 most interesting ones to highlight. Skip live albums, EPs of B-sides, and re-releases unless they're notable. Prefer studio albums + significant collaborations.
Respond with ONLY a JSON array, no other text.
Format: [{{"artist": "...", "title": "...", "reason": "Why this stands out"}}]

New releases:
{releases}"""


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


async def build_mood_playlist(library_artists: list[str], mood: str) -> dict | None:
    """Mood-driven playlist. Same shape as build_playlist but seeded with the
    listener's free-text mood/theme. Library is taste reference, not a hard
    constraint — model can include adjacent artists when the library doesn't
    cover the requested mood."""
    if not mood or not mood.strip():
        return None
    sample = (library_artists or [])[:60]
    content = await _call_ollama(_MOOD_PLAYLIST_PROMPT.format(
        mood=mood.strip()[:200],  # cap to keep the prompt budget sane
        artists=", ".join(sample) or "(empty library)",
    ))
    if not content:
        return None
    raw = _extract_json(content, "{")
    if not raw:
        return None
    try:
        result = json.loads(raw)
        return {
            "name": str(result.get("name", f"Mood: {mood[:40]}")),
            "description": str(result.get("description", "")),
            "track_list": json.dumps(result.get("tracks", [])),
        }
    except Exception:
        return None


async def library_digest(library_artists: list[str]) -> str | None:
    """Returns a 3-5 sentence narrative about the library — dominant style,
    interesting patterns, suggested direction. Prose, not JSON."""
    if not library_artists:
        return None
    sample = library_artists[:60]
    content = await _call_ollama(_DIGEST_PROMPT.format(artists=", ".join(sample)))
    if not content:
        return None
    # Strip any accidental markdown fences GLM-4 sometimes adds.
    return content.strip().strip("`").strip()


async def lyric_search(query: str) -> list[dict]:
    """Returns [{artist, title, reason}, ...] for a free-text track query."""
    if not query or not query.strip():
        return []
    content = await _call_ollama(_LYRIC_SEARCH_PROMPT.format(query=query.strip()[:200]))
    if not content:
        return []
    raw = _extract_json(content, "[")
    if not raw:
        return []
    try:
        results = json.loads(raw)
        return [
            {
                "artist": str(r.get("artist", "")),
                "title":  str(r.get("title", "")),
                "reason": str(r.get("reason", "")),
            }
            for r in results
            if isinstance(r, dict) and r.get("artist") and r.get("title")
        ]
    except Exception:
        return []


async def auto_genres(artist_name: str) -> list[str]:
    """Returns up to 6 canonical genre tags for an artist. Lowercase, hyphenated."""
    if not artist_name or not artist_name.strip():
        return []
    content = await _call_ollama(_GENRE_TAG_PROMPT.format(artist=artist_name.strip()[:80]))
    if not content:
        return []
    raw = _extract_json(content, "[")
    if not raw:
        return []
    try:
        tags = json.loads(raw)
        return [str(t).lower().strip() for t in tags if isinstance(t, str) and t.strip()][:6]
    except Exception:
        return []


async def pick_album(artist_name: str, album_title: str, candidates: list[dict]) -> int | None:
    """Given multiple album candidates (each a dict with a 'title' and 'year'
    at minimum), pick the canonical studio version. Returns the index of the
    chosen candidate, or None on failure. Returns 0 trivially if only one
    candidate so callers don't need to special-case."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return 0
    formatted = "\n".join(
        f"{i}. {c.get('title', '?')} ({c.get('year', '?')}) — {c.get('source', '?')}"
        for i, c in enumerate(candidates[:10])
    )
    content = await _call_ollama(_ALBUM_PICK_PROMPT.format(
        artist=artist_name.strip()[:80],
        album_title=album_title.strip()[:120],
        candidates=formatted,
    ))
    if not content:
        return None
    raw = _extract_json(content, "{")
    if not raw:
        return None
    try:
        result = json.loads(raw)
        idx = int(result.get("pick_index", -1))
        if 0 <= idx < len(candidates):
            return idx
    except Exception:
        pass
    return None


async def rank_filenames(artist: str, title: str, filenames: list[str]) -> list[int]:
    """Rank Soulseek filename matches best-to-worst. Returns indices into
    `filenames` ordered by quality. Empty list on failure — callers should
    fall back to their existing heuristic."""
    if not filenames or len(filenames) < 2:
        return list(range(len(filenames)))
    # Truncate to keep prompt budget reasonable. Soulseek searches can return
    # 100+ results; ranking the top 20 by heuristic first is the caller's job.
    sample = filenames[:20]
    formatted = "\n".join(f"{i}. {f[:200]}" for i, f in enumerate(sample))
    content = await _call_ollama(_FILENAME_RANK_PROMPT.format(
        artist=artist.strip()[:80],
        title=title.strip()[:120],
        filenames=formatted,
    ))
    if not content:
        return []
    raw = _extract_json(content, "[")
    if not raw:
        return []
    try:
        ranks = json.loads(raw)
        # Validate: all entries are valid indices.
        return [int(i) for i in ranks if isinstance(i, (int, float)) and 0 <= int(i) < len(sample)]
    except Exception:
        return []


async def filter_new_releases(releases: list[dict]) -> list[dict]:
    """Given new releases by monitored artists, return the top 5 with reasons.
    Each release dict should have at least 'artist' and 'title'."""
    if not releases:
        return []
    formatted = "\n".join(
        f"- {r.get('artist', '?')} — {r.get('title', '?')} ({r.get('year', '?')})"
        for r in releases[:30]
    )
    content = await _call_ollama(_NEW_RELEASE_FILTER_PROMPT.format(releases=formatted))
    if not content:
        return []
    raw = _extract_json(content, "[")
    if not raw:
        return []
    try:
        results = json.loads(raw)
        return [
            {
                "artist": str(r.get("artist", "")),
                "title":  str(r.get("title", "")),
                "reason": str(r.get("reason", "")),
            }
            for r in results
            if isinstance(r, dict) and r.get("artist") and r.get("title")
        ][:5]
    except Exception:
        return []
