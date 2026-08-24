// YouTube Downloader — Frontend

const ACTIVE_STATUSES = [
  "pending",
  "probing",
  "analyzing",
  "downloading",
  "moving",
  "ordering",
  "finalizing",
  "cancelling",
];

const DAY_NAMES = [
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
  "Sunday",
];
const COMPLETED_STATUSES = ["completed", "failed", "cancelled"];
const ALL_STATUS_CLASSES = [...ACTIVE_STATUSES, ...COMPLETED_STATUSES];

// Resolution priority: highest first
const RES_PRIORITY = ["2160p", "1440p", "1080p", "720p"];

const tasks = new Map();
let ws = null;
let wsReconnectTimer = null;
let activeToastTimer = null;
let probeTimer = null;
let selectedRes = null;
let jellyfinLibraries = []; // cached list from /api/jellyfin/libraries
let selectedJfLibrary = null; // { id, name, type, path } or null
let jellyfinEnabled = false;
let currentTab = "downloads";
let monitors = [];
let selectedMonitorJfLibrary = null;
let monitorTasks = new Map(); // monitor_id → active task
let musicModeActive = false;
let folderBrowserBase = ""; // root path of selected library
let folderBrowserCurrent = ""; // current path being browsed (absolute)
let selectedFolderOverride = null; // relative path the user picked (e.g. "Mixes")
let defaultMusicFolder = null; // { jellyfin_library_id, jellyfin_library_name, jellyfin_library_path, jellyfin_library_type, folder_override } or null
let lastProbe = null; // { title, channel, is_playlist, thumbnail } from the most recent successful /api/probe
let autoDownloaded = new Set(); // task ids already handed to the browser via the auto-save flow
let editingMonitorId = null; // monitor id currently loaded into the Add Monitor form for editing, or null
let monitorScheduleEl = null;
let monitorTimeEl = null;
let monitorDayEl = null;

// ── DOM helpers ───────────────────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);

const els = {
  form: () => $("download-form"),
  urlInput: () => $("url-input"),
  hiddenRes: () => $("resolution-select"),
  submitBtn: () => $("submit-btn"),
  diskWarning: () => $("disk-warning"),
  activeList: () => $("active-list"),
  activeEmpty: () => $("active-empty"),
  completedList: () => $("completed-list"),
  completedEmpty: () => $("completed-empty"),
  activeCount: () => $("active-count"),
  completedCount: () => $("completed-count"),
  clearBtn: () => $("clear-completed-btn"),
  template: () => $("task-template"),
  playlistIndexArea: () => $("playlist-index-area"),
  playlistIndexCheckbox: () => $("playlist-index-checkbox"),
  monitorPlaylistIndexCheckbox: () => $("monitor-playlist-index-checkbox"),
  // resolution area
  resArea: () => $("res-area"),
  resHint: () => $("res-hint"),
  resProbing: () => $("res-probing"),
  resButtons: () => $("res-buttons"),
  resBtn720: () => $("res-btn-720p"),
  resBtn1080: () => $("res-btn-1080p"),
  resBtn1440: () => $("res-btn-1440p"),
  resBtn2160: () => $("res-btn-2160p"),
  resBtnAudio: () => $("res-btn-audio"),
  // probe info strip
  probeInfo: () => $("probe-info"),
  probeThumb: () => $("probe-thumb"),
  probeTitle: () => $("probe-title"),
  probeChannel: () => $("probe-channel"),
  probeBadge: () => $("probe-badge"),
  jellyfinArea: () => $("jellyfin-area"),
  jellyfinToggle: () => $("jellyfin-toggle"),
  jellyfinBtnText: () => $("jellyfin-btn-text"),
  jellyfinChevron: () => $("jellyfin-chevron"),
  jellyfinPicker: () => $("jellyfin-picker"),
  jellyfinGrid: () => $("jellyfin-picker-grid"),
  folderBrowser: () => $("folder-browser"),
  folderBrowserGrid: () => $("folder-browser-grid"),
  folderBrowserPath: () => $("folder-browser-path"),
  folderBrowserBack: () => $("folder-browser-back"),
  folderBrowserClear: () => $("folder-browser-clear"),
  folderBrowserDefault: () => $("folder-browser-default"),
  audioModeRow: () => $("audio-mode-row"),
};

// ── Utilities ─────────────────────────────────────────────────────────────────

const truncateUrl = (url, maxLen = 60) =>
  url.length > maxLen ? url.slice(0, maxLen) + "…" : url;

const isMusicUrl = (url) => {
  try {
    return new URL(url).hostname === "music.youtube.com";
  } catch {
    return false;
  }
};

const updateCounts = () => {
  let active = 0,
    completed = 0;
  for (const t of tasks.values()) {
    // Monitor-triggered downloads show their progress on the Monitor tab's
    // card instead — they don't count toward the Downloads tab's lists.
    if (t.monitor_id) continue;
    if (ACTIVE_STATUSES.includes(t.status)) active++;
    else if (COMPLETED_STATUSES.includes(t.status)) completed++;
  }
  els.activeCount().textContent = active;
  els.completedCount().textContent = completed;
  els.activeEmpty().hidden = active > 0;
  els.completedEmpty().hidden = completed > 0;
};

// ── Toast ─────────────────────────────────────────────────────────────────────

const showToast = (msg, type = "info") => {
  let toast = document.querySelector(".toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.className = "toast";
    document.body.appendChild(toast);
  }
  clearTimeout(activeToastTimer);
  toast.textContent = msg;
  toast.className = `toast toast-${type} show`;
  activeToastTimer = setTimeout(() => toast.classList.remove("show"), 3500);
};

// ── Probe info strip ──────────────────────────────────────────────────────────

const showProbeInfo = (
  title,
  channel,
  isPlaylist,
  thumbnail = null,
  typeOverride = null,
) => {
  if (!title && !channel) {
    hideProbeInfo();
    return;
  }

  const thumbEl = els.probeThumb();
  if (thumbnail) {
    thumbEl.src = thumbnail;
    thumbEl.hidden = false;
  } else {
    thumbEl.hidden = true;
    thumbEl.removeAttribute("src");
  }

  els.probeTitle().textContent = title || "";
  els.probeChannel().textContent = channel || "";

  const badge = els.probeBadge();
  if (typeOverride === "audio") {
    badge.textContent = isPlaylist ? "ALBUM" : "AUDIO";
    badge.className = "probe-badge audio";
  } else {
    badge.textContent = isPlaylist ? "Playlist" : "Video";
    badge.className = isPlaylist ? "probe-badge playlist" : "probe-badge";
  }

  els.probeInfo().hidden = false;

  // The numbering toggle only makes sense for playlists/channels, which yt-dlp
  // both report as a "playlist" type. Hide it for single videos.
  const indexArea = els.playlistIndexArea();
  if (indexArea) indexArea.hidden = !isPlaylist;
};

const hideProbeInfo = () => {
  els.probeInfo().hidden = true;
  els.probeThumb().hidden = true;
  els.probeThumb().removeAttribute("src");
  const indexArea = els.playlistIndexArea();
  if (indexArea) indexArea.hidden = true;
};

// ── Jellyfin ──────────────────────────────────────────────────────────────────

// ── Folder browser ────────────────────────────────────────────────────────────

const folderRelative = (absPath) => {
  // Strip the library root prefix to get a relative path for folder_override
  if (absPath === folderBrowserBase) return "";
  if (absPath.startsWith(folderBrowserBase + "/")) {
    return absPath.slice(folderBrowserBase.length + 1);
  }
  return absPath;
};

const renderFolderBrowser = async (path) => {
  folderBrowserCurrent = path;
  const rel = folderRelative(path);
  els.folderBrowserPath().textContent = rel || "(library root)";
  els.folderBrowserBack().hidden = path === folderBrowserBase;
  updateSetDefaultButton();

  const grid = els.folderBrowserGrid();
  grid.innerHTML = '<span class="folder-loading">Loading…</span>';

  try {
    const resp = await fetch(`/api/browse?path=${encodeURIComponent(path)}`);
    const data = await resp.json();
    grid.innerHTML = "";

    if (!data.folders || data.folders.length === 0) {
      grid.innerHTML = '<span class="folder-empty">No subfolders found</span>';
      return;
    }

    data.folders.forEach((name) => {
      const absPath = path + "/" + name;
      const relPath = folderRelative(absPath);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className =
        "folder-btn" + (selectedFolderOverride === relPath ? " selected" : "");
      btn.innerHTML = `<span class="folder-name">${name}</span>`;
      btn.addEventListener("click", () =>
        selectFolderOverride(absPath, relPath, name),
      );
      grid.appendChild(btn);
    });
  } catch {
    grid.innerHTML = '<span class="folder-empty">Could not load folders</span>';
  }
};

const selectFolderOverride = async (absPath, relPath, name) => {
  selectedFolderOverride = relPath;
  // Update button text to show selected folder
  els.jellyfinBtnText().textContent =
    (selectedJfLibrary?.name || "Jellyfin") + " › " + relPath;
  els.submitBtn().disabled = !selectedRes;
  // Drill into sub-folder to let user go deeper
  await renderFolderBrowser(absPath);
};

const openFolderBrowser = async (libraryPath) => {
  folderBrowserBase = libraryPath;
  folderBrowserCurrent = libraryPath;
  selectedFolderOverride = null;
  els.folderBrowser().hidden = false;
  await renderFolderBrowser(libraryPath);
};

const closeFolderBrowser = () => {
  els.folderBrowser().hidden = true;
  folderBrowserBase = "";
  folderBrowserCurrent = "";
  selectedFolderOverride = null;
};

// ── Default music folder ─────────────────────────────────────────────────────
// Lets the user pin a Jellyfin music library (+ optional subfolder) so it's
// selected automatically whenever a music.youtube.com link is pasted.

const fetchDefaultMusicFolder = async () => {
  try {
    const resp = await fetch("/api/settings/default-music-folder");
    if (!resp.ok) return;
    const data = await resp.json();
    defaultMusicFolder = data && data.jellyfin_library_id ? data : null;
  } catch {
    // Non-fatal — the default just won't be auto-applied this session.
  }
};

const isCurrentDefaultMusicFolder = () =>
  !!defaultMusicFolder &&
  !!selectedJfLibrary &&
  defaultMusicFolder.jellyfin_library_id === selectedJfLibrary.id &&
  (defaultMusicFolder.folder_override || null) ===
    (selectedFolderOverride || null);

const updateSetDefaultButton = () => {
  const btn = els.folderBrowserDefault();
  if (!btn) return;
  const isDefault = isCurrentDefaultMusicFolder();
  btn.textContent = isDefault ? "Default for Music links" : "Set as default";
  btn.classList.toggle("is-default", isDefault);
};

const handleSetDefaultMusicFolder = async () => {
  if (!selectedJfLibrary) return;
  const btn = els.folderBrowserDefault();
  btn.disabled = true;
  try {
    if (isCurrentDefaultMusicFolder()) {
      await fetch("/api/settings/default-music-folder", { method: "DELETE" });
      defaultMusicFolder = null;
      showToast("Default music folder cleared", "success");
    } else {
      const resp = await fetch("/api/settings/default-music-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jellyfin_library_id: selectedJfLibrary.id,
          jellyfin_library_name: selectedJfLibrary.name,
          jellyfin_library_path: selectedJfLibrary.path,
          jellyfin_library_type: selectedJfLibrary.type,
          folder_override: selectedFolderOverride || null,
        }),
      });
      if (!resp.ok) throw new Error("failed");
      defaultMusicFolder = await resp.json();
      showToast("Set as default folder for Music links", "success");
    }
  } catch {
    showToast("Failed to update default music folder", "error");
  } finally {
    btn.disabled = false;
    updateSetDefaultButton();
  }
};

// Auto-select the saved default music library/folder for a music.youtube.com
// link. Returns true if applied so the caller can skip its own fallback.
const applyDefaultMusicFolder = () => {
  if (!defaultMusicFolder?.jellyfin_library_id) return false;
  const lib = jellyfinLibraries.find(
    (l) => l.id === defaultMusicFolder.jellyfin_library_id,
  );
  if (!lib) return false;

  selectedJfLibrary = lib;
  selectedFolderOverride = defaultMusicFolder.folder_override || null;
  renderJellyfinLibraries();
  els.jellyfinBtnText().textContent = selectedFolderOverride
    ? `${lib.name} › ${selectedFolderOverride}`
    : lib.name;
  els.submitBtn().disabled = !selectedRes;
  // Keep the subfolder browser collapsed — the default already covers it.
  // The library picker stays open (via setJellyfinOpen) so the choice is visible.
  els.folderBrowser().hidden = true;
  return true;
};

const renderJellyfinLibraries = () => {
  const grid = els.jellyfinGrid();
  grid.innerHTML = "";
  const libs = musicModeActive
    ? jellyfinLibraries.filter((l) =>
        (l.type_label || l.type || "").toLowerCase().includes("music"),
      )
    : jellyfinLibraries;
  libs.forEach((lib) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "jellyfin-lib-btn" +
      (selectedJfLibrary?.id === lib.id ? " selected" : "");
    btn.dataset.id = lib.id;
    // Show the Jellyfin-internal path for display; host path is used for writing
    const displayPath = lib.jellyfin_path || lib.path;
    btn.innerHTML = `
      <span class="jf-lib-name">${lib.name}</span>
      <span class="jf-lib-type">${lib.type_label}</span>
      <span class="jf-lib-path" title="${lib.path}">${displayPath}</span>
    `;
    btn.addEventListener("click", () => selectJfLibrary(lib));
    grid.appendChild(btn);
  });
};

const selectJfLibrary = (lib) => {
  selectedJfLibrary = lib;
  selectedFolderOverride = null;
  renderJellyfinLibraries();
  els.jellyfinBtnText().textContent = lib.name;
  els.submitBtn().disabled = !selectedRes;
  // Only open the folder browser for music libraries
  if ((lib.type || "").toLowerCase() === "music") {
    openFolderBrowser(lib.path);
  } else {
    closeFolderBrowser();
  }
};

const clearJfSelection = () => {
  selectedJfLibrary = null;
  selectedFolderOverride = null;
  els.jellyfinBtnText().textContent = "Save to Jellyfin";
  renderJellyfinLibraries();
  closeFolderBrowser();
};

const setJellyfinOpen = (open) => {
  const toggle = els.jellyfinToggle();
  const picker = els.jellyfinPicker();
  toggle.classList.toggle("active", open);
  toggle.setAttribute("aria-pressed", String(open));
  picker.hidden = !open;
  if (open && jellyfinLibraries.length === 0) {
    // Fetch libraries on first open
    fetchJellyfinLibraries();
  }
};

const handleJellyfinToggle = () => {
  const isOpen = !els.jellyfinPicker().hidden;
  if (isOpen) {
    // Close — also clear selection
    setJellyfinOpen(false);
    clearJfSelection();
  } else {
    setJellyfinOpen(true);
  }
};

const fetchJellyfinLibraries = async () => {
  try {
    const resp = await fetch("/api/jellyfin/libraries");
    if (!resp.ok) throw new Error("failed");
    const data = await resp.json();
    jellyfinLibraries = data.libraries || [];
    renderJellyfinLibraries();
  } catch {
    showToast("Could not load Jellyfin libraries", "error");
  }
};

const initJellyfin = async () => {
  try {
    const resp = await fetch("/api/jellyfin/status");
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.enabled) {
      jellyfinEnabled = true;
      els.jellyfinArea().hidden = false;
      // Show Jellyfin section on Monitor tab too
      const mja = document.getElementById("monitor-jellyfin-area");
      if (mja) mja.hidden = false;
      // Monitors run unattended and always need a Jellyfin destination —
      // without Jellyfin configured, the whole tab has nothing usable in it.
      const monitorTabBtn = document.getElementById("monitor-tab-btn");
      if (monitorTabBtn) monitorTabBtn.hidden = false;
      // Pre-fetch libraries so first open is instant
      await fetchJellyfinLibraries();
    }
  } catch {
    // Jellyfin not configured — silently skip
  }
};

// ── Resolution picker ─────────────────────────────────────────────────────────

const setResState = (state) => {
  els.resArea().hidden = state === "idle";
  els.resHint().hidden = state !== "error";
  els.resProbing().hidden = state !== "probing";
  els.resButtons().hidden = state !== "buttons";
  els.audioModeRow().hidden = state !== "audio";

  if (state === "error") {
    els.resHint().textContent = "Could not read video info — check the URL";
    els.resHint().classList.add("res-hint--error");
  } else {
    els.resHint().classList.remove("res-hint--error");
  }
};

const allResBtns = () =>
  [
    els.resBtn720(),
    els.resBtn1080(),
    els.resBtn1440(),
    els.resBtn2160(),
    els.resBtnAudio(),
  ].filter(Boolean);

const clearResSelection = () => {
  selectedRes = null;
  els.hiddenRes().value = "";
  allResBtns().forEach((btn) => btn.classList.remove("active"));
  els.submitBtn().disabled = true;
  musicModeActive = false;
};

// Select a specific resolution button programmatically. Also handles the
// "Audio only" button, which lives alongside the quality buttons (rather
// than switching to the separate audio-mode UI reserved for music.youtube.com
// links) so a plain video link can be downloaded as audio in one click.
const selectResolution = (res) => {
  const map = {
    "720p": els.resBtn720(),
    "1080p": els.resBtn1080(),
    "1440p": els.resBtn1440(),
    "2160p": els.resBtn2160(),
    audio: els.resBtnAudio(),
  };
  allResBtns().forEach((btn) => btn.classList.remove("active"));
  const btn = map[res];
  if (btn && !btn.hidden) {
    btn.classList.add("active");
    selectedRes = res;
    els.hiddenRes().value = res;
    els.submitBtn().disabled = false;
    musicModeActive = res === "audio";
    renderJellyfinLibraries(); // re-render with/without the music-only filter
    if (lastProbe) {
      showProbeInfo(
        lastProbe.title,
        lastProbe.channel,
        lastProbe.is_playlist,
        lastProbe.thumbnail,
        res === "audio" ? "audio" : null,
      );
    }
  }
};

const applyProbeResult = ({ available, title, channel, is_playlist, thumbnail }) => {
  // Show/hide buttons based on what the video actually has
  els.resBtn720().hidden = !available.includes("720p");
  els.resBtn1080().hidden = !available.includes("1080p");
  els.resBtn1440().hidden = !available.includes("1440p");
  els.resBtn2160().hidden = !available.includes("2160p");
  els.resBtnAudio().hidden = false; // audio extraction works regardless of video formats

  clearResSelection();
  setResState("buttons");
  lastProbe = { title, channel, is_playlist, thumbnail };

  // Auto-select the highest available quality
  const best = RES_PRIORITY.find((r) => available.includes(r));
  if (best) selectResolution(best);

  showProbeInfo(title, channel, is_playlist, thumbnail);
};

const applyAudioMode = ({ title, channel, is_playlist, thumbnail }) => {
  setResState("audio");
  selectedRes = "audio";
  els.hiddenRes().value = "audio";
  els.submitBtn().disabled = false;
  musicModeActive = true;
  lastProbe = { title, channel, is_playlist, thumbnail };
  renderJellyfinLibraries(); // re-render with music filter
  showProbeInfo(title, channel, is_playlist, thumbnail, "audio");

  // Auto-expand Jellyfin and select a destination so downloading needs no
  // extra clicks: prefer the user's saved default music folder, falling
  // back to the single music library (if there's exactly one) with its
  // subfolder browser open for manual choice.
  if (jellyfinEnabled) {
    setJellyfinOpen(true);
    if (!applyDefaultMusicFolder()) {
      const musicLibs = jellyfinLibraries.filter(
        (l) => (l.type || "").toLowerCase() === "music",
      );
      if (musicLibs.length === 1) {
        selectJfLibrary(musicLibs[0]);
      }
    }
  }
};

const probeUrl = async (url) => {
  clearResSelection();
  hideProbeInfo();
  setResState("probing");
  lastProbe = null;

  try {
    const resp = await fetch("/api/probe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    if (!resp.ok) throw new Error("probe failed");
    const data = await resp.json();
    if (data.is_audio) {
      applyAudioMode(data);
    } else {
      applyProbeResult(data);
    }
  } catch {
    setResState("error");
  }
};

const handleResClick = (e) => {
  const btn = e.target.closest(".res-btn");
  if (!btn || btn.hidden) return;
  selectResolution(btn.dataset.res);
};

// ── Task card rendering ───────────────────────────────────────────────────────

const renderTask = (task, container) => {
  let card = container.querySelector(`[data-id="${task.id}"]`);

  if (!card) {
    const tpl = els.template().content.cloneNode(true);
    card = tpl.querySelector(".task-card");
    card.dataset.id = task.id;
    container.appendChild(card);
  }

  card.querySelector(".task-channel").textContent = task.channel || "…";

  const chip = card.querySelector(".folder-chip");
  chip.textContent = task.folder || "";
  chip.hidden = !task.folder;

  // Resolution badge
  const resChip = card.querySelector(".task-res");
  if (task.resolution_override) {
    resChip.textContent = task.resolution_override;
    resChip.hidden = false;
  } else {
    resChip.hidden = true;
  }

  // Jellyfin badge — show the library's name (e.g. "YT"), not its type.
  // Falls back to the type only for older tasks that predate name storage.
  const jfBadge = card.querySelector(".task-jf-badge");
  const jfName = jfBadge?.querySelector(".task-jf-name");
  if (task.jellyfin_library_id && jfName) {
    jfName.textContent =
      task.jellyfin_library_name || task.jellyfin_library_type || "Jellyfin";
    jfBadge.hidden = false;
  } else if (jfBadge) {
    jfBadge.hidden = true;
  }

  card.querySelector(".task-title").textContent = task.title || task.url;
  card.querySelector(".task-url").textContent = truncateUrl(task.url);

  const pct = task.progress || 0;
  card.querySelector(".progress-bar-fill").style.width = `${pct}%`;
  card.querySelector(".progress-pct").textContent = `${Math.round(pct)}%`;

  const badge = card.querySelector(".task-status-badge");
  badge.textContent = task.status;
  badge.className = `task-status-badge badge-${task.status}`;

  // Speed + ETA (speed is already a formatted string from the server)
  const speedEl = card.querySelector(".task-speed");
  if (task.status === "downloading") {
    const parts = [];
    if (task.speed && task.speed !== "—") parts.push(task.speed);
    if (task.eta && task.eta !== "—") parts.push(`ETA ${task.eta}`);
    speedEl.textContent = parts.join("  ·  ");
  } else {
    speedEl.textContent = "";
  }

  // Playlist counter
  const counter = card.querySelector(".playlist-counter");
  if (task.playlist_index && task.playlist_count) {
    counter.textContent = `${task.playlist_index} / ${task.playlist_count}`;
    counter.hidden = false;
  } else {
    counter.hidden = true;
  }

  const statusText = card.querySelector(".task-status-text");
  if (task.status === "failed") {
    statusText.textContent = task.status_text || "Download failed";
  } else if (task.status === "completed" && task.browser_ready) {
    statusText.textContent = autoDownloaded.has(task.id)
      ? "Sent to your device"
      : "Sending to your device…";
  } else if (task.status === "completed") {
    statusText.textContent = task.final_path || task.status_text || "Done";
  } else {
    statusText.textContent = task.status_text || "";
  }

  ALL_STATUS_CLASSES.forEach((cls) => card.classList.remove(cls));
  card.classList.add(task.status);

  // Show retry button for failed or cancelled tasks
  const retryBtn = card.querySelector(".btn-retry");
  if (retryBtn) {
    retryBtn.hidden = !["failed", "cancelled"].includes(task.status);
  }

  // Show cancel button only while a download is actively in progress
  const cancelBtn = card.querySelector(".btn-cancel");
  if (cancelBtn) {
    const cancellable = [
      "downloading",
      "probing",
      "analyzing",
      "pending",
    ].includes(task.status);
    cancelBtn.hidden = !cancellable;
  }

  const fill = card.querySelector(".progress-bar-fill");
  if (["probing", "analyzing", "pending"].includes(task.status)) {
    fill.classList.add("indeterminate");
  } else {
    fill.classList.remove("indeterminate");
  }

  return card;
};

// ── Task routing ──────────────────────────────────────────────────────────────

// A download with no Jellyfin destination is staged in the container instead
// of written to server disk — as soon as it's ready, hand it straight to the
// browser's own download flow rather than making the user click a button.
const autoSaveToDevice = (task) => {
  if (autoDownloaded.has(task.id)) return;
  autoDownloaded.add(task.id);
  const a = document.createElement("a");
  a.href = `/api/tasks/${task.id}/file`;
  a.download = task.download_filename || "";
  document.body.appendChild(a);
  a.click();
  a.remove();
};

const updateTask = (task) => {
  tasks.set(task.id, task);

  if (task.status === "completed" && task.browser_ready) {
    autoSaveToDevice(task);
  }

  const activeList = els.activeList();
  const completedList = els.completedList();

  // Monitor-triggered downloads (including ad-hoc downloads that matched an
  // existing monitor) get their progress shown on that monitor's card on the
  // Monitor tab instead — keep them off the Downloads tab's lists entirely,
  // active or completed.
  if (task.monitor_id) {
    activeList.querySelector(`[data-id="${task.id}"]`)?.remove();
    completedList.querySelector(`[data-id="${task.id}"]`)?.remove();
    updateCounts();
    return;
  }

  const isCompleted = COMPLETED_STATUSES.includes(task.status);

  if (isCompleted) {
    activeList.querySelector(`[data-id="${task.id}"]`)?.remove();
    renderTask(task, completedList);
  } else {
    completedList.querySelector(`[data-id="${task.id}"]`)?.remove();
    renderTask(task, activeList);
  }
  updateCounts();
};

const removeTask = (taskId) => {
  tasks.delete(taskId);
  document
    .querySelectorAll(`[data-id="${taskId}"]`)
    .forEach((el) => el.remove());
  updateCounts();
};

// ── WebSocket ──────────────────────────────────────────────────────────────────────

const connectWs = () => {
  clearTimeout(wsReconnectTimer);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.addEventListener("close", () => {
    wsReconnectTimer = setTimeout(connectWs, 3000);
  });
  ws.addEventListener("error", () => {
    ws.close();
  });

  ws.addEventListener("message", ({ data }) => {
    let msg;
    try {
      msg = JSON.parse(data);
    } catch {
      return;
    }

    switch (msg.type) {
      case "init":
        tasks.clear();
        monitorTasks.clear();
        els
          .activeList()
          .querySelectorAll(".task-card")
          .forEach((el) => el.remove());
        els
          .completedList()
          .querySelectorAll(".task-card")
          .forEach((el) => el.remove());
        (msg.tasks || []).forEach((t) => {
          updateTask(t);
          if (t.monitor_id && ACTIVE_STATUSES.includes(t.status)) {
            monitorTasks.set(t.monitor_id, t);
          }
        });
        renderMonitors();
        break;
      case "task_added":
      case "task_update":
        updateTask(msg.task);
        if (msg.task.monitor_id) {
          if (ACTIVE_STATUSES.includes(msg.task.status)) {
            monitorTasks.set(msg.task.monitor_id, msg.task);
          } else {
            monitorTasks.delete(msg.task.monitor_id);
          }
          renderMonitors();
        }
        break;
      case "task_removed":
        removeTask(msg.task_id);
        break;
      case "monitors_update":
        monitors = msg.monitors || [];
        renderMonitors();
        break;
    }
  });
};

// ── Health check ──────────────────────────────────────────────────────────────

const renderDiskWarning = (data) => {
  const el = els.diskWarning();
  if (!el) return;
  if (data && data.disk_low) {
    const free = Math.max(
      0,
      Math.round(((data.disk_free_mb || 0) / 1024) * 10) / 10,
    );
    const min = Math.round(((data.disk_min_mb || 0) / 1024) * 10) / 10;
    el.textContent =
      `Low disk space — only ${free} GB free on the download cache drive ` +
      `(minimum ${min} GB). New downloads will be refused until space is freed.`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
};

const fetchHealth = async () => {
  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    renderDiskWarning(data);
  } catch {
    // Health endpoint unreachable — disk warning simply stays hidden.
  }
};

// ── Form submission ───────────────────────────────────────────────────────────

const handleFormSubmit = async (e) => {
  e.preventDefault();

  const urlVal = els.urlInput().value.trim();
  if (!urlVal) {
    showToast("Please enter a URL", "error");
    return;
  }
  if (!selectedRes) {
    showToast("Please select a quality", "error");
    return;
  }

  const btn = els.submitBtn();
  btn.disabled = true;

  try {
    const resp = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: urlVal,
        resolution_override: selectedRes,
        jellyfin_library_id: selectedJfLibrary?.id ?? null,
        jellyfin_library_name: selectedJfLibrary?.name ?? null,
        jellyfin_library_path: selectedJfLibrary?.path ?? null,
        jellyfin_library_type: selectedJfLibrary?.type ?? null,
        folder_override: selectedFolderOverride || null,
        include_playlist_index: els.playlistIndexCheckbox().checked,
      }),
    });

    if (resp.ok) {
      els.urlInput().value = "";
      clearResSelection();
      clearJfSelection();
      setJellyfinOpen(false);
      hideProbeInfo();
      setResState("idle");
      showToast("Queued!", "success");
    } else {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || `Error ${resp.status}`, "error");
      btn.disabled = false;
    }
  } catch {
    showToast("Network error — could not reach server", "error");
    btn.disabled = false;
  }
};

// ── URL input → probe ─────────────────────────────────────────────────────────

const handleUrlInput = () => {
  const url = els.urlInput().value.trim();
  clearResSelection();

  if (!url) {
    hideProbeInfo();
    setResState("idle");
    return;
  }

  // Debounce — wait 700 ms of idle before probing
  clearTimeout(probeTimer);
  probeTimer = setTimeout(() => probeUrl(url), 700);
};

// ── Cancel task (event delegation) ───────────────────────────────────────────

const handleCancelClick = async (e) => {
  const btn = e.target.closest(".btn-cancel");
  if (!btn) return;
  const taskId = btn.closest(".task-card")?.dataset.id;
  if (!taskId) return;
  btn.disabled = true;
  try {
    await fetch(`/api/tasks/${taskId}/cancel`, { method: "POST" });
  } catch {
    showToast("Failed to cancel task", "error");
    btn.disabled = false;
  }
};

// ── Retry task (event delegation) ────────────────────────────────────────────

const handleRetryClick = async (e) => {
  const btn = e.target.closest(".btn-retry");
  if (!btn) return;
  const taskId = btn.closest(".task-card")?.dataset.id;
  if (!taskId) return;
  const task = tasks.get(taskId);
  if (!task) return;

  btn.disabled = true;
  try {
    const resp = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: task.url,
        resolution_override: task.resolution_override,
        jellyfin_library_id: task.jellyfin_library_id ?? null,
        jellyfin_library_name: task.jellyfin_library_name ?? null,
        jellyfin_library_path: task.jellyfin_library_path ?? null,
        jellyfin_library_type: task.jellyfin_library_type ?? null,
        folder_override: task.folder_override ?? null,
        include_playlist_index: task.include_playlist_index ?? true,
      }),
    });
    if (resp.ok) {
      // Remove the old failed/cancelled entry to avoid clutter
      await fetch(`/api/tasks/${taskId}`, { method: "DELETE" });
      removeTask(taskId);
      showToast("Re-queued!", "success");
    } else {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || `Error ${resp.status}`, "error");
      btn.disabled = false;
    }
  } catch {
    showToast("Network error — could not reach server", "error");
    btn.disabled = false;
  }
};

// ── Remove task (event delegation) ────────────────────────────────────────────

const handleRemoveClick = async (e) => {
  const btn = e.target.closest(".btn-remove");
  if (!btn) return;
  const taskId = btn.closest(".task-card")?.dataset.id;
  if (!taskId) return;
  try {
    await fetch(`/api/tasks/${taskId}`, { method: "DELETE" });
    removeTask(taskId);
  } catch {
    showToast("Failed to remove task", "error");
  }
};

// ── Clear completed ───────────────────────────────────────────────────────────

const handleClearCompleted = async () => {
  const toDelete = [...tasks.values()].filter(
    (t) => !t.monitor_id && COMPLETED_STATUSES.includes(t.status),
  );
  await Promise.allSettled(
    toDelete.map((t) => fetch(`/api/tasks/${t.id}`, { method: "DELETE" })),
  );
  toDelete.forEach((t) => removeTask(t.id));
};

// ── Tab navigation ───────────────────────────────────────────────────────────

const switchTab = (tab) => {
  currentTab = tab;
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  document.querySelectorAll(".tab-pane").forEach((pane) => {
    pane.hidden = pane.id !== `tab-${tab}`;
  });
};

// ── Monitor tab ───────────────────────────────────────────────────────────────

const scheduleLabel = (m) => {
  const s = m.schedule;
  if (s === "daily") {
    const [h, min] = (m.schedule_time || "03:00").split(":").map(Number);
    const d = new Date();
    d.setHours(h, min, 0, 0);
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return `Daily at ${t}`;
  }
  if (s === "hourly") return "Every hour";
  if (s === "6h") return "Every 6 hours";
  if (s === "12h") return "Every 12 hours";
  if (s === "weekly") {
    const [h, min] = (m.schedule_time || "03:00").split(":").map(Number);
    const d = new Date();
    d.setHours(h, min, 0, 0);
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const dayName = m.schedule_day != null ? DAY_NAMES[m.schedule_day] : null;
    return dayName ? `Weekly on ${dayName} at ${t}` : "Weekly";
  }
  return s;
};

const fmtDatetime = (iso) => {
  if (!iso) return "Never";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
};

const renderMonitors = () => {
  const list = document.getElementById("monitor-list");
  const empty = document.getElementById("monitor-empty");
  const countEl = document.getElementById("monitor-count");
  if (!list) return;
  if (countEl) countEl.textContent = monitors.length;
  if (empty) empty.hidden = monitors.length > 0;

  list.querySelectorAll(".monitor-card").forEach((card) => {
    if (!monitors.find((m) => m.id === card.dataset.id)) card.remove();
  });

  monitors.forEach((m) => {
    let card = list.querySelector(`.monitor-card[data-id="${m.id}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "monitor-card";
      card.dataset.id = m.id;
      list.appendChild(card);
    }
    const activeTask = monitorTasks.get(m.id);
    const isDownloading = !!activeTask;
    const statusClass = isDownloading
      ? "badge-downloading"
      : m.status === "up-to-date"
        ? "badge-completed"
        : m.status === "checking"
          ? "badge-analyzing"
          : "badge-pending";
    const statusLabel = isDownloading
      ? "downloading"
      : m.status === "up-to-date"
        ? "up to date"
        : m.status === "checking"
          ? "checking…"
          : m.status || "idle";
    const jfBadge = m.jellyfin_library_id
      ? `<span class="monitor-jf-badge"><img src="/static/img/jellyfin.svg" class="jellyfin-icon jellyfin-icon--sm" alt=""/> ${m.jellyfin_library_name || m.jellyfin_library_type || "Jellyfin"}</span>`
      : "";
    const numberingBadge = `<span class="monitor-numbering">${m.include_playlist_index ? "Numbering on" : "Numbering off"}</span>`;
    const archiveInfo =
      m.archive_count != null || m.playlist_count != null
        ? `<span class="monitor-archive">${m.archive_count ?? 0} / ${m.playlist_count ?? "?"} downloaded</span>`
        : "";
    const newBadge = m.last_new_videos
      ? `<span class="monitor-new">+${m.last_new_videos} new</span>`
      : "";
    // Progress section — shown while a monitor-triggered download is running
    const progressSection = activeTask
      ? (() => {
          const pct = activeTask.progress || 0;
          const pi = activeTask.playlist_index;
          const pc = activeTask.playlist_count;
          const counter = pi && pc ? `${pi} / ${pc} · ` : "";
          const text = activeTask.status_text || activeTask.status || "";
          return `
        <div class="monitor-dl-progress">
          <div class="monitor-dl-bar-wrap">
            <div class="monitor-dl-bar-fill ${pct < 1 ? "indeterminate" : ""}" style="width:${pct}%"></div>
          </div>
          <span class="monitor-dl-text">${counter}${text}</span>
        </div>`;
        })()
      : "";

    card.classList.toggle("editing", editingMonitorId === m.id);

    card.innerHTML = `
      <div class="monitor-header">
        <div class="monitor-meta">
          <span class="monitor-name">${m.name || m.url}</span>
          ${jfBadge}
        </div>
        <div class="monitor-actions">
          <span class="task-status-badge ${statusClass}">${statusLabel}</span>
          <button class="btn-monitor-run" data-id="${m.id}" title="Check now">Check now</button>
          <button class="btn-monitor-edit" data-id="${m.id}" title="Edit">Edit</button>
          <button class="btn-remove" data-id="${m.id}" title="Remove">Remove</button>
        </div>
      </div>
      <div class="monitor-url">${m.url}</div>
      ${progressSection}
      <div class="monitor-footer">
        <span class="monitor-schedule">${scheduleLabel(m)}</span>
        <span class="monitor-res">${m.resolution_override || "1080p"}</span>
        ${numberingBadge}
        ${archiveInfo}
        ${newBadge}
        <span class="monitor-last">Last checked: ${fmtDatetime(m.last_checked)}</span>
        <span class="monitor-next">Next: ${fmtDatetime(m.next_run)}</span>
      </div>`;
  });
};

const renderMonitorJellyfinLibraries = () => {
  const grid = document.getElementById("monitor-jellyfin-grid");
  if (!grid) return;
  grid.innerHTML = "";
  jellyfinLibraries.forEach((lib) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className =
      "jellyfin-lib-btn" +
      (selectedMonitorJfLibrary?.id === lib.id ? " selected" : "");
    const displayPath = lib.jellyfin_path || lib.path;
    btn.innerHTML = `<span class="jf-lib-name">${lib.name}</span><span class="jf-lib-type">${lib.type_label}</span><span class="jf-lib-path" title="${lib.path}">${displayPath}</span>`;
    btn.addEventListener("click", () => {
      selectedMonitorJfLibrary = lib;
      renderMonitorJellyfinLibraries();
      const btnText = document.getElementById("monitor-jellyfin-btn-text");
      if (btnText) btnText.textContent = lib.name;
      const picker = document.getElementById("monitor-jellyfin-picker");
      if (picker) picker.hidden = true;
    });
    grid.appendChild(btn);
  });
};

const syncMonitorScheduleFieldsVisibility = () => {
  const val = monitorScheduleEl?.value;
  if (monitorTimeEl) monitorTimeEl.hidden = !(val === "daily" || val === "weekly");
  if (monitorDayEl) monitorDayEl.hidden = val !== "weekly";
};

// Load an existing monitor into the Add Monitor form for editing. The URL is
// locked (it identifies which playlist/archive this monitor owns) — every
// other field can be changed and is saved via PATCH instead of POST.
const startEditMonitor = (m) => {
  editingMonitorId = m.id;

  const urlInput = document.getElementById("monitor-url");
  if (urlInput) {
    urlInput.value = m.url || "";
    urlInput.disabled = true;
  }
  if (monitorScheduleEl) monitorScheduleEl.value = m.schedule || "daily";
  if (monitorTimeEl) monitorTimeEl.value = m.schedule_time || "03:00";
  if (monitorDayEl) {
    monitorDayEl.value = m.schedule_day != null ? String(m.schedule_day) : "0";
  }
  syncMonitorScheduleFieldsVisibility();

  const resSelect = document.getElementById("monitor-res");
  if (resSelect) resSelect.value = m.resolution_override || "1080p";
  if (els.monitorPlaylistIndexCheckbox()) {
    els.monitorPlaylistIndexCheckbox().checked = m.include_playlist_index !== false;
  }

  selectedMonitorJfLibrary = m.jellyfin_library_id
    ? {
        id: m.jellyfin_library_id,
        name: m.jellyfin_library_name,
        path: m.jellyfin_library_path,
        type: m.jellyfin_library_type,
      }
    : null;
  const btnText = document.getElementById("monitor-jellyfin-btn-text");
  if (btnText) {
    btnText.textContent = selectedMonitorJfLibrary
      ? selectedMonitorJfLibrary.name
      : "Save to Jellyfin";
  }
  renderMonitorJellyfinLibraries();

  const submitBtn = document.getElementById("monitor-submit-btn");
  if (submitBtn) submitBtn.textContent = "Save changes";
  const cancelBtn = document.getElementById("monitor-cancel-edit-btn");
  if (cancelBtn) cancelBtn.hidden = false;
  const formTitle = document.getElementById("monitor-form-title");
  if (formTitle) formTitle.textContent = "Edit Monitor";
  const formCard = document.getElementById("monitor-form-card");
  if (formCard) formCard.classList.add("editing");

  renderMonitors(); // highlight this monitor's card as the one being edited
  urlInput?.scrollIntoView({ behavior: "smooth", block: "center" });
};

const cancelEditMonitor = () => {
  editingMonitorId = null;

  const urlInput = document.getElementById("monitor-url");
  if (urlInput) {
    urlInput.value = "";
    urlInput.disabled = false;
  }
  if (monitorScheduleEl) monitorScheduleEl.value = "daily";
  syncMonitorScheduleFieldsVisibility();

  selectedMonitorJfLibrary = null;
  const btnText = document.getElementById("monitor-jellyfin-btn-text");
  if (btnText) btnText.textContent = "Save to Jellyfin";
  renderMonitorJellyfinLibraries();

  const submitBtn = document.getElementById("monitor-submit-btn");
  if (submitBtn) submitBtn.textContent = "+ Add Monitor";
  const cancelBtn = document.getElementById("monitor-cancel-edit-btn");
  if (cancelBtn) cancelBtn.hidden = true;
  const formTitle = document.getElementById("monitor-form-title");
  if (formTitle) formTitle.textContent = "Add Monitor";
  const formCard = document.getElementById("monitor-form-card");
  if (formCard) formCard.classList.remove("editing");

  renderMonitors(); // clear the editing highlight
};

const handleMonitorFormSubmit = async (e) => {
  e.preventDefault();
  const url = document.getElementById("monitor-url").value.trim();
  if (!url) return;
  if (!selectedMonitorJfLibrary?.id) {
    showToast("Select a Jellyfin library first — monitors need one", "error");
    return;
  }
  const btn = document.getElementById("monitor-submit-btn");
  const isEdit = !!editingMonitorId;
  btn.disabled = true;
  btn.textContent = isEdit ? "Saving…" : "Adding…";

  const schedule = monitorScheduleEl?.value || "daily";
  const payload = {
    url,
    schedule,
    schedule_time: monitorTimeEl?.value || "03:00",
    schedule_day: schedule === "weekly" ? Number(monitorDayEl?.value ?? 0) : null,
    resolution_override: document.getElementById("monitor-res").value,
    jellyfin_library_id: selectedMonitorJfLibrary?.id ?? null,
    jellyfin_library_name: selectedMonitorJfLibrary?.name ?? null,
    jellyfin_library_path: selectedMonitorJfLibrary?.path ?? null,
    jellyfin_library_type: selectedMonitorJfLibrary?.type ?? null,
    include_playlist_index: els.monitorPlaylistIndexCheckbox().checked,
  };

  try {
    const resp = isEdit
      ? await fetch(`/api/monitors/${editingMonitorId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        })
      : await fetch("/api/monitors", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
    if (resp.ok) {
      const m = await resp.json();
      monitors = [...monitors.filter((x) => x.id !== m.id), m];
      renderMonitors();
      showToast(isEdit ? "Monitor updated!" : "Monitor added!", "success");
      cancelEditMonitor();
    } else {
      const err = await resp.json().catch(() => ({}));
      showToast(err.detail || "Failed to save monitor", "error");
    }
  } catch {
    showToast("Network error", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = editingMonitorId ? "Save changes" : "+ Add Monitor";
  }
};

const handleMonitorListClick = async (e) => {
  const runBtn = e.target.closest(".btn-monitor-run");
  const editBtn = e.target.closest(".btn-monitor-edit");
  const removeBtn = e.target.closest(".btn-remove");
  const id = runBtn?.dataset.id || editBtn?.dataset.id || removeBtn?.dataset.id;
  if (!id) return;

  if (editBtn) {
    const m = monitors.find((x) => x.id === id);
    if (m) startEditMonitor(m);
    return;
  }
  if (runBtn) {
    runBtn.disabled = true;
    try {
      await fetch(`/api/monitors/${id}/run`, { method: "POST" });
      showToast("Check queued!", "success");
    } catch {
      showToast("Failed to queue", "error");
    } finally {
      runBtn.disabled = false;
    }
  }
  if (removeBtn) {
    try {
      await fetch(`/api/monitors/${id}`, { method: "DELETE" });
      monitors = monitors.filter((m) => m.id !== id);
      renderMonitors();
      if (editingMonitorId === id) cancelEditMonitor();
    } catch {
      showToast("Failed to remove", "error");
    }
  }
};

const fetchMonitors = async () => {
  try {
    const resp = await fetch("/api/monitors");
    monitors = await resp.json();
    renderMonitors();
  } catch {
    // silently fail
  }
};

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  connectWs();
  fetchHealth();
  // Re-check disk space / health periodically so the warning appears and
  // clears without needing a page reload.
  setInterval(fetchHealth, 30000);
  setResState("idle");

  els.form().addEventListener("submit", handleFormSubmit);
  els.urlInput().addEventListener("input", handleUrlInput);
  els.resButtons().addEventListener("click", handleResClick);
  els.activeList().addEventListener("click", handleCancelClick);
  els.activeList().addEventListener("click", handleRetryClick);
  els.activeList().addEventListener("click", handleRemoveClick);
  els.completedList().addEventListener("click", handleRetryClick);
  els.completedList().addEventListener("click", handleRemoveClick);
  els.clearBtn().addEventListener("click", handleClearCompleted);
  els.jellyfinToggle().addEventListener("click", handleJellyfinToggle);

  // Tab navigation
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  // Monitor form
  const monitorForm = document.getElementById("monitor-form");
  if (monitorForm)
    monitorForm.addEventListener("submit", handleMonitorFormSubmit);
  // Populate time dropdown with locale-formatted hours
  monitorTimeEl = document.getElementById("monitor-time");
  if (monitorTimeEl) {
    for (let h = 0; h < 24; h++) {
      const d = new Date();
      d.setHours(h, 0, 0, 0);
      const label = d.toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      });
      const value = String(h).padStart(2, "0") + ":00";
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      if (h === 3) opt.selected = true;
      monitorTimeEl.appendChild(opt);
    }
  }

  monitorDayEl = document.getElementById("monitor-day");
  if (monitorDayEl) {
    // Default to today's weekday (JS getDay(): Sun=0…Sat=6 → our Mon=0…Sun=6)
    monitorDayEl.value = String((new Date().getDay() + 6) % 7);
  }

  monitorScheduleEl = document.getElementById("monitor-schedule");
  if (monitorScheduleEl) {
    monitorScheduleEl.addEventListener("change", syncMonitorScheduleFieldsVisibility);
    syncMonitorScheduleFieldsVisibility(); // set initial state
  }
  const monitorCancelEditBtn = document.getElementById("monitor-cancel-edit-btn");
  if (monitorCancelEditBtn) {
    monitorCancelEditBtn.addEventListener("click", cancelEditMonitor);
  }
  const monitorJfToggle = document.getElementById("monitor-jellyfin-toggle");
  if (monitorJfToggle) {
    monitorJfToggle.addEventListener("click", () => {
      const picker = document.getElementById("monitor-jellyfin-picker");
      if (!picker) return;
      picker.hidden = !picker.hidden;
      if (!picker.hidden) {
        if (jellyfinLibraries.length === 0) fetchJellyfinLibraries();
        renderMonitorJellyfinLibraries();
      }
    });
  }
  const monitorListEl = document.getElementById("monitor-list");
  if (monitorListEl)
    monitorListEl.addEventListener("click", handleMonitorListClick);
  fetchMonitors();

  els.folderBrowserBack().addEventListener("click", () => {
    const parent = folderBrowserCurrent.split("/").slice(0, -1).join("/");
    renderFolderBrowser(parent || folderBrowserBase);
  });
  els.folderBrowserClear().addEventListener("click", () => {
    selectedFolderOverride = null;
    if (selectedJfLibrary)
      els.jellyfinBtnText().textContent = selectedJfLibrary.name;
    renderFolderBrowser(folderBrowserBase);
  });
  els.folderBrowserDefault().addEventListener("click", handleSetDefaultMusicFolder);
  initJellyfin();
  fetchDefaultMusicFolder();

  updateCounts();
});
