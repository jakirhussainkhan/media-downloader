/* ==========================================================================
   Media Downloader — frontend client (v1.0.0)
   API + SSE behavior is identical to the original client; only the DOM
   this renders into was redesigned (glassmorphism theme).
   Endpoints used: POST /api/info, POST /api/download, GET /api/jobs,
   GET /api/events (SSE), DELETE /api/jobs[/...], POST /api/jobs/{id}/retry,
   POST /api/upload-cookies, DELETE /api/jobs/{id}?drop=
   ========================================================================== */

const $ = (sel) => document.querySelector(sel);

/* ---------- constants ---------- */

const QUALITIES = {
  video: [
    ["best", "Best quality"],
    ["2160", "4K 2160p"],
    ["1440", "1440p"],
    ["1080", "1080p Full HD"],
    ["720", "720p HD"],
    ["480", "480p"],
    ["360", "360p"],
  ],
  audio: [
    ["mp3", "MP3"],
    ["m4a", "M4A"],
    ["opus", "OPUS"],
  ],
};

const STATUS_LABEL = {
  queued: "Queued",
  downloading: "Downloading",
  processing: "Converting",
  finished: "Completed",
  paused: "Paused",
  error: "Failed",
  cancelled: "Cancelled",
};

const ACTIVE = ["queued", "downloading", "processing"];

/* ---------- state ---------- */

let mode = "video";
const nodes = new Map();

// Live-update policy: the server pushes updates over SSE (/api/jobs is no
// longer polled in a tight loop). Polling is only a fallback for browsers
// without SSE, runs at a relaxed interval and stops as soon as no job is
// queued/downloading/processing.
const POLL_INTERVAL = 2500;
const MAX_FETCH_FAILURES = 3;
// Single persistent SSE connection policy: exactly one EventSource for the
// lifetime of the page. Reconnects use exponential backoff and there is only
// ever one pending reconnect timer, so we never hammer /api/events.
const RECONNECT_BASE = 1000;
const RECONNECT_MAX = 30000;
let currentJobs = [];
let firstJobsLoaded = false;
let pollTimer = null;
let sse = null;
let sseConnected = false;
let reconnectTimer = null;
let reconnectDelay = RECONNECT_BASE;
let visibilityTimer = null;
let offline = false;
let fetchFailures = 0;
let rateLimitedUntil = 0;
let rateLimitTimer = null;
let jobsRequest = null;

// Re-entrancy guards (button spam protection).
let previewBusy = false;
let downloadBusy = null;
let clearBusy = false;
let clearFailedBusy = false;
let cookiesBusy = false;
let searchQuery = "";
const pendingJobActions = new Set();

const els = {
  urls: $("#urls"),
  quality: $("#quality"),
  playlist: $("#playlist"),
  fetch: $("#fetch"),
  download: $("#download"),
  clear: $("#clear"),
  clearFailed: $("#clear-failed"),
  search: $("#search"),
  jobs: $("#jobs"),
  jobsLoading: $("#jobs-loading"),
  empty: $("#empty"),
  preview: $("#preview"),
  formError: $("#form-error"),
  toast: $("#toast"),
  conn: $("#conn"),
  pill: $("#conn-pill"),
  cookiesBtn: $("#cookies-btn"),
  cookiesInput: $("#cookies-input"),
  themeToggle: $("#theme-toggle"),
  confirmModal: $("#confirm-modal"),
  confirmTitle: $("#confirm-title"),
  confirmMessage: $("#confirm-message"),
  confirmOk: $("#confirm-ok"),
  confirmCancel: $("#confirm-cancel"),
};

/* ---------- helpers ---------- */

function refreshIcons() {
  try {
    if (window.lucide && typeof window.lucide.createIcons === "function") {
      window.lucide.createIcons();
    }
  } catch { /* icons are decorative — never break the app */ }
}

function matchesSearch(job) {
  const q = searchQuery.trim().toLowerCase();
  if (!q) return true;
  const title = String(job.title || job.url || "").toLowerCase();
  const status = String(job.status || "").toLowerCase();
  const label = String(STATUS_LABEL[job.status] || "").toLowerCase();
  return title.includes(q) || status.includes(q) || (label && label.includes(q));
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtSpeed(v) {
  if (!v) return "";
  if (v > 1048576) return (v / 1048576).toFixed(1) + " MB/s";
  return Math.round(v / 1024) + " KB/s";
}

function fmtEta(s) {
  if (s === null || s === undefined) return "";
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60);
  return "Time " + m + ":" + String(s % 60).padStart(2, "0");
}

function fmtDur(s) {
  if (!s) return "";
  s = Math.round(s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtSize(b) {
  if (!b) return "";
  if (b > 1e9) return (b / 1e9).toFixed(2) + " GB";
  if (b > 1048576) return (b / 1048576).toFixed(1) + " MB";
  return Math.round(b / 1024) + " KB";
}

/* Stacked glass toasts (top-right). Same toast(msg, isError) call signature
   used across the app; items auto-dismiss after 4s with fade-out, click to
   dismiss early, newest on top, capped to keep the corner tidy. */
function toast(message, isError = false) {
  const box = els.toast;
  if (!box) return;
  const item = document.createElement("div");
  item.className = "toast-item glass show" + (isError ? " error" : "");
  item.setAttribute("role", "status");
  const dot = document.createElement("span");
  dot.className = "toast-dot";
  dot.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  item.append(dot, text);
  box.prepend(item);
  while (box.children.length > 4) box.lastChild.remove();
  const dismiss = () => {
    if (!item.isConnected) return;
    item.classList.add("leaving");
    setTimeout(() => item.remove(), 320);
  };
  item.addEventListener("click", () => {
    clearTimeout(timer);
    dismiss();
  });
  const timer = setTimeout(() => {
    dismiss();
    if (toast._timers) {
      const i = toast._timers.indexOf(timer);
      if (i !== -1) toast._timers.splice(i, 1);
    }
  }, 4000);
  (toast._timers = toast._timers || []).push(timer);
  if (toast._timers.length > 20) toast._timers.splice(0, toast._timers.length - 20);
}

/* Glass confirmation modal for destructive actions. Resolves true when the
   user confirms, false on cancel / backdrop click / Escape. */
let _confirmResolve = null;
function confirmDialog({ title = "Are you sure?", message = "", okLabel = "Confirm" } = {}) {
  return new Promise((resolve) => {
    if (!els.confirmModal) return resolve(true); // no modal in DOM: don't block
    if (_confirmResolve) _confirmResolve(false); // supersede any pending dialog
    _confirmResolve = resolve;
    els.confirmTitle.textContent = title;
    els.confirmMessage.textContent = message;
    const okLabelEl = els.confirmOk.querySelector(".btn-label");
    if (okLabelEl) okLabelEl.textContent = okLabel;
    else els.confirmOk.textContent = okLabel;
    els.confirmModal.classList.remove("hidden");
    refreshIcons();
    els.confirmCancel.focus();
  });
}
function _closeConfirm(value) {
  if (!els.confirmModal || !_confirmResolve) return;
  els.confirmModal.classList.add("hidden");
  const resolve = _confirmResolve;
  _confirmResolve = null;
  resolve(value);
}

function showFormError(message) {
  if (!message) {
    els.formError.classList.add("hidden");
    return;
  }
  els.formError.textContent = message;
  els.formError.classList.remove("hidden");
}

function setConn(state, message) {
  if (els.pill) els.pill.dataset.state = state;
  if (!message) {
    els.conn.classList.add("hidden");
    els.conn.textContent = "";
    els.conn.classList.remove("error");
    return;
  }
  els.conn.textContent = message;
  els.conn.classList.toggle("error", state === "offline");
  els.conn.classList.remove("hidden");
}

function renderQuality() {
  const list = QUALITIES[mode];
  const previous = els.quality.value;
  els.quality.innerHTML = list
    .map(([value, label]) => `<option value="${value}">${label}</option>`)
    .join("");
  if (list.some(([value]) => value === previous)) els.quality.value = previous;
}

function getUrls() {
  return els.urls.value
    .split(/[\n,]+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function isValidUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:";
  } catch {
    return false;
  }
}

// Returns { urls } when everything checks out, otherwise { error }.
function validateLinks() {
  const all = getUrls();
  if (!all.length) return { error: "Paste at least one link first." };

  const invalid = all.filter((link) => !isValidUrl(link));
  if (invalid.length) {
    const extra = invalid.length > 1 ? ` (+${invalid.length - 1} more)` : "";
    return { error: `Not a valid link (needs http:// or https://): ${invalid[0]}${extra}` };
  }

  const seen = new Set();
  const urls = [];
  for (const link of all) {
    if (!seen.has(link)) {
      seen.add(link);
      urls.push(link);
    }
  }
  return { urls };
}

async function postJson(url, payload) {
  let res;
  try {
    res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch {
    throw new Error("Can't reach the server. Check your connection and try again.");
  }

  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 429) {
      const retry = res.headers.get("Retry-After");
      throw new Error(`Too many requests${retry ? ` — retry in ${retry}s` : " — wait a few seconds and try again"}.`);
    }
    const detail = data.detail;
    throw new Error(
      typeof detail === "string" ? detail
        : Array.isArray(detail) ? detail.map((d) => d.msg || JSON.stringify(d)).join(", ")
        : detail ? JSON.stringify(detail)
        : `Request failed (${res.status})`
    );
  }
  return data;
}

// Icon-safe busy toggle: buttons contain an <i data-lucide> + <span class="btn-label">,
// so the label text is swapped on the inner span instead of wiping the icon.
function setBusy(button, busy, label) {
  const labelEl = button.querySelector(".btn-label");
  const target = labelEl || button;
  if (busy && !button.dataset.label) button.dataset.label = target.textContent;
  button.disabled = busy;
  button.classList.toggle("busy", busy);
  if (busy) target.textContent = label || button.dataset.label;
  else if (button.dataset.label) target.textContent = button.dataset.label;
}

/* ---------- preview ---------- */

function renderPreviewSkeleton(count) {
  els.preview.classList.remove("hidden");
  els.preview.innerHTML = Array.from({ length: count }, () => `
    <div class="preview-item">
      <div class="job-thumb skeleton"></div>
      <div class="preview-body" style="flex:1">
        <div class="skeleton" style="height:14px;width:70%;margin-bottom:8px"></div>
        <div class="skeleton" style="height:12px;width:45%"></div>
      </div>
    </div>`).join("");
}

async function fetchInfo() {
  if (previewBusy) return;
  const checked = validateLinks();
  if (checked.error) {
    showFormError(checked.error);
    return;
  }
  showFormError("");

  previewBusy = true;
  setBusy(els.fetch, true, "Loading...");
  renderPreviewSkeleton(Math.min(checked.urls.length, 3));
  try {
    const results = await Promise.all(
      checked.urls.map((url) =>
        postJson("/api/info", { url })
          .then((data) => ({ ...data, url }))
          .catch((err) => ({ ok: false, error: err.message, url }))
      )
    );
    renderPreview(results);
  } finally {
    previewBusy = false;
    setBusy(els.fetch, false, "Preview");
  }
}

function renderPreview(items) {
  if (!items.length) {
    els.preview.classList.add("hidden");
    els.preview.innerHTML = "";
    return;
  }
  els.preview.classList.remove("hidden");
  els.preview.innerHTML = items.map((item) => {
    if (!item.ok) {
      return `
        <div class="preview-item">
          <div class="job-thumb">!</div>
          <div class="preview-body">
            <strong>${esc(item.url)}</strong>
            <span style="color:var(--err)">${esc(item.error || "Could not read this link")}</span>
          </div>
        </div>`;
    }
    const meta = [
      item.uploader,
      fmtDur(item.duration),
      item.type === "playlist" ? `${item.count} items` : "",
      item.heights && item.heights.length ? `up to ${item.heights[0]}p` : "",
    ].filter(Boolean).join(" · ");
    const thumb = item.thumbnail
      ? `<img src="${esc(item.thumbnail)}" alt="" loading="lazy" onerror="this.remove()">`
      : `<span>${item.type === "playlist" ? "&#128193;" : "&#127916;"}</span>`;
    return `
      <div class="preview-item">
        <div class="job-thumb">${thumb}</div>
        <div class="preview-body">
          <strong title="${esc(item.title)}">${esc(item.title)}</strong>
          <span>${esc(meta)}</span>
        </div>
      </div>`;
  }).join("");
}

/* ---------- downloads ---------- */

async function startDownload() {
  if (downloadBusy) return;
  const checked = validateLinks();
  if (checked.error) {
    showFormError(checked.error);
    return;
  }
  showFormError("");

  // Skip links that are already queued or running, so spamming the button
  // (or re-submitting the same URL) never creates duplicate jobs.
  const activeUrls = new Set(
    currentJobs.filter((job) => ACTIVE.includes(job.status)).map((job) => job.url)
  );
  const fresh = checked.urls.filter((url) => !activeUrls.has(url));
  const skipped = checked.urls.length - fresh.length;
  if (!fresh.length) {
    toast("Those links are already in the queue.");
    return;
  }

  downloadBusy = true;
  setBusy(els.download, true, "Queueing...");
  try {
    const data = await postJson("/api/download", {
      urls: fresh,
      mode,
      quality: els.quality.value,
      playlist: els.playlist.checked,
    });
    const suffix = skipped ? ` (${skipped} already queued)` : "";
    toast(`Queued ${data.jobs.length} download${data.jobs.length > 1 ? "s" : ""}${suffix}`);
    await refreshJobs();
  } catch (err) {
    showFormError(err.message);
  } finally {
    downloadBusy = false;
    setBusy(els.download, false, "Download");
  }
}

/* ---------- job list rendering ---------- */

// Auto-download: finished files save straight to the browser's default
// Downloads folder (Content-Disposition: attachment) with no manual click.
const autoDownloaded = new Set();
function autoDownload(job) {
  if (!job || job.status !== "finished" || !job.file) return;
  if (autoDownloaded.has(job.id)) return;
  autoDownloaded.add(job.id);
  const a = document.createElement("a");
  a.href = `/api/jobs/${job.id}/file`;
  a.download = job.file || "";
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function createJobEl(job) {
  const el = document.createElement("article");
  el.className = "job glass job-enter";
  el.dataset.id = job.id;
  el.innerHTML = `
    <div class="job-thumb"></div>
    <div class="job-main">
      <div class="job-top">
        <h3 class="job-title"></h3>
        <span class="badge"></span>
      </div>
      <div class="bar"><div class="fill"></div></div>
      <div class="job-meta"></div>
      <p class="job-error hidden"></p>
    </div>
    <div class="job-actions">
      <button type="button" class="btn ghost retry" data-action="retry">Retry</button>
      <button type="button" class="btn ghost stop" data-action="stop">Stop</button>
      <button type="button" class="btn primary resume" data-action="resume">Resume</button>
    </div>`;
  el._refs = {
    thumb: el.querySelector(".job-thumb"),
    title: el.querySelector(".job-title"),
    badge: el.querySelector(".badge"),
    fill: el.querySelector(".fill"),
    meta: el.querySelector(".job-meta"),
    error: el.querySelector(".job-error"),
    retry: el.querySelector(".retry"),
    stop: el.querySelector(".stop"),
    resume: el.querySelector(".resume"),
  };
  return el;
}

function updateJobEl(el, job) {
  const r = el._refs;
  el.dataset.status = job.status;

  const title = job.title || job.url;
  if (r.title.textContent !== title) r.title.textContent = title;
  r.title.title = title;

  const label = STATUS_LABEL[job.status] || job.status;
  if (r.badge.textContent !== label) r.badge.textContent = label;
  r.badge.dataset.for = job.status;

  const thumbSrc = job.thumbnail || "";
  const thumbImg = r.thumb.querySelector("img");
  if (thumbSrc) {
    if (!thumbImg) {
      const img = document.createElement("img");
      img.src = thumbSrc;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = () => img.remove();
      r.thumb.replaceChildren(img);
    } else if (thumbImg.getAttribute("src") !== thumbSrc) {
      // Thumbnail arrived/updated after the first render (e.g. playlist
      // entries): swap the source instead of keeping the stale image.
      thumbImg.src = thumbSrc;
    }
  } else if (thumbImg) {
    r.thumb.replaceChildren();
  }
  if (!r.thumb.querySelector("img")) {
    const icon = job.mode === "audio" ? "\u266B" : "\u{1F3AC}";
    if (r.thumb.dataset.icon !== icon) {
      r.thumb.dataset.icon = icon;
      r.thumb.textContent = icon;
    }
  } else {
    r.thumb.dataset.icon = "";
  }

  let percent = job.percent || 0;
  if (job.status === "finished") percent = 100;
  percent = Math.max(0, Math.min(100, percent));
  const width = percent + "%";
  if (r.fill.style.width !== width) r.fill.style.width = width;

  const parts = [];
  if (job.status !== "queued") parts.push(`${Math.round(percent)}%`);
  if (job.status === "downloading") {
    const speed = fmtSpeed(job.speed);
    if (speed) parts.push(speed);
    const eta = fmtEta(job.eta);
    if (eta) parts.push(eta);
  }
  if (job.entries && job.entry) parts.push(`item ${job.entry}/${job.entries}`);
  if (job.status === "finished" && job.size) parts.push(fmtSize(job.size));
  const metaHtml = parts.map((p) => `<span>${esc(p)}</span>`).join("");
  if (r.meta.dataset.html !== metaHtml) {
    r.meta.dataset.html = metaHtml;
    r.meta.innerHTML = metaHtml;
  }

  if (job.error) {
    if (r.error.textContent !== job.error) r.error.textContent = job.error;
    r.error.classList.remove("hidden");
    r.error.title = job.error;
  } else {
    r.error.classList.add("hidden");
    r.error.removeAttribute("title");
  }

  const isActive = ACTIVE.includes(job.status);
  const isFailed = job.status === "error";
  const isPaused = job.status === "paused" || job.status === "cancelled";
  // Completed cards: no Save / Remove buttons (auto-download handles saving,
  // Clear finished / Clear Failed handle removal).
  r.retry.classList.toggle("hidden", !isFailed);
  r.stop.classList.toggle("hidden", !isActive);
  r.resume.classList.toggle("hidden", !(isPaused || isFailed));
}

const EMPTY_DEFAULT_HTML = "Your downloads will appear here.";

function renderJobs(jobs) {
  const seen = new Set();
  let visible = 0;
  let created = false;
  for (const job of jobs) {
    seen.add(job.id);
    let el = nodes.get(job.id);
    if (!el) {
      el = createJobEl(job);
      nodes.set(job.id, el);
      els.jobs.appendChild(el);
      created = true;
    }
    updateJobEl(el, job);
    const show = matchesSearch(job);
    el.classList.toggle("hidden", !show);
    if (show) visible += 1;
  }
  for (const [id, el] of nodes) {
    if (!seen.has(id)) {
      el.remove();
      nodes.delete(id);
    }
  }
  if (created) refreshIcons();
  if (jobs.length === 0) {
    els.empty.innerHTML = EMPTY_DEFAULT_HTML;
    els.empty.classList.remove("hidden");
  } else if (visible === 0) {
    els.empty.textContent = "No downloads match your search.";
    els.empty.classList.remove("hidden");
  } else {
    els.empty.classList.add("hidden");
  }
}

function hasActiveJobs() {
  return currentJobs.some((job) => ACTIVE.includes(job.status));
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(refreshJobs, POLL_INTERVAL);
}

function syncLiveUpdates() {
  if (sseConnected || offline || rateLimitedUntil > Date.now()) {
    stopPolling();
    return;
  }
  if (hasActiveJobs()) startPolling();
  else stopPolling();
}

function applyJobs(jobs) {
  // Only auto-download on active -> finished transition.
  // Jobs already finished on first load / refresh are marked as seen
  // so refresh never re-triggers the browser download.
  const prevStatus = new Map(currentJobs.map((j) => [j.id, j.status]));
  const isFirstLoad = !firstJobsLoaded;
  currentJobs = jobs;
  if (isFirstLoad) {
    firstJobsLoaded = true;
    if (els.jobsLoading) {
      els.jobsLoading.remove();
      els.jobsLoading = null;
    }
  }
  renderJobs(jobs);
  for (const job of jobs) {
    if (job.status !== "finished" || !job.file) continue;
    if (autoDownloaded.has(job.id)) continue;
    if (isFirstLoad) {
      autoDownloaded.add(job.id);
      continue;
    }
    const prev = prevStatus.get(job.id);
    if (prev === undefined || ACTIVE.includes(prev)) autoDownload(job);
    else autoDownloaded.add(job.id);
  }
  if (!offline && !rateLimitTimer) setConn(sseConnected ? "live" : "polling", "");
  syncLiveUpdates();
}

function handleRateLimit(res) {
  const retry = Math.max(1, Number(res.headers.get("Retry-After")) || 5);
  rateLimitedUntil = Date.now() + retry * 1000;
  stopPolling();
  setConn("polling", `Server is rate limiting requests — pausing updates for ${retry}s.`);
  clearTimeout(rateLimitTimer);
  rateLimitTimer = setTimeout(() => {
    rateLimitTimer = null;
    rateLimitedUntil = 0;
    setConn(sseConnected ? "live" : "polling", "");
    refreshJobs();
  }, retry * 1000);
}

async function refreshJobs() {
  if (offline || rateLimitedUntil > Date.now()) return;
  if (jobsRequest) jobsRequest.abort();
  const controller = new AbortController();
  jobsRequest = controller;
  try {
    const res = await fetch("/api/jobs", { cache: "no-store", signal: controller.signal });
    if (res.status === 429) {
      handleRateLimit(res);
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    fetchFailures = 0;
    applyJobs(data.jobs || []);
  } catch (err) {
    if (err.name === "AbortError") return;
    fetchFailures += 1;
    if (fetchFailures >= MAX_FETCH_FAILURES) {
      setConn("offline", "Can't reach the server — retrying automatically.");
    }
  } finally {
    if (jobsRequest === controller) jobsRequest = null;
  }
}

/* ---------- SSE: single persistent stream ---------- */

function sseAlive() {
  return Boolean(sse) && sse.readyState !== EventSource.CLOSED && sseConnected;
}

function scheduleReconnect() {
  if (reconnectTimer || offline) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectEvents();
  }, reconnectDelay);
  reconnectDelay = Math.min(RECONNECT_MAX, reconnectDelay * 2);
}

function connectEvents() {
  if (typeof EventSource === "undefined") {
    setConn("polling", "Live updates unavailable in this browser — polling while downloads run.");
    return;
  }
  // Idempotent: never open a second stream while one is connecting/open.
  if (sse && sse.readyState !== EventSource.CLOSED) return;
  if (sse) {
    try { sse.close(); } catch { /* ignore */ }
    sse = null;
  }

  sse = new EventSource("/api/events");
  sse.onopen = () => {
    sseConnected = true;
    fetchFailures = 0;
    reconnectDelay = RECONNECT_BASE;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    setConn("live", "");
    stopPolling();
  };
  sse.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      fetchFailures = 0;
      applyJobs(data.jobs || []);
    } catch {
      /* malformed frame, ignore */
    }
  };
  sse.onerror = () => {
    // Torn down on purpose (pagehide) — do not reconnect or poll.
    if (!sse || sse.readyState === EventSource.CLOSED) return;
    // Close the broken stream and reconnect once via backoff instead of
    // letting the browser retry aggressively while we also poll, which
    // previously created overlapping /api/events connections + /api/jobs
    // fetches on every error frame.
    try { sse.close(); } catch { /* ignore */ }
    sse = null;
    sseConnected = false;
    setConn("offline", "Live updates interrupted — reconnecting…");
    syncLiveUpdates();
    scheduleReconnect();
  };
}

function teardownLiveUpdates() {
  stopPolling();
  if (toast._timers) {
    toast._timers.forEach(clearTimeout);
    toast._timers = [];
  }
  if (els.toast) els.toast.innerHTML = "";
  if (_confirmResolve) _closeConfirm(false);
  clearTimeout(rateLimitTimer);
  clearTimeout(reconnectTimer);
  clearTimeout(visibilityTimer);
  reconnectTimer = null;
  visibilityTimer = null;
  if (jobsRequest) {
    jobsRequest.abort();
    jobsRequest = null;
  }
  if (sse) {
    try { sse.close(); } catch { /* ignore */ }
    sse = null;
  }
  sseConnected = false;
}

// Guard against the script being evaluated twice (e.g. duplicate <script>
// tags or dev-tools re-injection): tear down the previous page instance so
// only one SSE connection and one set of listeners ever exists.
if (window.__mdCleanup) {
  try { window.__mdCleanup(); } catch { /* ignore */ }
}
window.__mdCleanup = teardownLiveUpdates;

window.addEventListener("pagehide", teardownLiveUpdates);

// Restored from the back/forward cache: pagehide tore everything down, so
// rebuild the single SSE stream and refresh the snapshot instead of leaving
// the UI dead until the next visibility change.
window.addEventListener("pageshow", (event) => {
  if (!event.persisted) return;
  offline = !navigator.onLine;
  reconnectDelay = RECONNECT_BASE;
  connectEvents();
  refreshJobs();
});

window.addEventListener("offline", () => {
  offline = true;
  stopPolling();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  setConn("offline", "You're offline — updates resume when the connection returns.");
});

window.addEventListener("online", () => {
  offline = false;
  reconnectDelay = RECONNECT_BASE;
  setConn(sseConnected ? "live" : "polling", "");
  connectEvents();
  refreshJobs();
});

document.addEventListener("visibilitychange", () => {
  // Debounce rapid hide/show flaps and never create a second stream.
  clearTimeout(visibilityTimer);
  if (document.visibilityState === "hidden") {
    stopPolling();
    return;
  }
  visibilityTimer = setTimeout(() => {
    // Tab became visible: if the single persistent stream is healthy, do
    // nothing — no refresh, no reconnect, no static-asset reload. Only
    // repair the connection when it is actually down.
    if (sseAlive()) {
      syncLiveUpdates();
      return;
    }
    connectEvents();
    refreshJobs();
  }, 300);
});

/* ---------- theme: dark / light toggle (persisted) ---------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  const next = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("md-theme", next); } catch { /* private mode — theme just won't persist */ }
  // Sun shows in dark mode (tap for light), Moon shows in light mode.
  if (els.themeToggle) {
    els.themeToggle.setAttribute(
      "aria-label", next === "dark" ? "Switch to light theme" : "Switch to dark theme"
    );
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", next === "dark" ? "#07070f" : "#eef2ff");
  const scheme = document.querySelector('meta[name="color-scheme"]');
  if (scheme) scheme.setAttribute("content", next);
}

if (els.themeToggle) {
  // Honor the pre-paint choice from the inline head script, then wire up.
  applyTheme(currentTheme());
  els.themeToggle.addEventListener("click", () => {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
}

/* ---------- controls ---------- */

document.querySelectorAll("#mode button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("#mode button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    mode = btn.dataset.mode;
    renderQuality();
  });
});

els.fetch.addEventListener("click", fetchInfo);
els.download.addEventListener("click", startDownload);
els.urls.addEventListener("input", () => showFormError(""));

// Cookies.txt upload (private / login-gated videos): button opens the file
// picker, the file is POSTed to /api/upload-cookies.
if (els.cookiesBtn && els.cookiesInput) {
  els.cookiesBtn.addEventListener("click", () => els.cookiesInput.click());
  els.cookiesInput.addEventListener("change", async () => {
    const file = els.cookiesInput.files && els.cookiesInput.files[0];
    if (!file) return;
    if (cookiesBusy) return;
    cookiesBusy = true;
    setBusy(els.cookiesBtn, true, "Uploading...");
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      let res;
      try {
        res = await fetch("/api/upload-cookies", { method: "POST", body: form });
      } catch {
        toast("Can't reach the server.", true);
        return;
      }
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        toast(data.detail || `Upload failed (${res.status})`, true);
        return;
      }
      toast(`Cookies saved (${data.size || file.size} bytes) — private videos unlocked.`);
    } finally {
      cookiesBusy = false;
      setBusy(els.cookiesBtn, false);
      els.cookiesInput.value = "";
    }
  });
}

if (els.search) {
  els.search.addEventListener("input", () => {
    searchQuery = els.search.value || "";
    renderJobs(currentJobs);
  });
}

// Confirmation modal wiring: buttons, backdrop click, Escape.
if (els.confirmModal) {
  els.confirmOk.addEventListener("click", () => _closeConfirm(true));
  els.confirmCancel.addEventListener("click", () => _closeConfirm(false));
  els.confirmModal.querySelector("[data-close]").addEventListener("click", () => _closeConfirm(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !els.confirmModal.classList.contains("hidden")) _closeConfirm(false);
  });
}

// Keyboard shortcut: Ctrl+Enter / Cmd+Enter queues downloads from anywhere.
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    els.download.click();
  }
});

// Drag & drop: drop links (or a .txt of links) straight into the textarea.
// The glowing border is handled by the .drag-over class in CSS.
if (els.urls) {
  let dragDepth = 0;
  const setDragGlow = (on) => els.urls.classList.toggle("drag-over", on);
  els.urls.addEventListener("dragenter", (e) => {
    e.preventDefault();
    dragDepth += 1;
    setDragGlow(true);
  });
  els.urls.addEventListener("dragover", (e) => {
    e.preventDefault();
    setDragGlow(true);
  });
  els.urls.addEventListener("dragleave", (e) => {
    e.preventDefault();
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) setDragGlow(false);
  });
  els.urls.addEventListener("drop", async (e) => {
    e.preventDefault();
    dragDepth = 0;
    setDragGlow(false);
    const dt = e.dataTransfer;
    if (!dt) return;
    let text = dt.getData("text/plain") || dt.getData("text/uri-list") || "";
    if (!text && dt.files && dt.files.length) {
      const parts = [];
      for (const file of dt.files) {
        try {
          if (file.type.startsWith("text") || /\.txt$/i.test(file.name)) {
            parts.push(await file.text());
          }
        } catch { /* unreadable file — skip */ }
      }
      text = parts.join("\n");
    }
    text = text.trim();
    if (!text) return;
    const current = els.urls.value.trim();
    els.urls.value = current ? current + "\n" + text : text;
    showFormError("");
    toast("Links added — hit Download when ready.");
  });
}

els.clear.addEventListener("click", async () => {
  if (clearBusy) return;
  const ok = await confirmDialog({
    title: "Clear finished?",
    message: "This removes all completed and stopped entries from the list. Paused downloads are kept so you can resume them.",

    okLabel: "Clear finished",
  });
  if (!ok) return;
  clearBusy = true;
  setBusy(els.clear, true);
  try {
    let res;
    try {
      res = await fetch("/api/jobs", { method: "DELETE" });
    } catch {
      toast("Can't reach the server.", true);
      return;
    }
    if (res.status === 429) {
      handleRateLimit(res);
      return;
    }
    if (!res.ok) {
      toast(`Clear failed (${res.status})`, true);
      return;
    }
    const data = await res.json();
    toast(`Cleared ${data.removed} entr${data.removed === 1 ? "y" : "ies"}`);
    await refreshJobs();
  } finally {
    clearBusy = false;
    setBusy(els.clear, false);
  }
});

if (els.clearFailed) {
  els.clearFailed.addEventListener("click", async () => {
    if (clearFailedBusy) return;
    const ok = await confirmDialog({
      title: "Clear failed?",
      message: "This removes all failed downloads from the list. You can retry them first if needed.",
      okLabel: "Clear failed",
    });
    if (!ok) return;
    clearFailedBusy = true;
    setBusy(els.clearFailed, true);
    try {
      let res;
      try {
        res = await fetch("/api/jobs?status=failed", { method: "DELETE" });
      } catch {
        toast("Can't reach the server.", true);
        return;
      }
      if (res.status === 429) {
        handleRateLimit(res);
        return;
      }
      if (!res.ok) {
        toast(`Clear failed (${res.status})`, true);
        return;
      }
      const data = await res.json();
      toast(`Cleared ${data.removed} failed entr${data.removed === 1 ? "y" : "ies"}`);
      await refreshJobs();
    } finally {
      clearFailedBusy = false;
      setBusy(els.clearFailed, false);
    }
  });
}

els.jobs.addEventListener("click", async (event) => {
  const btn = event.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.closest(".job").dataset.id;
  if (pendingJobActions.has(id)) return;
  pendingJobActions.add(id);
  btn.disabled = true;
  try {
    if (btn.dataset.action === "retry") {
      let res;
      try {
        res = await fetch(`/api/jobs/${id}/retry`, { method: "POST" });
      } catch {
        toast("Can't reach the server.", true);
        return;
      }
      if (res.status === 429) {
        handleRateLimit(res);
        return;
      }
      if (res.status === 404) {
        toast("Job not found.", true);
        return;
      }
      if (!res.ok) {
        toast(`Retry failed (${res.status})`, true);
        return;
      }
      toast("Retrying download…");
      await refreshJobs();
      return;
    }
    if (btn.dataset.action === "stop" || btn.dataset.action === "pause") {
      let res;
      try {
        res = await fetch(`/api/jobs/${id}/stop`, { method: "POST" });
      } catch {
        toast("Can't reach the server.", true);
        return;
      }
      if (res.status === 429) {
        handleRateLimit(res);
        return;
      }
      if (!res.ok && res.status !== 404) {
        toast(`Stop failed (${res.status})`, true);
        return;
      }
      toast("Download stopped — Resume anytime.");
      await refreshJobs();
      return;
    }
    if (btn.dataset.action === "resume") {
      autoDownloaded.delete(id);
      let res;
      try {
        res = await fetch(`/api/jobs/${id}/resume`, { method: "POST" });
      } catch {
        toast("Can't reach the server.", true);
        return;
      }
      if (res.status === 429) {
        handleRateLimit(res);
        return;
      }
      if (!res.ok && res.status !== 404) {
        const data = await res.json().catch(() => ({}));
        toast(data.detail || `Resume failed (${res.status})`, true);
        return;
      }
      toast("Resuming download…");
      await refreshJobs();
      return;
    }
    // Legacy fallback (no Save/Remove buttons in UI anymore).
    const drop = btn.dataset.action === "remove";
    let res;
    try {
      res = await fetch(`/api/jobs/${id}?drop=${drop}`, { method: "DELETE" });
    } catch {
      toast("Can't reach the server.", true);
      return;
    }
    if (res.status === 429) {
      handleRateLimit(res);
      return;
    }
    if (!res.ok && res.status !== 404) {
      toast(`Request failed (${res.status})`, true);
      return;
    }
    await refreshJobs();
  } finally {
    pendingJobActions.delete(id);
    btn.disabled = false;
  }
});

/* ---------- boot ---------- */

offline = !navigator.onLine;
renderQuality();
refreshIcons();
connectEvents();
refreshJobs();
if (offline) setConn("offline", "You're offline — updates resume when the connection returns.");
// Render Lucide icons injected after async DOM work (dynamic job nodes).
if (document.readyState === "complete") refreshIcons();
else window.addEventListener("load", refreshIcons);
