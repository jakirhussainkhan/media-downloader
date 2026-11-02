#!/usr/bin/env bash
# Media Downloader - start script for Linux and macOS.
#
# Usage:
#   ./run.sh                  start in the foreground (Ctrl+C to stop)
#   ./run.sh --background     start detached, logging to server.log
#   ./run.sh --port 8080      listen on another port (default 8000)
#
# Then open http://127.0.0.1:8000 locally, or http://<your-LAN-IP>:8000
# from a phone on the same Wi-Fi.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=8000
HOST="${MEDIA_DOWNLOADER_HOST:-0.0.0.0}"
BACKGROUND=0

usage() {
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        -b|--background) BACKGROUND=1 ;;
        -p|--port)
            [ $# -ge 2 ] || { echo "Missing value for $1" >&2; exit 2; }
            PORT="$2"
            shift
            ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

case "$PORT" in
    ''|*[!0-9]*) echo "Invalid port: $PORT" >&2; exit 2 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "Invalid port: $PORT" >&2
    exit 2
fi

# PID listening on $PORT, if any. Works with lsof (macOS/Linux), ss, or fuser.
port_owner() {
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1
    elif command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$PORT" 2>/dev/null \
            | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1
    elif command -v fuser >/dev/null 2>&1; then
        fuser "$PORT/tcp" 2>/dev/null | tr -s ' ' '\n' | head -n 1
    fi
}

find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

is_online() {
    "$VENV/bin/python" - <<'PY' >/dev/null 2>&1
import socket
try:
    socket.create_connection(("pypi.org", 443), timeout=2).close()
except OSError:
    raise SystemExit(1)
PY
}

health_ok() {
    "$VENV/bin/python" - "$PORT" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{sys.argv[1]}/api/health", timeout=1.5
    ) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
}

# Guard: never start a second instance.
OWNER="$(port_owner || true)"
if [ -n "$OWNER" ]; then
    echo "Port $PORT is already in use by PID $OWNER."
    echo "Open http://127.0.0.1:$PORT or run ./stop.sh first."
    exit 1
fi

# Reuse .venv when it belongs to this OS; a Windows venv has Scripts/, not bin/.
if [ -x ".venv/bin/python" ]; then
    VENV=".venv"
elif [ -d ".venv" ]; then
    VENV=".venv-linux"
else
    VENV=".venv"
fi

if [ ! -x "$VENV/bin/python" ]; then
    if ! PY="$(find_python)"; then
        echo "Python 3.11+ not found. Install it and re-run ./run.sh." >&2
        echo "  Debian/Ubuntu: sudo apt install python3 python3-venv" >&2
        echo "  Fedora:        sudo dnf install python3" >&2
        echo "  macOS:         brew install python@3.12" >&2
        exit 1
    fi
    echo "Creating virtual environment in $VENV (using $PY) ..."
    if ! "$PY" -m venv "$VENV"; then
        echo "Could not create the virtual environment." >&2
        echo "  Debian/Ubuntu: sudo apt install python3-venv" >&2
        exit 1
    fi
fi

if is_online; then
    echo "Installing dependencies ..."
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q \
        --retries 1 --timeout 10 -r requirements.txt \
        || echo "pip install -r requirements.txt failed, continuing with cached packages."
    # Keep yt-dlp current: outdated extractors are the #1 cause of failures.
    "$VENV/bin/python" -m pip install --disable-pip-version-check -q \
        --retries 1 --timeout 10 -U yt-dlp \
        || echo "yt-dlp update failed, continuing with installed version."
else
    echo "Offline (pypi.org unreachable) - skipping pip install, using cached packages."
fi

if ! "$VENV/bin/python" -c 'import fastapi, uvicorn, yt_dlp' 2>/dev/null; then
    echo "Dependencies missing and offline install impossible." >&2
    echo "Connect to the internet once and re-run ./run.sh." >&2
    exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
    case "$(uname -s)" in
        Darwin) echo "ffmpeg not found - install with: brew install ffmpeg" ;;
        *) echo "ffmpeg not found - install with: sudo apt install ffmpeg (or dnf/pacman)" ;;
    esac
fi

URL="http://127.0.0.1:$PORT"

if [ "$BACKGROUND" -eq 1 ]; then
    nohup "$VENV/bin/python" -m uvicorn main:app --host "$HOST" --port "$PORT" \
        >>server.log 2>&1 &
    SERVER_PID=$!
    echo "$SERVER_PID" >server.pid

    ready=0
    i=0
    while [ "$i" -lt 60 ]; do
        if health_ok; then
            ready=1
            break
        fi
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            break
        fi
        i=$((i + 1))
        sleep 0.5
    done

    if [ "$ready" -eq 1 ]; then
        echo "Started Media Downloader (PID $SERVER_PID) on $URL"
        echo "Logs: $ROOT/server.log - stop with: ./stop.sh"
    else
        echo "Server did not become healthy. Check $ROOT/server.log" >&2
        exit 1
    fi
else
    echo "Starting Media Downloader on http://$HOST:$PORT (local: $URL)"
    echo "Press Ctrl+C to stop."
    exec "$VENV/bin/python" -m uvicorn main:app --host "$HOST" --port "$PORT"
fi
