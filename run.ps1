# Media Downloader - start script
# Usage: .\run.ps1   (then open http://127.0.0.1:8000 locally,
# or http://<your-PC-IP>:8000 from a phone on the same Wi-Fi)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Guard: never start a second instance
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $owner = $listener.OwningProcess | Select-Object -First 1
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $owner" -ErrorAction SilentlyContinue
    Write-Host "Port 8000 is already in use by PID $owner ($($proc.Name))." -ForegroundColor Yellow
    Write-Host "Open http://127.0.0.1:8000 or run .\stop.ps1 first." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path ".venv")) {
    python -m venv .venv
}

$py = ".\.venv\Scripts\python.exe"

function Test-Online {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $iar = $c.BeginConnect("pypi.org", 443, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(2000)
        $c.Close()
        return $ok
    } catch { return $false }
}

$online = Test-Online
if (-not $online) {
    Write-Host "Offline (pypi.org unreachable) - skipping pip install, using cached packages." -ForegroundColor Yellow
} else {
    # Online: ensure deps, fail fast (1 retry, 10s timeout) and never abort startup on pip errors.
    try {
        & $py -m pip install --disable-pip-version-check -q --retries 1 --timeout 10 -r requirements.txt
        if ($LASTEXITCODE -ne 0) { Write-Host "pip install -r requirements.txt failed, continuing with cached packages." -ForegroundColor Yellow }
    } catch { Write-Host "pip install skipped/failed ($($_.Exception.Message)), continuing." -ForegroundColor Yellow }

    # Auto-update yt-dlp so YouTube/Facebook/etc. extractor fixes are always
    # picked up (outdated yt-dlp is the #1 cause of sudden download failures).
    try {
        & $py -m pip install --disable-pip-version-check -q --retries 1 --timeout 10 -U yt-dlp
        if ($LASTEXITCODE -ne 0) { Write-Host "yt-dlp update failed, continuing with installed version." -ForegroundColor Yellow }
    } catch { Write-Host "yt-dlp update skipped ($($_.Exception.Message)), continuing." -ForegroundColor Yellow }
}

# Sanity check: fail with a clear message only if imports are actually broken.
& $py -c "import fastapi, uvicorn, yt_dlp" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Dependencies missing and offline install impossible. Connect to the internet once and re-run .\run.ps1." -ForegroundColor Red
    exit 1
}

# ffmpeg: make it visible to this session if winget installed it
$ff = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg_*" -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if ($ff) { $env:Path = "$($ff.DirectoryName);$env:Path" }

Write-Host "Starting Media Downloader on http://0.0.0.0:8000 (local: http://127.0.0.1:8000)" -ForegroundColor Cyan
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
