#!/usr/bin/env bash
# Media Downloader - stop script for Linux and macOS.
#
# Usage:
#   ./stop.sh              stop the server on port 8000
#   ./stop.sh --port 8080  stop the server on another port
set -uo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT=8000
PID_FILE="$ROOT/server.pid"

usage() {
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
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

is_alive() {
    kill -0 "$1" 2>/dev/null
}

PIDS=""

add_pid() {
    local pid="$1"
    [ -n "$pid" ] || return 0
    case " $PIDS " in
        *" $pid "*) return 0 ;;
    esac
    PIDS="$PIDS $pid"
}

# 1. PID file written by ./run.sh --background
if [ -f "$PID_FILE" ]; then
    FILE_PID="$(tr -dc '0-9' <"$PID_FILE")"
    if [ -n "$FILE_PID" ] && is_alive "$FILE_PID"; then
        add_pid "$FILE_PID"
    fi
fi

# 2. Whoever is listening on the port
add_pid "$(port_owner || true)"

# 3. Fallback: the uvicorn process for this app
if [ -z "${PIDS// /}" ]; then
    for pid in $(pgrep -f "uvicorn main:app" 2>/dev/null || true); do
        add_pid "$pid"
    done
fi

if [ -z "${PIDS// /}" ]; then
    echo "No Media Downloader server is running on port $PORT."
    rm -f "$PID_FILE"
    echo "Port $PORT is free."
    exit 0
fi

for pid in $PIDS; do
    echo "Stopping PID $pid ..."
    kill -TERM "$pid" 2>/dev/null || true
done

# Give the server up to 5 seconds to shut down cleanly.
i=0
while [ "$i" -lt 10 ]; do
    ANY_ALIVE=0
    for pid in $PIDS; do
        if is_alive "$pid"; then
            ANY_ALIVE=1
        fi
    done
    [ "$ANY_ALIVE" -eq 0 ] && break
    i=$((i + 1))
    sleep 0.5
done

for pid in $PIDS; do
    if is_alive "$pid"; then
        echo "PID $pid ignored SIGTERM, forcing ..."
        kill -KILL "$pid" 2>/dev/null || true
    fi
done

rm -f "$PID_FILE"
sleep 0.5

STILL="$(port_owner || true)"
if [ -n "$STILL" ]; then
    echo "WARNING: port $PORT is still held by PID $STILL" >&2
    exit 1
fi

echo "Port $PORT is free."
