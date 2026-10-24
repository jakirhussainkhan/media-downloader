# Media Downloader

**Version 1.0.0 — stable release**

A small self-hosted web app for downloading media from **YouTube, Facebook, Instagram, TikTok, X (Twitter) and Pinterest**.

Built with **FastAPI + yt-dlp**, single-page UI, live progress bars.

## Features

- Supports YouTube, Facebook, Instagram, TikTok, X (Twitter) and Pinterest
- Paste multiple links at once (batch downloads)
- Video (MP4) or audio only (MP3 / M4A / OPUS)
- Quality selection: best / 4K / 1440p / 1080p / 720p / 480p / 360p
- Optional full-playlist download
- Link preview (title, thumbnail, duration, available resolutions)
- Live progress with speed, ETA and per-item counter for playlists
- Cancel / remove jobs, save finished files straight from the browser
- Finished files are ephemeral: stored outside the project folder (OS temp
  dir, override with `MEDIA_DOWNLOADER_DIR`), auto-deleted when you
  remove/clear the job and expired after 48h (`MEDIA_DOWNLOADER_RETENTION_HOURS`)

## Requirements

- Python 3.11+ (3.12 tested)
- ffmpeg (for merging video+audio streams and extracting audio)

## Quick start

```powershell
.\run.ps1
```

Then open <http://127.0.0.1:8000> on this PC.

### Access from your phone (same Wi-Fi)

The server listens on `0.0.0.0:8000`, so any device on your local network
can reach it via your PC's LAN IP, e.g. `http://192.168.x.x:8000`:

1. Make sure your phone and PC are on the same Wi-Fi.
2. Find your PC's IPv4 address (`ipconfig` → IPv4 Address, e.g. `192.168.1.10`).
3. On your mobile browser, open `http://192.168.1.10:8000`
   (replace with your actual IP).

> Windows may prompt to allow Python/Uvicorn through the firewall on first
> LAN access — click Allow.

Manual start:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### Installing ffmpeg (once, Windows)

```powershell
winget install Gyan.FFmpeg
```

Restart the shell afterwards. The app also auto-detects the winget install location, so no configuration is needed.

## API

| Method | Route | Body / params |
| --- | --- | --- |
| `GET` | `/api/health` | liveness probe — returns app name and version |
| `POST` | `/api/info` | `{"url": "..."}` → title, thumbnail, duration, formats |
| `POST` | `/api/download` | `{"urls": [...], "mode": "video"\|"audio", "quality": "1080"\|"mp3"\|..., "playlist": false}` |
| `GET` | `/api/jobs` | job list + progress |
| `GET` | `/api/events` | Server-Sent Events stream — job updates pushed only when something changes |
| `GET` | `/api/jobs/{id}` | single job |
| `GET` | `/api/jobs/{id}/file` | download the finished file (supports HTTP Range) |
| `POST` | `/api/jobs/{id}/stop` | stop a download, keeping partial data |
| `POST` | `/api/jobs/{id}/pause` | pause a download, keeping partial data |
| `POST` | `/api/jobs/{id}/resume` | resume a paused/stopped/failed job in place |
| `POST` | `/api/jobs/{id}/retry` | start a fresh job from an existing one |
| `DELETE` | `/api/jobs/{id}?drop=true\|false` | remove / cancel a job |
| `DELETE` | `/api/jobs` | clear finished entries (`?status=failed` for failed ones) |
| `POST` | `/api/upload-cookies` | upload a Netscape `cookies.txt` (max 1 MB) |
| `GET` | `/api/cookies/status` | whether cookies have been uploaded |
| `GET` | `/api/thumbnail?url=...` | SSRF-guarded thumbnail proxy |

## Troubleshooting

- **YouTube errors / HTTP 403**: yt-dlp gets updated often, upgrade it first:
  ```powershell
  .\.venv\Scripts\python.exe -m pip install -U yt-dlp
  ```
- **Instagram / Facebook / TikTok / X need login**: some posts are only visible to logged-in users. Export browser cookies with a `cookies.txt` extension and they can be wired into yt-dlp (`cookiefile` option) if needed.
- **Merge fails / "ffmpeg not installed"**: install ffmpeg and restart the terminal.

## Release notes

### v1.0.0 — first stable release

- Rebranded the app as **Media Downloader** (UI, API title, tray app and installer).
- Full project version set to `1.0.0` (API `/api/health`, `pyproject.toml`, installer).
- Stability fixes: deleting a queued job now actually cancels its worker; RSS
  feeds with malformed format heights no longer break link previews; download
  requests are protected by the same private-host (SSRF) guard as previews.
- Frontend: thumbnails refresh in place, and the live update stream recovers
  correctly when the page is restored from the back/forward cache.
- Pause / resume / stop, batch downloads, playlist support and cookie upload
  included.

## Disclaimer

For personal use with content you have the right to download. Respect the terms of service of each platform.
