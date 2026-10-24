from __future__ import annotations

import asyncio
import ipaddress
import json
import mimetypes
import os
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from downloader import (
    AUDIO_FORMATS,
    COOKIE_FILE,
    COOKIES_DIR,
    DOWNLOAD_DIR,
    VIDEO_QUALITIES,
    JobBusyError,
    JobManager,
    _url_host_blocked,
    probe,
)
from app_paths import data_dir, is_frozen, resource_path
from version import APP_NAME, __version__

BASE_DIR = Path(__file__).resolve().parent
# Frozen EXE: static/ lives inside the bundle (_MEIPASS), never next to source.
STATIC_DIR = resource_path("static") if is_frozen() else BASE_DIR / "static"

# Ephemeral state: wipe any legacy persisted queue from previous versions so
# no download history survives a refresh/restart via disk.
_legacy_dirs = [BASE_DIR]
if is_frozen():
    try:
        _legacy_dirs.append(data_dir())
    except Exception:
        pass
for _base in _legacy_dirs:
    for _legacy in ("jobs.db", "jobs.db-journal", "jobs.db-wal", "jobs.db-shm"):
        try:
            _p = _base / _legacy
            if _p.is_file():
                _p.unlink()
        except OSError:
            pass

# Server-Sent Events tuning: at most 4 pushes/second per client, plus an
# immediate push whenever any job changes status, and a keep-alive frame.
SSE_MIN_INTERVAL = 0.25
SSE_HEARTBEAT = 15.0

app = FastAPI(title=APP_NAME, version=__version__)
# In-memory only — no database. Refreshing the browser wipes the visible
# list (frontend keeps it in memory only); restarting the server wipes all.
manager = JobManager()

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_DIR.mkdir(parents=True, exist_ok=True)


class InfoRequest(BaseModel):
    url: str = Field(min_length=4, max_length=2048)


class DownloadRequest(BaseModel):
    urls: List[str] = Field(min_length=1, max_length=50)
    mode: str = "video"
    quality: str = "best"
    playlist: bool = False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "app": APP_NAME, "version": __version__}


@app.post("/api/info")
def get_info(req: InfoRequest, request: Request) -> dict:
    if _rate_limited(f"info:{_client_key(request)}", limit=30, window=60.0):
        raise HTTPException(status_code=429, detail="Too many preview requests — wait a bit.")
    url = req.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Enter a valid link starting with http(s)://")
    return probe(url)


@app.post("/api/upload-cookies")
async def upload_cookies(file: UploadFile = File(...)) -> dict:
    name = (file.filename or "").lower()
    if not name.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Please upload a cookies .txt file")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > 1_000_000:
        raise HTTPException(status_code=400, detail="Cookies file too large (max 1 MB)")
    try:
        head = data[:4096].decode("utf-8", "ignore")
    except Exception:
        head = ""
    if "# Netscape HTTP Cookie File" not in head and "TRUE" not in head and "FALSE" not in head:
        raise HTTPException(status_code=400, detail="Not a valid cookies.txt (Netscape format expected)")
    COOKIES_DIR.mkdir(exist_ok=True)
    # Atomic write so concurrent yt-dlp readers never see a truncated file.
    tmp = COOKIE_FILE.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, COOKIE_FILE)
    return {"ok": True, "file": COOKIE_FILE.name, "size": len(data)}


# Lightweight in-memory throttling: per-IP sliding window to stop
# accidental or abusive request storms from exhausting yt-dlp threads.
import threading as _rate_threading
_RATE: dict[str, list[float]] = {}
_RATE_THREAD_LOCK = _rate_threading.Lock()

def _rate_limited(key: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    with _RATE_THREAD_LOCK:
        # Bound the table so a stream of unique client IPs (or spoofed
        # forwarded addresses) cannot grow it without limit.
        if len(_RATE) > 1024:
            stale = [k for k, hits in _RATE.items() if not hits or now - hits[-1] >= window]
            for k in stale:
                _RATE.pop(k, None)
        hits = [t for t in _RATE.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _RATE[key] = hits
            return True
        hits.append(now)
        _RATE[key] = hits
        return False


def _client_key(request: Request) -> str:
    try:
        return request.client.host if request.client else "unknown"
    except Exception:
        return "unknown"


@app.get("/api/cookies/status")
def cookies_status() -> dict:
    exists = COOKIE_FILE.is_file()
    size = COOKIE_FILE.stat().st_size if exists else 0
    return {"ok": True, "uploaded": exists and size > 0, "size": size}


@app.post("/api/download")
def start_download(req: DownloadRequest, request: Request) -> dict:
    if _rate_limited(f"dl:{_client_key(request)}", limit=20, window=60.0):
        raise HTTPException(status_code=429, detail="Too many download requests — wait a bit.")
    if req.mode not in ("video", "audio"):
        raise HTTPException(status_code=400, detail="mode must be 'video' or 'audio'")
    if req.mode == "video" and req.quality not in VIDEO_QUALITIES:
        raise HTTPException(status_code=400, detail="Unknown video quality")
    if req.mode == "audio" and req.quality not in AUDIO_FORMATS:
        raise HTTPException(status_code=400, detail="Unknown audio format")

    urls = []
    for raw in req.urls:
        url = raw.strip()
        if not url or url in urls:
            continue
        if not url.lower().startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"Invalid link: {url[:80]}")
        # SSRF guard: refuse URLs whose host resolves to loopback / private /
        # link-local addresses before handing them to yt-dlp.
        if _url_host_blocked(url):
            raise HTTPException(status_code=400, detail=f"Blocked link (private or unresolvable host): {url[:80]}")
        urls.append(url)
    if not urls:
        raise HTTPException(status_code=400, detail="No links provided")

    jobs = [manager.create(url, req.mode, req.quality, req.playlist) for url in urls]
    return {"jobs": [job.to_dict() for job in jobs]}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {"jobs": [job.to_dict() for job in manager.list()]}


@app.get("/api/events")
async def job_events(request: Request) -> StreamingResponse:
    """Push job snapshots over SSE whenever something actually changed."""

    async def stream():
        yield "retry: 3000\n\n"
        last_payload: str | None = None
        last_statuses: dict = {}
        last_sent = 0.0
        while True:
            if await request.is_disconnected():
                break
            version = manager.version()
            jobs = [job.to_dict() for job in manager.list()]
            payload = json.dumps({"jobs": jobs}, separators=(",", ":"))
            statuses = {job["id"]: job["status"] for job in jobs}
            now = time.monotonic()

            if payload != last_payload:
                if statuses != last_statuses or now - last_sent >= SSE_MIN_INTERVAL:
                    yield f"data: {payload}\n\n"
                    last_payload, last_statuses = payload, statuses
                    last_sent = now
                else:
                    await asyncio.sleep(max(0.0, SSE_MIN_INTERVAL - (now - last_sent)))
                    continue

            updated = await asyncio.to_thread(manager.wait_for_change, version, SSE_HEARTBEAT)
            if await request.is_disconnected():
                break
            if updated == version:
                yield ": ping\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str) -> dict:
    old = manager.get(job_id)
    if not old:
        raise HTTPException(status_code=404, detail="Job not found")
    job = manager.create(old.url, old.mode, old.quality, old.playlist)
    return {"job": job.to_dict(), "retried_from": old.id}


@app.post("/api/jobs/{job_id}/pause")
def pause_job(job_id: str) -> dict:
    """Pause an active download, keeping partial data for resume (--continue)."""
    job = manager.pause(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True, "job": job.to_dict()}


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str) -> dict:
    """Resume a paused/stopped/failed download from where it left off."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        resumed = manager.resume(job_id)
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not resumed:
        raise HTTPException(status_code=404, detail="Job not found")
    # Success means the worker was started (or the job was already active).
    # Do not inspect `running`/`status` afterwards: the new worker may finish
    # a tiny file before this response is built, which previously produced a
    # bogus 409 while the download had actually resumed.
    return {"ok": True, "job": resumed.to_dict()}


@app.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str) -> dict:
    """Stop an active download (keeps partial data so it can be resumed)."""
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    manager.cancel(job_id)
    current = manager.get(job_id)
    return {"ok": True, "job": current.to_dict() if current else None}


@app.get("/api/jobs/{job_id}/file")
def get_file(job_id: str, request: Request):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    path = Path(job.filename) if job.filename else None
    if not path or not path.is_file():
        raise HTTPException(status_code=404, detail="File not ready")
    resolved = path.resolve()
    if not resolved.is_relative_to(DOWNLOAD_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Forbidden")
    name = resolved.name
    ascii_fallback = name.encode("ascii", "replace").decode("ascii").replace('"', "")
    quoted = urllib.parse.quote(name)
    # Content-Disposition: attachment forces the browser to save straight to
    # the user's default Downloads folder (used by the auto-save flow).
    disposition = f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted}'
    media_type, _ = mimetypes.guess_type(name)
    media_type = media_type or "application/octet-stream"
    file_size = resolved.stat().st_size

    # HTTP Range support: resumable/chunked delivery of the finished file.
    # The frontend auto-save uses `Range: bytes=<offset>-` with
    # AbortController so an interrupted save can resume where it stopped.
    range_header = request.headers.get("range")
    if range_header:
        try:
            units, _, spec = range_header.partition("=")
            if units.strip().lower() != "bytes":
                raise ValueError("unsupported units")
            start_s, _, end_s = spec.strip().partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
            # Suffix range "bytes=-N" (last N bytes).
            if not start_s and end_s:
                length = int(end_s)
                start = max(0, file_size - length)
                end = file_size - 1
            start = max(0, min(start, file_size - 1)) if file_size else 0
            end = max(start, min(end, file_size - 1)) if file_size else start
            length = end - start + 1

            def _chunk_stream():
                with open(resolved, "rb") as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        data = fh.read(min(1024 * 256, remaining))
                        if not data:
                            break
                        remaining -= len(data)
                        yield data

            return StreamingResponse(
                _chunk_stream(),
                status_code=206,
                media_type=media_type,
                headers={
                    "Content-Disposition": disposition,
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                },
            )
        except (ValueError, OSError):
            pass  # fall through to the full-file response
    return FileResponse(
        resolved,
        media_type=media_type,
        filename=resolved.name,
        headers={"Content-Disposition": disposition, "Accept-Ranges": "bytes"},
    )


def _host_blocked(hostname: str) -> bool:
    """True if hostname resolves to loopback/private/link-local/etc."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return True
    for _fam, _, _, _, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return True
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    return False


class _RedirectSignal(Exception):
    def __init__(self, newurl: str):
        super().__init__(newurl)
        self.newurl = newurl


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _RedirectSignal(newurl)


@app.get("/api/thumbnail")
def thumbnail_proxy(url: str = Query(min_length=8, max_length=2048)) -> StreamingResponse:
    current = url
    # Manually follow up to 3 redirects, re-validating each hop's IP so a
    # public URL cannot 302 to 127.0.0.1 / cloud metadata (SSRF).
    for _ in range(4):
        parsed = urllib.parse.urlparse(current)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HTTPException(status_code=400, detail="Invalid thumbnail URL")
        if _host_blocked(parsed.hostname):
            raise HTTPException(status_code=400, detail="Blocked thumbnail host")
        req = urllib.request.Request(
            current,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MediaDownloader/1.0",
                "Accept": "image/*,*/*;q=0.8",
                "Referer": "https://www.youtube.com/",
            },
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=15)
            break
        except _RedirectSignal as sig:
            if not sig.newurl:
                raise HTTPException(status_code=502, detail="Could not fetch thumbnail")
            current = urllib.parse.urljoin(current, sig.newurl)
            continue
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=502, detail="Could not fetch thumbnail")
    else:
        raise HTTPException(status_code=400, detail="Too many thumbnail redirects")
    try:
        try:
            ctype = resp.headers.get_content_type() or ""
            if not (ctype.startswith("image/") or ctype == "application/octet-stream"):
                raise HTTPException(status_code=415, detail="URL is not an image")
            length = resp.headers.get("Content-Length")
            if length and int(length) > 10_000_000:
                raise HTTPException(status_code=413, detail="Thumbnail too large")
            data = resp.read(10_000_000 + 1)
            if len(data) > 10_000_000:
                raise HTTPException(status_code=413, detail="Thumbnail too large")
            media = ctype if ctype.startswith("image/") else "image/jpeg"
        finally:
            resp.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=502, detail="Could not fetch thumbnail")

    async def stream():
        yield data

    return StreamingResponse(
        stream(),
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400", "Content-Length": str(len(data))},
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, drop: bool = Query(default=True)) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    manager.cancel(job_id)
    if drop:
        if not manager.remove(job_id):
            raise HTTPException(status_code=409, detail="Still stopping — try again in a few seconds.")
        return {"ok": True, "removed": job_id, "status": None}
    current = manager.get(job_id)
    return {"ok": True, "removed": None, "status": current.status if current else None}


@app.delete("/api/jobs")
def clear_jobs(status: str | None = Query(default=None)) -> dict:
    if status in ("failed", "error"):
        return {"ok": True, "removed": manager.clear_failed()}
    return {"ok": True, "removed": manager.clear_done()}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
