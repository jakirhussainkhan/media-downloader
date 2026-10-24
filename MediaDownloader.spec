# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for Media Downloader one-click EXE.

Build with:  .\\build-exe.ps1
Or directly: pyinstaller MediaDownloader.spec --noconfirm --clean

Produces a single windowed file: dist/MediaDownloader.exe
- static/      -> bundled (_MEIPASS/static)
- bin/ffmpeg.exe, bin/ffprobe.exe -> bundled root (if present)
- cookies/ NOT bundled (runtime goes to %APPDATA%/MediaDownloader)
"""
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)  # type: ignore[name-defined]  # noqa: F821

datas = []
static_dir = ROOT / "static"
if static_dir.is_dir():
    datas.append((str(static_dir), "static"))

binaries = []
bin_dir = ROOT / "bin"
if bin_dir.is_dir():
    for name in ("ffmpeg.exe", "ffprobe.exe", "aria2c.exe"):
        cand = bin_dir / name
        if cand.is_file():
            binaries.append((str(cand), "."))

icon_file = ROOT / "assets" / "icon.ico"
icon = str(icon_file) if icon_file.is_file() else None

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "fastapi",
    "starlette",
    "pydantic",
    "yt_dlp",
    "mutagen",
    "multipart",
    "pystray",
    "PIL",
]

a = Analysis(  # noqa: F821
    ["launcher.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PySide6"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MediaDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed: no black CMD window on double-click
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
