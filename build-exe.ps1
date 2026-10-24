param([switch]$SkipFfmpeg)
# Media Downloader - one-click EXE builder
# Usage:
#   .\build-exe.ps1            # full build (ffmpeg bundle + portable exe)
#   .\build-exe.ps1 -SkipFfmpeg  # skip ffmpeg download (smaller, needs system ffmpeg)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "Creating venv..." -ForegroundColor Cyan
    python -m venv .venv
}
Write-Host "Installing dependencies..." -ForegroundColor Cyan
& $py -m pip install --disable-pip-version-check -q -r requirements.txt
& $py -m pip install --disable-pip-version-check -q pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# --- ffmpeg bundle (so users never install ffmpeg separately) ---
$binDir = Join-Path $root "bin"
if (-not $SkipFfmpeg) {
    $needDl = -not ((Test-Path (Join-Path $binDir "ffmpeg.exe")) -and (Test-Path (Join-Path $binDir "ffprobe.exe")))
    if ($needDl) {
        Write-Host "Downloading ffmpeg (gyan.dev essentials)..." -ForegroundColor Cyan
        New-Item -ItemType Directory -Path $binDir -Force | Out-Null
        $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
        $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        try {
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
            $tmp = Join-Path $env:TEMP "ffmpeg-dl"
            if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $ff = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            $fp = Get-ChildItem $tmp -Recurse -Filter ffprobe.exe | Select-Object -First 1
            if ($ff) { Copy-Item $ff.FullName (Join-Path $binDir "ffmpeg.exe") -Force }
            if ($fp) { Copy-Item $fp.FullName (Join-Path $binDir "ffprobe.exe") -Force }
            Remove-Item -Force $zip -ErrorAction SilentlyContinue
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
        } catch {
            Write-Host "WARNING: ffmpeg download failed ($($_.Exception.Message)). Continuing without bundle." -ForegroundColor Yellow
        }
    }
    if ((Test-Path (Join-Path $binDir "ffmpeg.exe")) -and (Test-Path (Join-Path $binDir "ffprobe.exe"))) {
        Write-Host "ffmpeg bundle: OK" -ForegroundColor Green
    } else {
        Write-Host "WARNING: bin/ffmpeg.exe not found - EXE will use system ffmpeg instead." -ForegroundColor Yellow
    }
}

# --- app icon (generated once, reused by spec + installer) ---
$iconPath = Join-Path $root "assets\icon.ico"
if (-not (Test-Path $iconPath)) {
    Write-Host "Generating icon..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Path (Split-Path $iconPath) -Force | Out-Null
    & $py -c "from PIL import Image, ImageDraw; s=256; im=Image.new('RGBA',(s,s),(0,0,0,0)); d=ImageDraw.Draw(im); d.rounded_rectangle([8,8,s-8,s-8],radius=56,fill=(24,24,40,255)); d.rounded_rectangle([8,8,s-8,s-8],radius=56,outline=(139,92,246,255),width=12); d.rectangle([s//2-20,56,s//2+20,152],fill=(255,255,255,255)); d.polygon([(s//2-48,144),(s//2+48,144),(s//2,200)],fill=(59,130,246,255)); im.save(r'$iconPath')"
}

# --- build ---
Write-Host "Running PyInstaller..." -ForegroundColor Cyan
& $py -m PyInstaller MediaDownloader.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $root "dist\MediaDownloader.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe not found" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Built: $exe ($size MB)" -ForegroundColor Green

# --- portable zip (single exe, one-click) ---
$zipOut = Join-Path $root "dist\MediaDownloader-Portable.zip"
if (Test-Path $zipOut) { Remove-Item -Force $zipOut }
Compress-Archive -Path $exe -DestinationPath $zipOut
Write-Host "Portable zip: $zipOut" -ForegroundColor Green
Write-Host ""
Write-Host "Done. Double-click dist\MediaDownloader.exe to run. For Setup.exe, install Inno Setup and compile installer.iss." -ForegroundColor Cyan
