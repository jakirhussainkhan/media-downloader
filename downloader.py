from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app_paths import bundle_dir, data_dir, ensure_dir, is_frozen

import yt_dlp
from yt_dlp.utils import DownloadCancelled, DownloadError

BASE_DIR = Path(__file__).resolve().parent
# Writable data dir: ./cookies in source, %APPDATA%/MediaDownloader when frozen.
_DATA_DIR = data_dir()
_BUNDLE_DIR = bundle_dir()
# Finished files are ephemeral and live OUTSIDE the project folder (OS temp
# dir), so other people's downloads never clutter the project directory.
# Override with MEDIA_DOWNLOADER_DIR env var if a custom location is wanted.
_download_dir_override = os.environ.get("MEDIA_DOWNLOADER_DIR")
DOWNLOAD_DIR = (
    Path(_download_dir_override).expanduser()
    if _download_dir_override
    else Path(tempfile.gettempdir()) / "media-downloader-downloads"
)
# NOTE: Ephemeral state — there is intentionally NO database, NO localStorage,
# NO sessionStorage and NO persisted queue. Everything lives in memory only.
# Frozen EXE: cookies live in %APPDATA%/MediaDownloader/cookies (writable).
# Source run: ./cookies (unchanged behaviour).
COOKIES_DIR = ensure_dir(_DATA_DIR / "cookies") if is_frozen() else BASE_DIR / "cookies"
COOKIE_FILE = COOKIES_DIR / "cookies.txt"

# Ephemeral-file policy: files of finished/failed/cancelled jobs are deleted
# from disk as soon as their job entry is removed/cleared, plus a background
# sweeper deletes leftover files older than this.
def _retention_seconds() -> float:
    try:
        hours = float(os.environ.get("MEDIA_DOWNLOADER_RETENTION_HOURS", "48"))
    except (TypeError, ValueError):
        hours = 48.0
    return max(1.0, hours) * 3600

FILE_RETENTION_SECONDS = _retention_seconds()

MAX_CONCURRENT_DOWNLOADS = 3
MAX_JOBS_KEPT = 100

VIDEO_QUALITIES: Dict[str, Optional[int]] = {
    "best": None,
    "2160": 2160,
    "1440": 1440,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "360": 360,
}
AUDIO_FORMATS = ("mp3", "m4a", "opus")
ACTIVE_STATUSES = ("queued", "downloading", "processing")
PAUSED_STATUS = "paused"
RESUMABLE_STATUSES = ("paused", "cancelled", "error")

# Human-friendly resolution tags appended to video filenames before .mp4.
RESOLUTION_LABELS: Dict[str, str] = {
    "360": "360p",
    "480": "480p",
    "720": "720p HD",
    "1080": "1080p Full HD",
    "1440": "1440p QHD",
    "2160": "4K 2160p",
}


@lru_cache(maxsize=1)
def find_ffmpeg() -> Optional[str]:
    # 1) Bundled next to the EXE / inside _MEIPASS (PyInstaller --add-binary).
    for base in (_BUNDLE_DIR, _BUNDLE_DIR / "bin"):
        for name in ("ffmpeg.exe", "ffmpeg"):
            cand = base / name
            try:
                if cand.is_file():
                    return str(cand.parent)
            except OSError:
                continue
    # 2) EXE folder itself (onedir build / portable zip with bin/).
    if is_frozen():
        try:
            exe_dir = Path(sys.executable).resolve().parent  # noqa: F821
        except Exception:
            exe_dir = None
        if exe_dir is not None:
            for sub in (exe_dir, exe_dir / "bin"):
                cand = sub / "ffmpeg.exe"
                try:
                    if cand.is_file():
                        return str(cand.parent)
                except OSError:
                    continue
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)
    root = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    if root.exists():
        for cand in root.glob("**/ffmpeg.exe"):
            return str(cand.parent)
    return None


@lru_cache(maxsize=1)
def find_aria2c() -> Optional[str]:
    """Return the aria2c binary path when installed, else None.

    When present, yt-dlp uses it as external downloader so HTTP(S)
    downloads support multi-connection + ``--continue`` resume.
    """
    # Bundled copy first (optional — EXE may ship aria2c.exe next to ffmpeg).
    for base in (_BUNDLE_DIR, _BUNDLE_DIR / "bin"):
        cand = base / "aria2c.exe"
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    exe = shutil.which("aria2c")
    if exe:
        return exe
    # Common Windows install locations.
    candidates = [
        Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
        Path("C:/aria2/aria2c.exe"),
        Path("C:/Program Files/aria2/aria2c.exe"),
    ]
    for base in candidates:
        try:
            if base.is_file() and base.name.lower() == "aria2c.exe":
                return str(base)
            if base.is_dir():
                for cand in base.glob("**/aria2c.exe"):
                    return str(cand)
        except OSError:
            continue
    return None


def get_cookiefile() -> Optional[str]:
    """Return the cookies.txt path if one has been uploaded, else None."""
    try:
        if COOKIE_FILE.is_file() and COOKIE_FILE.stat().st_size > 0:
            return str(COOKIE_FILE)
    except OSError:
        pass
    return None


def sanitize_filename(name: str, fallback: str = "media") -> str:
    """Strip Windows/URL-illegal chars so a title is safe as a filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 180:
        cleaned = cleaned[:180].rstrip(" .") or fallback
    return cleaned


def extract_thumbnail(info: Dict[str, Any] | None) -> str:
    if not isinstance(info, dict):
        return ""
    direct = (info.get("thumbnail") or "").strip() if isinstance(info.get("thumbnail"), str) else ""
    if direct.startswith(("http://", "https://")):
        return direct
    thumbs = info.get("thumbnails") or []
    if isinstance(thumbs, list) and thumbs:
        valid = [
            t for t in thumbs
            if isinstance(t, dict) and isinstance(t.get("url"), str)
            and t["url"].startswith(("http://", "https://"))
        ]
        if valid:
            def _score(t: Dict[str, Any]) -> tuple:
                try:
                    w = int(t.get("width") or 0)
                except (TypeError, ValueError):
                    w = 0
                try:
                    h = int(t.get("height") or 0)
                except (TypeError, ValueError):
                    h = 0
                try:
                    pref = int(t.get("preference") or 0)
                except (TypeError, ValueError):
                    pref = 0
                return (w * h, w, h, pref)
            valid.sort(key=_score)
            return valid[-1]["url"]
    for entry in (info.get("entries") or []):
        if isinstance(entry, dict):
            nested = extract_thumbnail(entry)
            if nested:
                return nested
    return direct if direct else ""


def delete_job_files(job: Job) -> None:
    """Delete a job's finished files from disk (best-effort, never raises)."""
    seen = set()
    targets: list[str] = []
    for raw in [job.filename, *(job.files or [])]:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        targets.append(raw)
        # Also queue partial/fragment sidecars (.part, .ytdl, aria2, etc.)
        try:
            base = Path(raw).name
            parent = Path(raw).parent
            if parent.is_dir():
                for suffix in (".part", ".ytdl", ".aria2", ".temp"):
                    cand = parent / (base + suffix)
                    if str(cand) not in seen:
                        seen.add(str(cand))
                        targets.append(str(cand))
        except OSError:
            continue
    try:
        download_root = DOWNLOAD_DIR.resolve()
    except OSError:
        return
    for raw in targets:
        try:
            p = Path(raw)
            try:
                if not p.resolve().is_relative_to(download_root):
                    continue
            except OSError:
                continue
            if p.is_file():
                p.unlink()
        except OSError:
            continue


def _wait_for_idle(job: Job, timeout: float = 5.0) -> bool:
    """Wait until the worker thread releases the job. Never raises."""
    waited = 0.0
    while job.running and waited < timeout:
        time.sleep(0.1)
        waited += 0.1
    return not job.running


class JobBusyError(RuntimeError):
    """Raised when a job's previous worker has not shut down yet."""


def video_format_selector(quality: str) -> str:
    height = VIDEO_QUALITIES.get(quality)
    if height is None:
        # "Best quality" = true maximum (4K VP9/AV1 allowed). H.264 tops out
        # at 1080p on YouTube, so avc1-first here would silently cap Best at
        # 1080p. Audio still prefers AAC for device compatibility.
        return (
            "bestvideo+bestaudio[acodec^=mp4a]/"
            "bestvideo+bestaudio/"
            "best"
        )
    if height > 1080:
        # 1440/2160: resolution must outrank codec — H.264 has no 4K stream,
        # so any avc1-first option would lock the request to 1080p (this was
        # the "4K downloads at 1080p size" bug). VP9/AV1 merged to MP4.
        return (
            f"bestvideo[height<={height}]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]"
        )
    if find_ffmpeg():
        # <=1080p: prefer H.264+AAC in MP4 (plays everywhere), capped.
        return (
            f"bestvideo[vcodec^=avc1][height<={height}]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[vcodec^=avc1][height<={height}]+bestaudio/"
            f"bestvideo[height<={height}]+bestaudio[acodec^=mp4a]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[ext=mp4][height<={height}]/"
            f"best[height<={height}]"
        )
    return f"best[ext=mp4][height<={height}]/best[height<={height}]"


def label_for_height(height: Optional[int]) -> str:
    if not height:
        return "Best"
    key = str(int(height))
    if key in RESOLUTION_LABELS:
        return RESOLUTION_LABELS[key]
    if int(height) >= 2000:
        return f"4K {int(height)}p"
    return f"{int(height)}p"


def video_resolution_label(job: Job) -> str:
    if job.quality in RESOLUTION_LABELS:
        return RESOLUTION_LABELS[job.quality]
    if job.quality == "best":
        return label_for_height(job.actual_height)
    try:
        return label_for_height(int(job.quality))
    except (TypeError, ValueError):
        return label_for_height(job.actual_height) if job.actual_height else "Best"


def finalize_video_path(original: str, job: Job) -> str:
    src = Path(original)
    try:
        if not src.is_file():
            return original
        label = video_resolution_label(job)
        stem = src.stem
        clean = re.sub(r"\s+\[[A-Za-z0-9_-]{6,}\]$", "", stem).strip() or stem
        clean = re.sub(r"\s+-\s+\[.+?\]$", "", clean).strip() or clean
        ext = src.suffix or ".mp4"
        if f"[{label}]" in stem:
            return original
        dst = src.with_name(f"{clean} - [{label}]{ext}")
        try:
            if dst.resolve() == src.resolve():
                return original
        except OSError:
            pass
        counter = 1
        candidate = dst
        while candidate.exists():
            try:
                if candidate.resolve() == src.resolve():
                    break
            except OSError:
                pass
            candidate = src.with_name(f"{clean} - [{label}] ({counter}){ext}")
            counter += 1
            if counter > 100:
                return original
        candidate.parent.mkdir(parents=True, exist_ok=True)
        src.rename(candidate)
        return str(candidate)
    except Exception:
        return original


def clean_error(exc: BaseException) -> str:
    raw = str(exc) or exc.__class__.__name__
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    low = raw.lower()
    if "winerror 32" in low or "unable to rename" in low or "being used by another process" in low:
        return "Interrupted while finalizing (file was busy after Stop). Press Resume to continue — partial data is kept."
    if "http error 403" in low or ("403" in low and "forbidden" in low):
        return "Login required or video is private. Try uploading cookies.txt."
    if "http error 429" in low or "too many requests" in low:
        return "Rate limited. Please wait a bit and retry."
    if "http error 404" in low or ("404" in low and "not found" in low):
        return "Video not found."
    if "video unavailable" in low:
        return "This video is unavailable or deleted."
    if "private video" in low:
        return "This video is private. Login required (upload cookies.txt)."
    if re.search(r"\bage\b", low) and ("confirm" in low or "restricted" in low or "gate" in low):
        return "Age-restricted video. Login required (upload cookies.txt)."
    if "sign in" in low or "log in" in low or "login required" in low or ("cookies" in low and "required" in low):
        return "Login required. Upload cookies.txt and retry."
    if "copyright" in low:
        return "Blocked on copyright grounds."
    if "unsupported url" in low or "no video formats" in low or "unable to extract" in low:
        return "Unsupported link or no downloadable media found."
    if "timed out" in low or "timeout" in low or ("connection" in low and ("reset" in low or "refused" in low or "failed" in low or "aborted" in low)):
        return "Network error. Check your connection and retry."
    if "name resolution" in low or "nodename nor servname" in low or "getaddrinfo failed" in low:
        return "Network error (DNS). Check your connection and retry."
    if "ffmpeg" in low and ("not found" in low or "not installed" in low):
        return "ffmpeg is missing. Install ffmpeg and restart."
    msg = raw.split("Traceback (most recent call last)")[0]
    msg = re.sub(r"^\s*(ERROR|WARNING):\s*", "", msg, flags=re.IGNORECASE)
    msg = re.sub(r"\s*use --verbose options to see.*$", "", msg, flags=re.IGNORECASE | re.DOTALL)
    msg = msg.strip().splitlines()
    text = " ".join(line.strip() for line in msg[:4]).strip()
    return text[:500] or "Download failed"


@dataclass
class Job:
    id: str
    url: str
    mode: str
    quality: str
    playlist: bool
    title: str = ""
    thumbnail: str = ""
    status: str = "queued"
    percent: float = 0.0
    speed: Optional[float] = None
    eta: Optional[int] = None
    entry: Optional[int] = None
    entries: Optional[int] = None
    filename: str = ""
    files: List[str] = field(default_factory=list)
    error: str = ""
    actual_height: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    cancel_requested: bool = False
    pause_requested: bool = False
    # True while a _run worker thread is inside yt-dlp for this job.
    # Used to serialize Stop -> Resume so two yt-dlp processes never touch
    # the same .part file at once (that race causes WinError 32 on rename).
    running: bool = False
    notify: Optional[Callable[[], None]] = field(default=None, repr=False, compare=False)

    def changed(self) -> None:
        if self.notify is not None:
            self.notify()

    @property
    def output(self) -> str:
        return Path(self.filename).name if self.filename else ""

    @property
    def size(self) -> int:
        # Only finished jobs expose a size; avoids a disk stat on every
        # SSE/poll tick while downloading (thundering-herd IO).
        if self.status != "finished" or not self.filename:
            return 0
        try:
            return Path(self.filename).stat().st_size
        except OSError:
            return 0

    def to_dict(self) -> Dict[str, Any]:
        # Snapshot mutable fields so hook-thread appends can't tear the read.
        try:
            files_snapshot = list(self.files or [])
        except Exception:
            files_snapshot = []
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "quality": self.quality,
            "playlist": self.playlist,
            "title": self.title or self.url,
            "thumbnail": self.thumbnail,
            "status": self.status,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "entry": self.entry,
            "entries": self.entries,
            "file": self.output,
            "size": self.size,
            "files": [Path(p).name for p in files_snapshot],
            "error": self.error,
            "created_at": self.created_at,
        }


class JobManager:
    """In-memory only job manager — no database, no persisted queue.

    Refreshing or closing the browser wipes the visible list (frontend keeps
    it in memory only), and restarting the server wipes everything here.
    """

    def __init__(self) -> None:
        self._jobs: Dict[str, Job] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._changed = threading.Condition()
        self._version = 0
        try:
            DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self._sweep_expired_files()
        threading.Thread(target=self._sweeper_loop, daemon=True).start()
    def _sweeper_loop(self) -> None:
        while True:
            try:
                time.sleep(3600)
                self._sweep_expired_files()
            except Exception:
                continue

    def _sweep_expired_files(self) -> None:
        now = time.time()
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            if job.status in ACTIVE_STATUSES or job.running:
                continue
            age = now - (job.created_at or now)
            if age >= FILE_RETENTION_SECONDS:
                delete_job_files(job)
                job.filename = ""
                job.files = []
        try:
            referenced = set()
            for job in jobs:
                for raw in [job.filename, *(job.files or [])]:
                    if raw:
                        referenced.add(str(Path(raw).resolve()) if Path(raw).exists() else raw)
            if DOWNLOAD_DIR.is_dir():
                for child in DOWNLOAD_DIR.iterdir():
                    try:
                        if not child.is_file():
                            continue
                        key = str(child.resolve())
                        if key in referenced:
                            continue
                        if now - child.stat().st_mtime >= FILE_RETENTION_SECONDS:
                            child.unlink()
                    except OSError:
                        continue
        except OSError:
            pass

    def _on_job_changed(self, job: Job) -> None:
        # Ephemeral: notify SSE listeners only, never persist.
        self._notify()

    def _notify(self) -> None:
        with self._changed:
            self._version += 1
            self._changed.notify_all()

    def version(self) -> int:
        with self._changed:
            return self._version

    def wait_for_change(self, version: int, timeout: float = 10.0) -> int:
        with self._changed:
            if self._version == version:
                self._changed.wait(timeout)
            return self._version

    def create(self, url: str, mode: str, quality: str, playlist: bool) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], url=url, mode=mode, quality=quality, playlist=playlist)
        job.notify = lambda: self._on_job_changed(job)
        with self._lock:
            self._prune_locked()
            self._jobs[job.id] = job
        self._notify()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _prune_locked(self) -> None:
        if len(self._jobs) < MAX_JOBS_KEPT:
            return
        done = [j for j in self._jobs.values()
                if j.status not in ACTIVE_STATUSES and not j.running]
        done.sort(key=lambda j: j.created_at)
        victims = done[: len(self._jobs) - MAX_JOBS_KEPT + 1]
        for job in victims:
            self._jobs.pop(job.id, None)
        # Disk IO outside the lock so list/get never block on unlink.
        for job in victims:
            delete_job_files(job)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs

    def cancel(self, job_id: str) -> Optional[Job]:
        """Stop a download (user pressed Stop). Partial files are kept."""
        job = self.get(job_id)
        if not job:
            return None
        job.pause_requested = False
        job.cancel_requested = True
        if job.status in ACTIVE_STATUSES:
            job.status = "cancelled"
        job.changed()
        self._notify()
        return job

    def pause(self, job_id: str) -> Optional[Job]:
        """Pause an active download. Partial (.part) files are kept so resume
        can continue with yt-dlp ``--continue`` / ``continuedl``."""
        job = self.get(job_id)
        if not job:
            return None
        if job.status not in ACTIVE_STATUSES:
            return job
        job.pause_requested = True
        job.cancel_requested = True
        job.status = "paused"
        job.speed = None
        job.eta = None
        job.changed()
        self._notify()
        return job

    def resume(self, job_id: str) -> Optional[Job]:
        """Resume a paused/cancelled/failed job in place (same id).

        Re-runs yt-dlp with ``continuedl`` so partial fragments are reused
        instead of starting over.
        """
        job = self.get(job_id)
        if not job:
            return None
        if job.status in ACTIVE_STATUSES:
            return job
        if job.status not in RESUMABLE_STATUSES:
            return job
        if job.running:
            # Old worker is still shutting down (releasing .part handles).
            # Wait briefly so we never run two yt-dlp instances on the same
            # files — that overlap is what caused WinError 32 on rename.
            # Short wait (2s) to avoid blocking the server threadpool.
            if not _wait_for_idle(job, timeout=2.0):
                # Signal the caller explicitly. Returning the job here used
                # to make the API answer 409 even when the resume succeeded
                # (the freshly started worker had already set `running`).
                raise JobBusyError("Still stopping — try Resume again in a few seconds.")
            # Status may have been updated by the finishing worker; re-check.
            if job.status in ACTIVE_STATUSES:
                return job
            if job.status not in RESUMABLE_STATUSES:
                return job
        job.pause_requested = False
        job.cancel_requested = False
        job.error = ""
        job.status = "queued"
        job.speed = None
        job.eta = None
        job.changed()
        self._notify()
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        # Always signal the worker to stop — even when `running` is still
        # False because the job is queued behind the concurrency semaphore.
        # Without this, deleting a queued job let its thread start later and
        # download into a job that no longer exists (orphan file + wasted
        # slot). The worker re-checks these flags right after acquiring a slot.
        job.cancel_requested = True
        if job.running:
            # Never delete files out from under a live yt-dlp/ffmpeg worker
            # (WinError 32 + orphan handles). Ask it to stop and wait.
            if not _wait_for_idle(job, timeout=5.0):
                return False
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            delete_job_files(job)
            self._notify()
            return True
        return False

    # Jobs safe to bulk-clear: finished/cancelled only. Paused jobs are
    # resumable by design and must survive "Clear finished".
    CLEARABLE_STATUSES = ("finished", "cancelled")

    def clear_done(self) -> int:
        with self._lock:
            stale = [j for j in self._jobs.values()
                     if j.status in self.CLEARABLE_STATUSES and not j.running]
            for job in stale:
                self._jobs.pop(job.id, None)
        for job in stale:
            delete_job_files(job)
        if stale:
            self._notify()
        return len(stale)

    def clear_failed(self) -> int:
        with self._lock:
            stale = [j for j in self._jobs.values()
                     if j.status == "error" and not j.running]
            for job in stale:
                self._jobs.pop(job.id, None)
        for job in stale:
            delete_job_files(job)
        if stale:
            self._notify()
        return len(stale)

    def _run(self, job: Job) -> None:
        with self._slots:
            job.running = True
            try:
                if job.cancel_requested and not job.pause_requested:
                    job.status = "cancelled"
                    job.changed()
                    return
                if job.pause_requested:
                    job.status = "paused"
                    job.changed()
                    return
                job.status = "downloading"
                job.changed()
                try:
                    with yt_dlp.YoutubeDL(build_options(job)) as ydl:
                        ydl.download([job.url])
                except DownloadCancelled:
                    if job.pause_requested:
                        job.status = "paused"
                    else:
                        job.status = "cancelled"
                except Exception as exc:  # noqa: BLE001
                    if job.pause_requested:
                        job.status = "paused"
                    elif job.cancel_requested:
                        job.status = "cancelled"
                    else:
                        job.status = "error"
                        job.error = clean_error(exc)
                else:
                    if job.pause_requested:
                        job.status = "paused"
                    elif job.cancel_requested:
                        job.status = "cancelled"
                    elif job.files:
                        # Hard cap guarantee: a capped request (e.g. 1080p)
                        # must NEVER finish above its height. If yt-dlp
                        # somehow resolved higher, fail loudly instead of
                        # silently handing over the wrong resolution.
                        cap = VIDEO_QUALITIES.get(job.quality) if job.mode == "video" else None
                        if cap and job.actual_height and int(job.actual_height) > int(cap):
                            job.status = "error"
                            job.error = (
                                f"Wrong resolution received ({job.actual_height}p > {cap}p). "
                                "Please Retry — partial data is kept."
                            )
                        else:
                            job.status = "finished"
                            job.percent = 100.0
                    else:
                        job.status = "error"
                        job.error = "No file was downloaded"
                finally:
                    job.speed = None
                    job.eta = None
                    # Clear one-shot flags (pause flag is consumed; a fresh
                    # pause() call sets it again for the next run).
                    if job.status != "paused":
                        job.pause_requested = False
                    job.changed()
            finally:
                job.running = False


def build_options(job: Job) -> Dict[str, Any]:
    progress_hook = _make_progress_hook(job)
    postprocessor_hook = _make_postprocessor_hook(job)
    post_hook = _make_post_hook(job)

    opts: Dict[str, Any] = {
        "outtmpl": {"default": str(DOWNLOAD_DIR / "%(title)s [%(id)s].%(ext)s")},
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
        # Resume support: never wipe partial files, continue where we left off
        # (yt-dlp equivalent of ``--continue``). Overwrites are off so a
        # resumed job reuses .part / fragment data instead of restarting.
        "continuedl": True,
        "overwrites": False,
        "nopart": False,
        "noplaylist": not job.playlist,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "post_hooks": [post_hook],
    }
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    cookiefile = get_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    # Prefer aria2c when available: multi-connection + ``--continue=true``
    # gives real resume for interrupted HTTP downloads.
    aria2c = find_aria2c()
    if aria2c:
        opts["external_downloader"] = {"default": "aria2c"}
        opts["external_downloader_args"] = {
            "default": ["--continue=true", "-x", "4", "-s", "4", "-k", "1M", "--auto-file-renaming=false"]
        }

    # PERMANENT QUALITY PRINCIPLE (do not change):
    # - Never degrade resolution: always take the maximum quality AT or BELOW
    #   the selected height cap (bestvideo[height<=H] chain, no upscale).
    # - Standard MB size like other platforms: original YouTube bitrate,
    #   no re-encode (merge/remux only), so no bloat and no quality loss.
    # - Universal device support: prefer H.264 (avc1) + AAC (mp4a) in MP4
    #   container; VP9/AV1/WebM only as last-resort fallback.
    _need_height = VIDEO_QUALITIES.get(job.quality) if job.mode == "video" else None
    if job.mode == "video" and (_need_height is None or _need_height > 1080):
        # High-res request: resolution/fps/bitrate must outrank codec, else
        # `best[height<=2160]`-style picks lose to 1080p H.264 (same 4K bug).
        opts["format_sort"] = ["res", "fps", "br", "vcodec:avc1", "acodec:mp4a", "ext:mp4"]
    else:
        opts["format_sort"] = ["vcodec:avc1", "acodec:mp4a", "ext:mp4", "res", "fps", "br"]
    opts["format_sort_force"] = False
    opts["prefer_free_formats"] = False
    opts["allow_unplayable_formats"] = False

    if job.mode == "audio":
        codec = job.quality if job.quality in AUDIO_FORMATS else "mp3"
        audio_quality = "320" if codec == "mp3" else "192"
        opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": codec,
                "preferredquality": audio_quality,
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
            {
                "key": "EmbedThumbnail",
            },
        ]
    else:
        opts["format"] = video_format_selector(job.quality)
        # MP4 everywhere, no re-encode: remux/merge only -> resolution intact,
        # standard size, plays on phone/TV/PC without conversion.
        opts["merge_output_format"] = "mp4"
        opts["remuxvideo"] = "mp4"
    return opts


def _make_progress_hook(job: Job):
    def hook(d: Dict[str, Any]) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Download stopped by user")

        info = d.get("info_dict") or {}
        playlist_title = info.get("playlist_title")
        if playlist_title:
            job.title = playlist_title
        elif not job.title and info.get("title"):
            job.title = info["title"]
        if not job.thumbnail:
            thumb = extract_thumbnail(info)
            if thumb:
                job.thumbnail = thumb
        try:
            height = info.get("height")
            if isinstance(height, (int, float)) and int(height) > 0:
                if job.actual_height is None or int(height) > job.actual_height:
                    job.actual_height = int(height)
        except (TypeError, ValueError):
            pass

        entries = info.get("n_entries") or 0
        entry = info.get("playlist_index") or 0
        job.entries = int(entries) if entries else None
        job.entry = int(entry) if entry else None

        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done * 100.0 / total) if total else 0.0
            job.percent = round(_overall(job, pct), 1)
            job.speed = d.get("speed")
            job.eta = d.get("eta")
            # A resumed job reports downloading again.
            job.status = "downloading"
        elif status == "finished":
            job.percent = round(_overall(job, 100.0), 1)
            job.speed = None
            job.eta = None
            job.status = "processing"
        job.changed()

    return hook


def _overall(job: Job, item_percent: float) -> float:
    if job.entries and job.entry:
        return min(99.5, ((job.entry - 1) + item_percent / 100.0) * 100.0 / job.entries)
    return min(99.5, item_percent)


def _make_postprocessor_hook(job: Job):
    def hook(d: Dict[str, Any]) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Download stopped by user")
        if d.get("status") == "started":
            job.status = "processing"
            job.speed = None
            job.eta = None
            job.changed()

    return hook


def _make_post_hook(job: Job):
    def hook(filepath: str) -> None:
        final_path = filepath
        if job.mode == "video":
            final_path = finalize_video_path(filepath, job)
        job.files.append(final_path)
        job.filename = final_path
        job.percent = round(_overall(job, 100.0), 1) if job.entries else 100.0
        job.changed()

    return hook


def _url_host_blocked(url: str) -> bool:
    """True if the URL's host is missing/non-http or resolves private."""
    import socket as _socket
    import urllib.parse as _parse
    import ipaddress as _ip
    try:
        parsed = _parse.urlparse(url)
    except Exception:
        return True
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    try:
        infos = _socket.getaddrinfo(parsed.hostname, None)
    except Exception:
        return True
    for _fam, _, _, _, sockaddr in infos:
        try:
            ip = _ip.ip_address(sockaddr[0])
        except ValueError:
            return True
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return True
    return False


def probe(url: str) -> Dict[str, Any]:
    if _url_host_blocked(url):
        return {"ok": False, "error": "Blocked host"}
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
    }
    cookiefile = get_cookiefile()
    if cookiefile:
        opts["cookiefile"] = cookiefile
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": clean_error(exc)}
    if not info:
        return {"ok": False, "error": "Could not read this link"}

    is_playlist = info.get("_type") in ("playlist", "multi_video")
    durations = info.get("duration")
    raw_entries = info.get("entries") or []
    entries = [
        {
            "id": e.get("id"),
            "title": e.get("title"),
            "duration": e.get("duration"),
        }
        for e in raw_entries[:100]
        if isinstance(e, dict)
    ]
    thumbnail = extract_thumbnail(info)
    # Coerce to ints: a single malformed (string) height from an extractor
    # would otherwise make sorted() raise TypeError and 500 the preview.
    height_values = set()
    for fmt in (info.get("formats") or []):
        if not isinstance(fmt, dict):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        try:
            h = int(fmt.get("height"))
        except (TypeError, ValueError):
            continue
        if h > 0:
            height_values.add(h)
    heights = sorted(height_values, reverse=True)
    return {
        "ok": True,
        "type": "playlist" if is_playlist else "video",
        "title": info.get("title") or url,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": durations,
        "thumbnail": thumbnail,
        "url": info.get("webpage_url") or url,
        "count": info.get("playlist_count") or len(entries) or 1,
        "entries": entries,
        "heights": [h for h in heights if h],
    }
