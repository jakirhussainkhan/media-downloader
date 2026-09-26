# Changelog — Click-to-Play Inline Preview

Adapted to this repo's real stack (`main.py` + `downloader.py`,
`POST /api/info`, port 8000, vanilla glass CSS). No new dependencies,
no redesign, download/MP3 pipeline untouched.

## Phase 1 — Backend fields (`downloader.py` only)
- `probe()` response gains additive fields (old fields unchanged):
  `platform` (youtube|facebook|instagram|pinterest|tiktok|twitter|generic,
  detected from URL hostname), `video_id` (YouTube only — handles
  youtu.be, watch?v=, shorts, embed, live + extra params), `embed_url`
  (`.../embed/ID?rel=0&modestbranding=1&playsinline=1`, YouTube only),
  `direct_url` (best progressive MP4 from the extractor's formats for
  single non-YouTube videos, else null), `media` (`video`|`image`).
- Empty thumbnails now fall back to a `placehold.co` image.
- New helpers: `detect_platform`, `extract_youtube_id`,
  `youtube_embed_url`, `pick_direct_url`, `is_image_url`.

## Phase 2 — Frontend player (`static/app.js`, `static/style.css`)
- Preview cards rebuilt with DOM APIs (no untrusted innerHTML):
  clickable thumbnail + title + meta + per-card Download button.
- YouTube → 16:9 IFrame embed in-card (native resolution + fullscreen),
  skeleton while loading, `✕` toggle, persistent "Watch on YouTube ↗".
- Other videos → native `<video controls playsinline preload="metadata"
  poster>`; only metadata loads until play.
- Images → shared full-size lightbox (ESC/backdrop closes, Download
  Original queues via the standard `/api/download` flow).
- Single-open rule: opening a card auto-collapses the previous one;
  closing destroys the player node (stops playback + bytes).

## Phase 3 — Error handling
- Embed not loaded within 3s → "Video can't be embedded.
  Watch on YouTube →" fallback link.
- `<video>` error (e.g. expired 403 CDN URL) → "Preview expired.
  Re-extract ↻" button re-calls `/api/info` for that URL only.
- Server unreachable → existing toast error, no crash.
