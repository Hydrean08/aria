# Aria

Self-hosted music management server with an **AI-driven recommendation and curation engine** powered by a local LLM (Ollama). FastAPI + SQLite + a pluggable source layer for metadata and downloads.

## What makes it interesting

The recommendation engine in [`ai_suggest.py`](ai_suggest.py) is the centerpiece. It runs a local LLM (`glm4:9b` on Ollama by default — chosen over Llama 3 8B based on the Nov 2025 Music Era benchmark for recommendation tasks) and does four things:

- **Weekly artist discovery** — given your library, suggests 8 new artists you'd likely enjoy, each tied to a specific existing artist as the "why"
- **Themed playlist generation** — picks a mood that fits your collection and produces a 12-track playlist
- **Mood-based playlists on demand** — give it a vibe ("rainy sunday afternoon", "high-energy workout") and it builds a playlist that prefers your library but reaches outside when the library doesn't cover the mood
- **Library digests** — a short critic-style note about your collection's dominant style, an interesting pattern, and a direction to explore

All of it runs against a private local Ollama instance — no cloud calls, no listening data leaves the host.

## Other features

- **Multi-source metadata & downloads** — Deezer, Spotify, Tidal, Qobuz (via Spotiflac), Soulseek (via slskd), YouTube Music
- **Cross-lookup** — MusicBrainz, AcoustID (fingerprint), Discogs for accurate tagging
- **Plex integration** — read existing Plex library to seed the recommendation engine
- **Scheduled processor** — background cycle on a configurable interval picks up new artists, fetches metadata, downloads, tags, and files into your music tree
- **Web dashboard** — single-page UI (vanilla HTML/CSS/JS, no build step) with search, browse, playlists, and AI suggestions
- **Health endpoint** — `/health` flags silent stalls (scheduler wedged on a hung await)
- **API key auth** — optional `ARIA_API_KEY` env var protects the management endpoints
- **Dockerized** — single-container deploy with the included `Dockerfile`

## Stack

- **Backend:** Python 3.12, FastAPI, SQLite, httpx (async)
- **Frontend:** Single-file HTML/CSS/JS, no framework
- **AI:** Local Ollama (`glm4:9b` default), strict JSON output prompts
- **Audio tooling:** ffmpeg, chromaprint (libchromaprint-tools)

## Setup

```bash
git clone https://github.com/Hydrean08/aria
cd aria

# Set your environment (see "Environment variables" below)
cp .env.example .env  # if provided, otherwise edit your compose/env directly

docker build -t aria .
docker run -d \
  --name aria \
  -p 7171:8000 \
  -v /your/music/dir:/music \
  -v /your/downloads/dir:/downloads \
  -v aria-data:/data \
  --env-file .env \
  aria
```

Open `http://localhost:7171` in your browser.

## Environment variables

| Variable | Purpose | Required for |
|---|---|---|
| `MUSIC_DIR` | Path to your music library inside the container | Core |
| `DOWNLOADS_DIR` | Staging dir for in-progress downloads | Core |
| `DB_PATH` | SQLite database path | Core |
| `INTERVAL` | Seconds between scheduler cycles (default 3600) | Core |
| `ARIA_API_KEY` | Optional API key for management endpoints | Recommended |
| `DEEMIX_ARL` | Your personal Deezer ARL cookie | Deezer downloads |
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Spotify app credentials | Spotify metadata |
| `DISCOGS_TOKEN` | Discogs personal access token | Discogs lookups |
| `PLEX_URL`, `PLEX_TOKEN` | Plex server URL and auth token | Plex library seeding |
| `QOBUZ_TOKEN` | Qobuz token (via Spotiflac) | Hi-res lookups |
| `ACOUSTID_API_KEY` | AcoustID application API key | Fingerprint matching |
| `SLSKD_URL`, `SLSKD_API_KEY` | Soulseek daemon URL and API key | Soulseek downloads |

## Disclaimer — read this

Aria is a personal music management tool. It is intended for use with **content the user has the right to access** — for example, music you've purchased, music you have a paid Deezer / Spotify / Qobuz / Tidal subscription to stream, or music in your own existing library that you're organizing.

You provide your own credentials and accounts for any source you want to use. The project does not bundle or distribute music, does not provide accounts, and does not encourage circumventing the terms of service of any music provider. **Respect the laws and licensing terms in your jurisdiction.**

If you don't have a legitimate basis for accessing a given source, don't configure that source. Most of the value of Aria is in the AI engine and library management — those work against your existing Plex library or local files without any third-party downloads.

## License

MIT — see [LICENSE](LICENSE).
