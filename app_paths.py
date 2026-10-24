"""Shared path helpers: work both from source and from a PyInstaller EXE.

Source run:  resources next to this file, writable data next to this file.
Frozen EXE:  resources inside the bundle (sys._MEIPASS / exe dir),
             writable data under %APPDATA%/MediaDownloader (portable EXE
             also honours MEDIA_DOWNLOADER_DATA_DIR env var).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Read-only resources (static/, bundled ffmpeg)."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if is_frozen():
        # onedir build: resources sit next to the exe
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Writable per-user data (cookies). Never inside the bundle."""
    override = os.environ.get("MEDIA_DOWNLOADER_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if is_frozen():
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "MediaDownloader"
        return Path.home() / "MediaDownloader"
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return bundle_dir().joinpath(*parts)


def ensure_dir(p: Path) -> Path:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p
