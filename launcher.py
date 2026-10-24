"""One-click launcher for the Windows EXE.

Double-click behaviour:
  1. If an instance is already running (port 8000-8010 answers /api/health),
     open the browser there and exit (single-instance).
  2. Otherwise start uvicorn in a background thread on the first free port.
  3. Auto-open the browser once /api/health is OK.
  4. Sit in the system tray (Open App / Quit). Without pystray, just block.

This file is the PyInstaller entry point (console=False). It must not print
to stdout at startup — there is no console in windowed mode.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from version import APP_NAME, __version__

PORT_START = 8000
PORT_END = 8010


def _bundle_dir() -> Path:
    import sys

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    if bool(getattr(sys, "frozen", False)):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _ensure_bundled_bins_on_path() -> None:
    """Let yt-dlp/ffmpeg subprocesses find bundled ffmpeg without full path."""
    bundle = _bundle_dir()
    candidates = [bundle, bundle / "bin"]
    if os.name == "nt":
        try:
            exe_dir = Path(__file__).resolve().parent
            import sys as _sys

            if bool(getattr(_sys, "frozen", False)):
                exe_dir = Path(_sys.executable).resolve().parent
            candidates.extend([exe_dir, exe_dir / "bin"])
        except Exception:
            pass
    extra = []
    for c in candidates:
        try:
            if c.is_dir() and str(c) not in os.environ.get("PATH", ""):
                extra.append(str(c))
        except OSError:
            continue
    if extra:
        os.environ["PATH"] = os.pathsep.join(extra + [os.environ.get("PATH", "")])


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _health_ok(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as r:
            return r.status == 200
    except Exception:
        return False


def _find_running_instance() -> int | None:
    for port in range(PORT_START, PORT_END + 1):
        if not _is_port_free(port) and _health_ok(port, timeout=1.0):
            return port
    return None


def _find_free_port() -> int:
    for port in range(PORT_START, PORT_END + 1):
        if _is_port_free(port):
            return port
    # Fallback: let the OS pick (browser URL will use the real port).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_ok(port, timeout=1.5):
            return True
        time.sleep(0.4)
    return False


def _open_browser(port: int) -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass


def _make_icon():
    """Simple download-arrow icon, no .ico file needed."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, fill=(24, 24, 40, 255))
    d.rounded_rectangle([2, 2, size - 3, size - 3], radius=14, outline=(139, 92, 246, 255), width=3)
    # Arrow: shaft + head
    d.rectangle([size // 2 - 5, 14, size // 2 + 5, 38], fill=(255, 255, 255, 255))
    d.polygon(
        [(size // 2 - 12, 36), (size // 2 + 12, 36), (size // 2, 50)],
        fill=(59, 130, 246, 255),
    )
    return img


def _run_server(port: int) -> None:
    import uvicorn

    from main import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def main() -> None:
    _ensure_bundled_bins_on_path()

    existing = _find_running_instance()
    if existing is not None:
        _open_browser(existing)
        return

    port = _find_free_port()
    server_thread = threading.Thread(target=_run_server, args=(port,), daemon=True)
    server_thread.start()

    def _open_when_ready() -> None:
        if _wait_for_health(port, timeout=30.0):
            _open_browser(port)

    threading.Thread(target=_open_when_ready, daemon=True).start()

    # System tray (preferred). Fallback: block on the server thread.
    try:
        import pystray  # type: ignore
        from pystray import Menu, MenuItem  # type: ignore
    except Exception:
        pystray = None  # type: ignore

    if pystray is None:
        _wait_for_health(port, timeout=30.0)
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass
        return

    icon_image = _make_icon()

    def _on_open(icon, item):
        _open_browser(port)

    def _on_quit(icon, item):
        try:
            icon.stop()
        except Exception:
            pass
        os._exit(0)

    menu = Menu(
        MenuItem("Open App", _on_open, default=True),
        MenuItem("Quit", _on_quit),
    )
    try:
        icon = pystray.Icon(APP_NAME, icon_image, f"{APP_NAME} v{__version__} — port {port}", menu)
        icon.run()
    except Exception:
        try:
            server_thread.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
