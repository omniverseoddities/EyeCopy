"use strict";

(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const bridge = window.bridge;
  const thumbnailRequests = new Map();
  const videoMetadataRequests = new Map();
  const videoMetadataAttempted = new Set();
  const metadataScanNeeded = new Set();
  const themeNames = new Set(["ember", "ocean", "violet", "forest"]);
  let appSettings = { stayInBackground: true, theme: "ember", updateRepository: "", autoCheckUpdates: true }, settingsLoaded = false, settingsBusy = false, settingsLoadPromise = null, settingsError = "";
  let updateStatus = { status: "unconfigured", message: "Set a GitHub repository to enable updates." }, updateActionBusy = false;
  const state = { devices: [], selectedDevice: null, connection: {}, catalogue: [], apps: [], jobs: [], downloadDir: "", usb: {}, scanning: false };
  const ui = { view: "photos", filter: "all", sort: "newest", showHidden: false, queueSort: "newest", hideCompleted: false, queueBusy: false, downloadBusy: false, activationBusy: false, enqueueGeneration: 0, layout: "grid", search: "", appId: "", path: "/Documents", routes: new Map(), history: new Map(), undoStack: [], redoStack: [], historyBusy: false, historySuppressed: false, selected: new Set(), anchor: null, scroll: new Map(), catalogues: new Map(), thumbnails: new Map(), thumbAttempted: new Set(), thumbPending: new Set(), thumbQueue: [], thumbActive: 0, thumbObserver: null, hydrated: new Set(), hydrating: new Map(), cacheGeneration: new Map(), autoLoaded: new Set(), photoPreviewAttempts: new Set(), jobBusy: new Set(), clearingFinished: false, visible: [], fileSignature: "", deviceSignature: "", appsSignature: "", previewKey: "", preview: null, previewPending: false, previewToken: 0, uploadBusy: false, uploadTargets: new Map(), operationBusy: false, navigationGeneration: 0, dragDepth: 0, renderTimer: null };
  try { ui.routes = new Map(JSON.parse(localStorage.getItem("eyecopy.directories") || "[]")); ui.scroll = new Map(JSON.parse(localStorage.getItem("eyecopy.scroll") || "[]")); } catch { /* An invalid saved UI preference cannot prevent launch. */ }
  const viewCopy = {
    photos: ["Photos App", ""],
    apps: ["Files App", ""],
    transfers: ["Transfer queue", ""],
    connection: ["Connection", ""]
  };
  const icon = (name, className = "") => `<svg${className ? ` class="${className}"` : ""} aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const escape = (value) => String(value == null ? "" : value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const text = (selector, value) => { const node = $(selector); if (node && node.textContent !== String(value)) node.textContent = value; };
  const hidden = (selector, value) => $(selector)?.classList.toggle("hidden", value);
  const number = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
  const deviceId = () => typeof state.selectedDevice === "object" ? state.selectedDevice?.id : state.selectedDevice;
  const device = () => state.devices.find((item) => item.id === deviceId()) || null;
  const online = () => Boolean(device()?.online) && !["offline", "locked", "trust_required", "untrusted", "missing_service", "error"].includes(state.connection?.status);
  const nameOf = (item) => item?.alias || item?.name || "Device";
  const keyOf = (item) => String(item.id || `${item.deviceId || deviceId()}|${item.source}|${item.appId || ""}|${item.path}|${item.name}`);
  const thumbnailKey = (item) => `${item.deviceId || deviceId()}|${keyOf(item)}|${item.modified}|${item.size}`;
  const thumbnailOf = (item) => item.thumbnailUrl || ui.thumbnails.get(thumbnailKey(item));
  const isLivePhoto = (item) => item?.kind === "photo" && item.livePhoto?.verified === true && item.livePhoto.video?.kind === "video";
  const durationOf = item => { const value = item?.durationSeconds; return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null; };
  const formatDuration = value => {
    const seconds = number(value); if (seconds === null || seconds < 0) return "Unavailable";
    const total = Math.floor(seconds), hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60, tail = String(total % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${tail}` : `${Math.floor(total / 60)}:${tail}`;
  };
  const durationBadge = (item, inspector = false) => item.kind === "video" ? `<span class="${inspector ? "inspector-duration" : "file-duration"}${durationOf(item) === null ? " hidden" : ""}" title="Original video duration">${icon("clock")}<span data-duration-text>${durationOf(item) === null ? "" : formatDuration(item.durationSeconds)}</span></span>` : "";
  const itemSize = (item) => isLivePhoto(item) ? number(item.livePhoto.totalSize) ?? ((number(item.size) || 0) + (number(item.livePhoto.video.size) || 0)) : number(item.size) || 0;
  const assetDownloaded = (item) => Boolean(item.localPath && (!isLivePhoto(item) || item.livePhoto.video.localPath));
  const groupedAssets = (items) => { const paired = new Set(items.filter(isLivePhoto).map((item) => keyOf(item.livePhoto.video))); return items.filter((item) => !paired.has(keyOf(item))); };
  const catalogueKey = () => `${deviceId() || ""}|${ui.view === "apps" ? "app" : "photos"}|${ui.view === "apps" ? ui.appId : ""}|${ui.view === "apps" ? ui.path : ""}`;
  const previewable = item => item.previewSupported !== false && (item.kind === "video" || item.kind === "photo" && !/\.(aae|dng)$/i.test(item.name || ""));
  const libraryItems = () => currentCatalogue().filter(item => ui.view !== "photos" || ui.showHidden || previewable(item));
  const pendingDownloads = items => items.filter(item => item.kind !== "folder" && !assetDownloaded(item));
  const activeJobs = () => state.jobs.filter((job) => ["queued", "transferring", "paused", "waiting", "waiting_for_approval"].includes(job.state));
  const formatBytes = (value, precision = 1) => {
    const bytes = number(value);
    if (bytes === null) return "—";
    if (bytes === 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.max(Math.floor(Math.log(Math.abs(bytes)) / Math.log(1000)), 0), units.length - 1);
    return `${(bytes / 1000 ** index).toLocaleString(undefined, { maximumFractionDigits: index ? precision : 0 })} ${units[index]}`;
  };
  const formatDate = (value, detailed = false) => {
    if (!value) return "Date unavailable";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Date unavailable";
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric", ...(detailed ? { hour: "numeric", minute: "2-digit" } : {}) });
  };
  const formatEta = (value) => {
    const seconds = number(value);
    if (seconds === null || seconds < 0) return "Estimating…";
    if (seconds < 60) return `${Math.max(1, Math.ceil(seconds))} sec remaining`;
    if (seconds < 3600) return `${Math.ceil(seconds / 60)} min remaining`;
    return `${Math.floor(seconds / 3600)} hr ${Math.ceil((seconds % 3600) / 60)} min remaining`;
  };
  const extension = (item) => item.kind === "folder" ? "FOLDER" : (item.name?.split(".").pop() || "FILE").slice(0, 10).toUpperCase();
  const typeIcon = (kind) => ({ photo: "photo", video: "video", folder: "folder" }[kind] || "file");
  const detailRows = (rows) => `<dl class="detail-list">${rows.map(([label, value, durationRow]) => `<div class="detail-row"${durationRow ? ' data-video-duration="true"' : ""}><dt>${escape(label)}</dt><dd>${escape(value)}</dd></div>`).join("")}</dl>`;

  function applyVideoMetadata(result) {
    const seconds = durationOf(result); if (!result?.itemId || seconds === null) return;
    const signature = result.sourceSignature;
    const matches = item => signature && number(signature.size) !== null && number(item.size) === number(signature.size) && String(item.modified ?? "") === String(signature.modified ?? "");
    let changed = false;
    const update = item => {
      if (keyOf(item) === String(result.itemId) && matches(item)) { item.durationSeconds = seconds; changed = true; }
      if (isLivePhoto(item) && keyOf(item.livePhoto.video) === String(result.itemId) && matches(item.livePhoto.video)) { item.livePhoto.video.durationSeconds = seconds; changed = true; }
    };
    state.catalogue.forEach(update); for (const catalogue of ui.catalogues.values()) catalogue.forEach(update);
    if (changed) scheduleCatalogue();
  }

  function ensureVideoMetadata(item) {
    const component = isLivePhoto(item) ? item.livePhoto.video : item.kind === "video" ? item : null;
    if (!component || durationOf(component) !== null) return;
    const key = thumbnailKey(component); if (videoMetadataAttempted.has(key) || videoMetadataRequests.has(key)) return;
    videoMetadataAttempted.add(key);
    const metadataDevice = component.deviceId || deviceId(), generation = ui.cacheGeneration.get(metadataDevice) || 0, sourceSignature = { size: component.size, modified: component.modified };
    const pending = request("video_metadata", { item: { id: component.id } }).then(result => {
      if ((ui.cacheGeneration.get(metadataDevice) || 0) === generation) applyVideoMetadata({ ...result, itemId: result?.itemId || component.id, sourceSignature: result?.sourceSignature || sourceSignature });
    }).catch(() => { /* Missing video metadata never starts a download or interrupts browsing. */ }).finally(() => videoMetadataRequests.delete(key));
    videoMetadataRequests.set(key, pending);
  }

  function updateInspectorDuration(item) {
    const component = isLivePhoto(item) ? item.livePhoto.video : item;
    const value = durationOf(component), badge = $("#inspector-content .inspector-duration"), row = $("#inspector-content [data-video-duration] dd");
    if (badge) { badge.classList.toggle("hidden", value === null); badge.querySelector("[data-duration-text]").textContent = value === null ? "" : formatDuration(value); }
    if (row) row.textContent = value === null ? "Unavailable" : formatDuration(value);
    const seek = $("#video-position");
    if (seek) { seek.max = String(Math.min(86400, value || 0)); seek.disabled = value === null || value <= 0; hidden("#numeric-seek", value !== null); updatePlayerPosition(); }
  }

  function toast(message, error = false, duration = 7000) {
    if (!message) return;
    const node = document.createElement("div");
    node.className = `toast${error ? " error" : ""}`;
    node.innerHTML = `${icon(error ? "info" : "check")}<span>${escape(message)}</span><button aria-label="Dismiss message">${icon("close")}</button>`;
    node.querySelector("button").addEventListener("click", () => node.remove());
    $("#toast-region").appendChild(node);
    while ($("#toast-region").children.length > 3) $("#toast-region").firstElementChild.remove();
    setTimeout(() => node.remove(), duration);
  }

  async function request(command, args = {}) {
    if (!bridge?.request) throw new Error("Open EyeCopy through its desktop launcher to connect to your device.");
    const result = await bridge.request(command, args);
    if (result?.error && !result?.devices && !result?.id && command !== "preview") throw new Error(typeof result.error === "string" ? result.error : result.error.message || "The request could not be completed.");
    if (result?.ok === false || result?.success === false) throw new Error(result.message || result.error || "The request could not be completed.");
    return result;
  }

  async function action(command, args = {}, callback) {
    try { const result = await request(command, args); if (callback) await callback(result); return result; }
    catch (error) { toast(error.message || String(error), true); return null; }
  }

  function renderSettings() {
    $("#settings-theme").value = appSettings.theme;
    $("#settings-background").checked = appSettings.stayInBackground;
    $("#settings-theme").disabled = settingsBusy;
    $("#settings-background").disabled = settingsBusy;
    if (document.activeElement !== $("#settings-repository")) $("#settings-repository").value = appSettings.updateRepository;
    $("#settings-repository").disabled = settingsBusy;
    $("#save-update-repository").disabled = settingsBusy;
    $("#settings-auto-updates").checked = appSettings.autoCheckUpdates;
    $("#settings-auto-updates").disabled = settingsBusy || !appSettings.updateRepository;
    renderUpdateStatus();
    $("#settings-dialog").setAttribute("aria-busy",String(settingsBusy));
    text("#settings-save-status", settingsBusy ? "Saving…" : settingsError || "Changes save automatically.");
    text("#settings-download-root", state.downloadRoot || state.rootPath || "Choose a default folder");
  }

  function renderUpdateStatus() {
    const running = ["checking", "downloading"].includes(updateStatus.status), downloading = updateStatus.status === "downloading";
    text("#update-status", updateStatus.message || "Check for published updates.");
    if (downloading && updateStatus.totalBytes) text("#update-status", `${updateStatus.message} ${formatBytes(updateStatus.downloadedBytes || 0)} / ${formatBytes(updateStatus.totalBytes)}`);
    $("#check-updates").disabled = settingsBusy || updateActionBusy || running || !appSettings.updateRepository;
    hidden("#install-update", !["available", "ready"].includes(updateStatus.status));
    $("#install-update").disabled = updateActionBusy || ui.downloadBusy || ui.uploadBusy || ui.activationBusy || ui.operationBusy || activeJobs().length > 0;
    text("#install-update", updateStatus.status === "ready" ? "Install update" : "Download and install");
    hidden("#cancel-update", !downloading); hidden("#update-progress", !downloading);
    $("#update-progress").value = updateStatus.totalBytes ? Math.min(100, 100 * (updateStatus.downloadedBytes || 0) / updateStatus.totalBytes) : 0;
  }

  async function checkUpdates() {
    try { const value = await request("check_updates"); if (value?.status) updateStatus = value; }
    catch (error) { updateStatus = { status: "error", message: error.message || "The update check could not finish." }; }
    renderUpdateStatus();
  }

  async function installUpdate() {
    if (updateActionBusy || activeJobs().length || ui.downloadBusy || ui.uploadBusy || ui.activationBusy || ui.operationBusy) { toast("Finish or stop transfers before installing an update.", true); return; }
    updateActionBusy = true; renderUpdateStatus();
    try { const value = await request("install_update"); if (value?.status) updateStatus = value; }
    catch (error) { toast(error.message || "The update could not be installed.", true); }
    finally { updateActionBusy = false; renderUpdateStatus(); }
  }

  function applyAppSettings(value) {
    if(!value || typeof value!=="object") return;
    appSettings = {
      stayInBackground: typeof value.stayInBackground==="boolean" ? value.stayInBackground : appSettings.stayInBackground,
      theme: themeNames.has(value.theme) ? value.theme : appSettings.theme,
      updateRepository: typeof value.updateRepository === "string" ? value.updateRepository : appSettings.updateRepository,
      autoCheckUpdates: typeof value.autoCheckUpdates === "boolean" ? value.autoCheckUpdates : appSettings.autoCheckUpdates
    };
    document.documentElement.dataset.theme = appSettings.theme;
    try { localStorage.setItem("eyecopy.theme",appSettings.theme); } catch { /* The desktop settings file remains authoritative. */ }
    renderSettings();
  }

  async function loadAppSettings() {
    if(settingsLoadPromise) return settingsLoadPromise;
    settingsLoadPromise = request("get_app_settings").then(value=>{settingsLoaded=true;settingsError="";applyAppSettings(value);}).catch(error=>{settingsError="Settings could not be loaded. Open Settings to retry.";renderSettings();}).finally(()=>{settingsLoadPromise=null;});
    return settingsLoadPromise;
  }

  async function openSettings() {
    if(document.querySelector("dialog[open]")) return;
    renderSettings();$("#settings-dialog").showModal();
    if(!settingsLoaded) await loadAppSettings();
    try { const value = await request("get_update_status"); if (value?.status) updateStatus = value; } catch { /* Source fixtures can omit the desktop updater. */ }
    renderUpdateStatus();
  }

  async function saveAppSettings(patch) {
    if(settingsBusy){renderSettings();return;}
    const previous={...appSettings};settingsBusy=true;settingsError="";renderSettings();
    try { applyAppSettings(await request("set_app_settings",patch));settingsLoaded=true; }
    catch(error){settingsError="Could not save settings. Try again.";applyAppSettings(previous);toast(error.message||settingsError,true);}
    finally{settingsBusy=false;renderSettings();}
  }

  function saveNavigation() {
    try { localStorage.setItem("eyecopy.directories", JSON.stringify(Array.from(ui.routes))); localStorage.setItem("eyecopy.scroll", JSON.stringify(Array.from(ui.scroll).slice(-200))); } catch { /* Browsing also works without persistent UI preferences. */ }
  }

  function renderSaveWarning() {
    hidden("#state-save-banner", !state.stateSavePending && !state.cacheWarning);
    $("#retry-state-save").disabled = Boolean(ui.saveWarningBusy);
  }

  function mergeSaveWarning(result) {
    if (!result || !("stateSavePending" in result || "cacheWarning" in result)) return;
    state.stateSavePending = Boolean(result.stateSavePending || result.cacheWarning);
    state.cacheWarning = result.cacheWarning || null;
    renderSaveWarning();
  }
  function rememberScroll() { ui.scroll.set(catalogueKey(), $("#catalogue-scroll").scrollTop); saveNavigation(); }
  function rememberRoute() { if (deviceId()) ui.routes.set(deviceId(), { appId: ui.appId, path: ui.path }); saveNavigation(); }
  function restoreRoute(id) { const route = ui.routes.get(id); ui.appId = route?.appId || ""; ui.path = route?.path || "/Documents"; }
  function releasePreview() { if (ui.preview?.url && (ui.preview.streaming || ["original", "compatible"].includes(ui.preview.mode))) request("stop_stream", { url: ui.preview.url }).catch(() => {}); }
  function clearSelection() { releasePreview(); ui.selected.clear(); ui.anchor = null; ui.preview = null; ui.previewKey = ""; ui.previewPending = false; ui.previewStage = null; ui.previewToken++; renderSelection(); renderInspector(); }

  function historySnapshot() {
    return { deviceId: deviceId(), view: ui.view, appId: ui.appId, path: ui.path, search: ui.search, filter: ui.filter, selected: Array.from(ui.selected), anchor: ui.anchor, scroll: $("#catalogue-scroll").scrollTop };
  }

  function renderHistory() {
    $("#change-default-folder").disabled = ui.historyBusy || ui.operationBusy;
    for (const direction of ["undo", "redo"]) {
      const button = $(`#${direction}-action`), entry = ui[`${direction}Stack`].at(-1);
      button.disabled = !entry || ui.historyBusy || ui.operationBusy;
      button.title = `${direction === "undo" ? "Undo" : "Redo"}${entry ? ` ${entry.label}` : ""} (${direction === "undo" ? "Ctrl+Z" : "Ctrl+Y"})`;
      button.setAttribute("aria-label", button.title);
    }
  }

  function addHistory(entry) {
    if (ui.historySuppressed) return;
    if (entry.type === "view" && JSON.stringify(entry.before) === JSON.stringify(entry.after)) return;
    entry.at = Date.now(); ui.undoStack.push(entry); ui.undoStack = ui.undoStack.slice(-100); ui.redoStack = []; renderHistory();
  }

  function seedBackendHistory(snapshot, direction) {
    const stack = ui[`${direction}Stack`], timestamp = value => {
      if (!value) return Date.now();
      const parsed = Number(value); return Number.isFinite(parsed) ? parsed < 100000000000 ? parsed * 1000 : parsed : new Date(value).getTime() || Date.now();
    };
    const pc = snapshot.recycleHistory, location = snapshot.locationHistory;
    if (pc?.[direction === "undo" ? "canUndo" : "canRedo"] && pc[`${direction}Token`] && !stack.some(entry => entry.type === "pc-cleanup" && entry.token === pc[`${direction}Token`])) {
      stack.push({ type: "pc-cleanup", label: pc[`${direction}Label`] || "PC cleanup", token: pc[`${direction}Token`], at: timestamp(pc[`${direction}At`]) });
    }
    if (location?.[direction === "undo" ? "canUndo" : "canRedo"] && location[`${direction}Id`] && !stack.some(entry => entry.type === "destination" && entry.historyId === location[`${direction}Id`])) {
      stack.push({ type: "destination", label: location[`${direction}Label`] || "download destination", historyId: location[`${direction}Id`], at: timestamp(location[`${direction}At`]) });
    }
    stack.sort((a, b) => direction === "undo" ? a.at - b.at : b.at - a.at);
  }

  function recordViewChange(label, before) { addHistory({ type: "view", label, before, after: historySnapshot() }); }

  async function restoreHistoryView(snapshot) {
    if (snapshot.deviceId !== deviceId()) {
      if (!state.devices.some(item => item.id === snapshot.deviceId)) throw new Error("This saved device is no longer available.");
      const result = await request("select_device", { id: snapshot.deviceId });
      state.selectedDevice = snapshot.deviceId; state.apps = [];
      if (result?.devices) mergeState(result); else mergeState(await request("state"));
    }
    clearSelection(); ui.appId = snapshot.appId; ui.path = snapshot.path; ui.search = snapshot.search; ui.filter = snapshot.filter;
    setView(snapshot.view, false);
    ui.filter = snapshot.filter; ui.search = snapshot.search; $("#search-input").value = snapshot.search;
    $$(".filter-tab").forEach(button => button.classList.toggle("active", button.dataset.filter === ui.filter));
    ui.selected = new Set(snapshot.selected); ui.anchor = snapshot.anchor; ui.fileSignature = ""; ui.appsSignature = "";
    rememberRoute(); renderApps(); renderAppBreadcrumbs(); renderCatalogue(true); renderInspector();
    $("#catalogue-scroll").scrollTop = snapshot.scroll; renderDevices(); renderConnection();
    await hydrateCatalogue();
  }

  async function runHistory(direction) {
    const source = ui[`${direction}Stack`], destination = ui[`${direction === "undo" ? "redo" : "undo"}Stack`], entry = source.at(-1);
    if (!entry || ui.historyBusy || ui.operationBusy) return;
    ui.historyBusy = true; ui.historySuppressed = true; renderHistory();
    let backendHistory;
    try {
      if (entry.type === "view") await restoreHistoryView(entry[direction === "undo" ? "before" : "after"]);
      else if (entry.type === "destination") {
        const result = await request(direction === "undo" ? "undo_download_location" : "redo_download_location", { historyId: entry.historyId });
        backendHistory = result?.devices ? result : await request("state"); mergeState(backendHistory);
      } else if (entry.type === "pc-cleanup") {
        const result = await request(direction === "undo" ? "undo_pc_cleanup" : "redo_pc_cleanup", { token: entry.token });
        const count = direction === "undo" ? result.restored : result.deleted;
        mergeSaveWarning(result); backendHistory = await request("state"); mergeState(backendHistory);
        toast(`${count || 0} PC ${count === 1 ? "copy" : "copies"} ${direction === "undo" ? "restored from" : "moved to"} Recycle Bin.${result.failed ? ` ${result.failed} could not be changed.` : ""}`, Boolean(result.failed));
        if (result.complete === false || !count && result.failed) {
          toast(result.message || "Some PC copies could not be changed. Undo/Redo is still available to retry the remaining copies; existing files will not be overwritten.", true); return;
        }
        entry.token = direction === "undo" ? result.redoToken || result.token : result.undoToken || result.token;
      }
      source.pop(); if (entry.type !== "pc-cleanup" || entry.token) destination.push(entry);
      if (backendHistory) seedBackendHistory(backendHistory, direction);
    } catch (error) { toast(error.message || String(error), true); }
    finally { ui.historyBusy = false; ui.historySuppressed = false; renderHistory(); }
  }

  function previewProgressMarkup(item) {
    if (!["photo", "video"].includes(item.kind)) return "";
    return `<div id="preview-progress" class="preview-progress hidden" aria-live="polite"><div class="preview-progress-heading"><span id="preview-load-label"></span><button class="text-button" data-inspector-action="cancel-preview">Cancel</button></div><div id="preview-load-bar" class="preview-load-bar" role="progressbar" aria-label="Preview loading"><span></span></div><small id="preview-load-detail"></small></div>`;
  }

  function updatePreviewProgress(video = $("#inspector-content video")) {
    const host = $("#preview-progress"); if (!host) return;
    const stage = ui.previewStage;
    host.classList.toggle("hidden", !stage); if (!stage) return;
    const loading = ["loading", "buffering"].includes(stage.phase), bar = $("#preview-load-bar"), cancel = host.querySelector("[data-inspector-action]");
    let label = stage.label, detail = "", percent = null;
    if (video && ui.preview?.url && !ui.previewPending && currentMediaEvent(video)) {
      const duration = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
      let buffered = 0;
      for (let index = 0; index < video.buffered.length; index++) buffered += Math.max(0, video.buffered.end(index) - video.buffered.start(index));
      if (duration && buffered > 0) { percent = Math.min(100, buffered / duration * 100); detail = `${buffered.toFixed(1)} / ${duration.toFixed(1)} seconds buffered for playback`; }
      else if (buffered > 0) detail = `${buffered.toFixed(1)} seconds buffered for playback`;
      if (stage.phase === "ready") label = percent !== null ? `Playback buffer · ${Math.round(percent)}%` : stage.label || "Preview ready to play";
    }
    text("#preview-load-label", label || "Loading preview…"); text("#preview-load-detail", detail || (loading ? "Preview loading does not download the original." : ""));
    bar.classList.toggle("hidden", !loading && percent === null); bar.classList.toggle("indeterminate", loading && percent === null);
    bar.firstElementChild.style.width = percent === null ? "35%" : `${percent}%`;
    if (percent === null) bar.removeAttribute("aria-valuenow"); else bar.setAttribute("aria-valuenow", String(Math.round(percent)));
    bar.setAttribute("aria-valuetext", percent === null ? label || "Loading" : `${Math.round(percent)} percent of video duration buffered`);
    cancel.classList.toggle("hidden", !loading && !(video && ui.preview?.url));
  }

  function cancelPreview() {
    releasePreview(); ui.previewToken++; ui.playerSeek = null; ui.playerScrubbing = false; ui.previewPending = false; ui.preview = null; ui.previewStage = { phase: "cancelled", label: "Preview cancelled" }; renderInspector();
  }

  function targetVideo() { return $("#inspector-content video"); }

  function capturePlayerState() {
    const video=targetVideo(); if(!video)return;
    ui.playerVolume=video.volume;ui.playerMuted=video.muted;ui.playerRate=video.playbackRate;ui.playerPaused=video.paused;
  }

  function currentMediaEvent(video) {
    return video === targetVideo() && Boolean(ui.preview?.url) && !ui.previewPending
      && video.dataset.mediaToken === String(ui.previewToken)
      && (!video.currentSrc || video.currentSrc === ui.preview.url);
  }

  function playerPosition(video = targetVideo()) {
    const base = ui.preview?.mode === "compatible" ? number(ui.preview.start) || 0 : 0;
    return base + (number(video?.currentTime) || 0);
  }

  function playerTargetPosition(video = targetVideo()) {
    return ui.playerSeek && ui.playerSeek.phase !== "ready" ? ui.playerSeek.target : playerPosition(video);
  }

  function playerDuration(item = selectedItems()[0]) {
    const nativeDuration = targetVideo()?.duration;
    return ui.preview?.mode !== "compatible" && Number.isFinite(nativeDuration) && nativeDuration > 0 ? nativeDuration : durationOf(item);
  }

  function playerResumeIntent() {
    return ui.playerSeek && ui.playerSeek.phase !== "ready" ? ui.playerSeek.resume : targetVideo() ? !targetVideo().paused : true;
  }

  function setPlayerTimeline(value) {
    const seek = $("#video-position"), duration = playerDuration(); if (!seek) return;
    const target = Math.max(0, Math.min(duration ?? 86400, number(value) || 0));
    seek.max = String(Math.min(86400, duration ?? 0)); seek.disabled = duration === null || duration <= 0;
    seek.value = String(target); text("#video-position-label", `${formatDuration(target)} / ${formatDuration(duration)}`);
    const numeric = $("#stream-start"); if (numeric) numeric.value = String(target);
    hidden("#numeric-seek", duration !== null);
  }

  function updatePlayerPosition() {
    const video = targetVideo(); if (!video || ui.playerScrubbing) return;
    const pending = ui.playerSeek;
    if (pending && pending.phase !== "ready") { setPlayerTimeline(pending.target); return; }
    if (currentMediaEvent(video)) setPlayerTimeline(playerPosition(video));
  }

  function bindPlayerSource(video) {
    if (!video || !ui.preview?.url || ui.previewPending) return;
    const changed = video.dataset.mediaUrl !== ui.preview.url;
    video.dataset.mediaToken = String(ui.previewToken);
    video.dataset.mediaUrl = ui.preview.url;
    if (changed) { video.pause(); video.src = ui.preview.url; video.load(); }
    video.volume = ui.playerVolume ?? 1; video.muted = Boolean(ui.playerMuted);
    video.defaultPlaybackRate = ui.playerRate || 1; video.playbackRate = ui.playerRate || 1;
    updatePlayerPosition();
  }

  function finishPlayerSeek(video) {
    if (!currentMediaEvent(video) || video.readyState < 2) return;
    const pending = ui.playerSeek;
    if (pending && pending.token === ui.previewToken && pending.phase !== "ready") {
      if (pending.native && (video.seeking || Math.abs(video.currentTime - pending.target) > .5)) return;
      pending.phase = "ready"; ui.playerPaused = !pending.resume;
      if (pending.resume && !video.ended) video.play().catch(() => {}); else video.pause();
    }
    updatePlayerPosition();
  }

  async function seekPlayer(value) {
    const item = selectedItems()[0], video = targetVideo(); if (!item || item.kind !== "video" || !video) return;
    const target = Math.max(0, Math.min(playerDuration(item) ?? 86400, number(value) || 0));
    const resume = ui.playerScrubResume ?? playerResumeIntent();
    ui.playerScrubbing = false; ui.playerScrubResume = null;
    if (ui.preview?.mode === "compatible" || ui.previewPending) {
      await streamVideo(item, true, target, Boolean(ui.preview?.streaming), { resume, seek: true }); return;
    }
    ui.playerSeek = { token: ui.previewToken, target, resume, native: true, phase: "seeking" };
    setPlayerTimeline(target);
    try { video.currentTime = target; finishPlayerSeek(video); }
    catch { await streamVideo(item, true, target, Boolean(ui.preview?.streaming), { resume, seek: true }); }
  }

  function togglePlayerFullscreen() {
    const player = $("#inspector-content .video-player"); if (!player) return;
    if (document.fullscreenElement) document.exitFullscreen(); else player.requestFullscreen().catch(error => toast(error.message, true));
  }

  function playerControlsMarkup(item) {
    const duration = playerDuration(item), value = ui.playerSeek?.phase !== "ready" ? ui.playerSeek?.target ?? ui.preview?.start ?? 0 : playerPosition();
    return `<form id="stream-seek-form" class="stream-seek"><label for="video-position">Video position <span id="video-position-label">${formatDuration(value)} / ${formatDuration(duration)}</span></label><input id="video-position" type="range" min="0" max="${Math.min(86400, duration || 0)}" step="0.1" value="${value}" aria-label="Original video position"${duration ? "" : " disabled"}><div id="numeric-seek" class="${duration !== null ? "hidden" : ""}"><input id="stream-start" type="number" min="0" max="86400" value="${Math.floor(value)}" aria-label="Stream start time in seconds"><button class="button secondary small-button" type="submit">Go</button></div><div class="player-actions"><button class="text-button" type="button" data-inspector-action="fullscreen">${icon("external")}Fullscreen</button><span>Space to play · ← / → to seek</span></div></form>`;
  }

  function dragFilesFor(item) {
    return item && !ui.selected.has(keyOf(item)) ? [item] : selectedItems().filter((file) => file.kind !== "folder");
  }

  function prepareNativeDrag(items) {
    if (items.length && items.every((item) => item.kind !== "folder" && (assetDownloaded(item) || online()))) request("prepare_drag", { items }).catch(() => {});
  }

  function startNativeDrag(event, items) {
    event.preventDefault();
    if (!items.length || !items.every((item) => item.kind !== "folder" && (assetDownloaded(item) || online()))) return;
    if (bridge?.startDrag) bridge.startDrag({ items }); else action("drag_out", { items });
  }

  function setView(view, recordHistory = true) {
    if (!viewCopy[view]) return;
    const changed = ui.view !== view;
    const before = changed && recordHistory ? historySnapshot() : null;
    if (changed) { rememberScroll(); ui.navigationGeneration++; }
    ui.view = view;
    if (!["photos", "apps"].includes(view)) { ui.thumbObserver?.disconnect(); ui.thumbQueue = []; }
    $$(".nav-button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    text("#page-title", viewCopy[view][0]); text("#breadcrumb-title", viewCopy[view][0]); text("#page-subtitle", viewCopy[view][1]);
    hidden("#library-panel", !["photos", "apps"].includes(view)); hidden("#transfers-panel", view !== "transfers"); hidden("#connection-panel", view !== "connection");
    hidden("#app-controls", view !== "apps"); hidden("#other-filter", view !== "apps" && !ui.showHidden); hidden(".hidden-files-toggle", view !== "photos"); hidden("#upload-main", view === "connection");
    hidden("#app-directory-sidebar", view !== "apps");
    if (changed) { clearSelection(); ui.fileSignature = ""; ui.filter = "all"; $$(".filter-tab").forEach((button) => button.classList.toggle("active", button.dataset.filter === "all")); }
    if (view === "apps" && !state.apps.length && online()) loadApps();
    if (["photos", "apps"].includes(view)) { renderApps(); renderCatalogue(true); $("#catalogue-scroll").scrollTop = ui.scroll.get(catalogueKey()) || 0; hydrateCatalogue(); }
    renderConnection();
    if (before) recordViewChange("workspace navigation", before);
  }

  function renderDevices() {
    const signature = state.devices.map((item) => `${item.id}|${nameOf(item)}|${item.online}|${item.defaultDownloadDir}|${deviceId()}`).join(";");
    text("#device-count", state.devices.length);
    if (ui.deviceSignature === signature) return;
    ui.deviceSignature = signature;
    $("#device-list").innerHTML = state.devices.length ? state.devices.map((item) => `<div class="device-entry"><button class="device-button${item.id === deviceId() ? " active" : ""}" data-device="${escape(item.id)}" title="${escape(nameOf(item))}">${icon("phone")}<div><strong>${escape(nameOf(item))}</strong><small>${item.online ? "Connected via USB" : "Saved device · offline"}</small></div><span class="status-dot${item.online ? " online" : ""}"></span></button><button class="device-edit-button" data-device-edit="${escape(item.id)}" title="Rename ${escape(nameOf(item))} and its download folder" aria-label="Rename ${escape(nameOf(item))}">${icon("edit")}</button><button class="device-folder-button" data-device-folder="${escape(item.id)}" title="Open ${escape(nameOf(item))} downloads${item.defaultDownloadDir ? ` · ${escape(item.defaultDownloadDir)}` : ""}" aria-label="Open ${escape(nameOf(item))} downloads">${icon("folder")}</button><button class="device-cleanup-button" data-device-cleanup="${escape(item.id)}" title="Clear ${escape(nameOf(item))} PC downloads · Recycle Bin" aria-label="Clear PC downloads for ${escape(nameOf(item))}">${icon("trash")}</button></div>`).join("") : `<div class="no-devices">${icon("phone")}Connect your device to create its workspace.</div>`;
  }

  function usbLabel() {
    const usb = state.usb || {};
    if (usb.deviceId && usb.deviceId !== deviceId()) return "Speed unverified";
    const speed = usb.negotiatedSpeed || usb.NegotiatedSpeed;
    const rate = usb.signalingRate || usb.SignalingRate;
    const prefix = !online() && device() ? "Last link · " : "";
    if (rate) return `${prefix}${speed === "SuperSpeedPlus" || /10|20/.test(rate) ? "USB 3" : speed === "HighSpeed" ? "USB 2" : "USB"} · ${rate}`;
    if (usb.receiveBitsPerSecond) return `${prefix}${(usb.receiveBitsPerSecond / 1000000000).toLocaleString(undefined, { maximumFractionDigits: 1 })} Gbit/s`;
    if (speed && speed !== "Unknown") return prefix + String(speed).replace("SuperSpeedPlus", "USB 3 SuperSpeed+").replace("SuperSpeed", "USB 3 SuperSpeed").replace("HighSpeed", "USB 2 High Speed");
    return online() ? "Speed unverified" : "Not connected";
  }

  function renderConnection() {
    renderSaveWarning();
    renderHistory();
    const current = device();
    const connected = online();
    const connection = state.connection || {};
    const message = connection.message || (connected ? "Your device is connected and ready." : "Connect and unlock your device to continue.");
    const status = connected ? "Device connected" : current ? "Device offline" : connection.serviceAvailable === false ? "Apple service unavailable" : "Waiting for device";
    const pill = $("#connection-pill");
    pill.classList.toggle("online", connected); pill.classList.toggle("warning", !connected && (Boolean(current) || connection.serviceAvailable === false));
    text("#connection-pill > span:last-child", status);
    text("#device-name", current ? nameOf(current) : "No device selected");
    text("#device-detail", current ? `${current.productVersion ? `Version ${current.productVersion}` : "Saved device"}${connected ? " · Connected" : " · Offline"}` : "Connect your device with a USB cable");
    text("#usb-speed", usbLabel());
    const battery = number(current?.battery);
    text("#battery-value", battery === null ? "Unavailable" : `${Math.round(battery)}%${current?.charging ? " · Charging" : ""}`);
    const total = number(current?.storageTotal), free = number(current?.storageFree);
    text("#storage-value", free === null ? "Unavailable" : `${formatBytes(free)} free`);
    $("#storage-meter-fill").style.width = total && free !== null ? `${Math.min(100, Math.max(0, (total - free) / total * 100))}%` : "0%";
    hidden("#offline-banner", connected || (!current && connection.serviceAvailable !== false && !["locked", "untrusted", "trust_required", "error"].includes(connection.status)));
    text("#offline-title", connection.status === "locked" ? "Unlock your device to connect" : ["untrusted", "trust_required"].includes(connection.status) ? "Trust this computer on your device" : connection.serviceAvailable === false ? "Apple device service is unavailable" : "Device is offline");
    text("#offline-message", connection.message || "Your catalogue is still here. Reconnect to continue transferring.");
    text("#footer-status", status); $("#footer-status-dot").classList.toggle("online", connected);
    $("#refresh-library").disabled = !connected || state.scanning; $("#clear-cache-button").disabled = !current;
    $("#list-apps-button").disabled = !connected; $("#upload-main").disabled = !connected || ui.uploadBusy; $("#new-folder-button").disabled = !connected || ui.operationBusy; $("#files-home-button").disabled = !connected || ui.uploadBusy; $("#reset-destination").disabled = !state.devices.length;
    text("#connection-title", connected ? "Connected and ready" : current ? `${nameOf(current)} is offline` : "No device connected");
    text("#connection-message", message);
    hidden("#pair-device", !current || connected || connection.serviceAvailable === false);
    hidden("#open-apple-devices", connection.serviceAvailable !== false);
    text("#usb-diagnostic-title", usbLabel());
    text("#usb-diagnostic-message", !connected && state.usb?.capturedAt ? "This is the last recorded USB link for this device. Reconnect and refresh diagnostics to verify its current link." : state.usb?.message || (state.usb?.verified ? "Windows reported this physical USB link. File transfer throughput depends on the protocol, phone storage, and workload." : "The negotiated USB speed will appear here when Windows reports it. Transfer speed is measured separately in the queue."));
    const usb = state.usb || {};
    $("#usb-details").innerHTML = detailRows([["Link verified", usb.verified ? "Yes · Windows USB diagnostics" : "Not yet"], ["Negotiated speed", usb.negotiatedSpeed || "Unavailable"], ["Signaling rate", usb.signalingRate || "Unavailable"], ["Last checked", usb.capturedAt ? formatDate(usb.capturedAt, true) : "Not checked"]]);
    text("#diagnostic-device-name", current ? nameOf(current) : "No device selected");
    $("#device-details").innerHTML = detailRows([["Status", connected ? "Connected" : "Offline"], ["Software version", current?.productVersion || "Unavailable"], ["Device model", current?.productType || "Unavailable"], ["Battery", battery === null ? "Unavailable" : `${Math.round(battery)}%`], ["Storage capacity", formatBytes(total)], ["Available storage", formatBytes(free)]]);
    text("#service-status", connection.serviceAvailable === true ? "Apple device service is available · All files stay on your devices" : connection.serviceAvailable === false ? "Apple device service needs attention. Open the setup guide." : "Apple device service status unavailable");
    text("#download-destination", state.downloadDir || "Choose a destination folder");
    $("#change-destination").title = "Change the queue destination. The saved default folder stays unchanged.";
    $("#reset-destination").title = `Reset to the saved device default folder${current?.defaultDownloadDir ? ` · ${current.defaultDownloadDir}` : ""}`;
    hidden("#scan-banner", !state.scanning);
    if (state.scanning && !$("#scan-message").textContent.startsWith("Reading")) text("#scan-message", "Reading your device library…");
    renderSelection();renderSettings();
  }

  function currentCatalogue() {
    if (ui.view === "apps" && !ui.appId) return [];
    const key = catalogueKey();
    if (ui.catalogues.has(key)) return groupedAssets(ui.catalogues.get(key));
    const source = ui.view === "apps" ? "app" : "photos";
    return groupedAssets((state.catalogue || []).filter((item) => (!item.deviceId || item.deviceId === deviceId()) && (!item.source || item.source === source) && (source !== "app" || ((!item.appId || item.appId === ui.appId) && ((item.parentPath || item.path?.slice(0, item.path.lastIndexOf("/")) || "/Documents") === ui.path)))));
  }

  function sortItems(items) {
    const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
    const timestamp = (item) => new Date(item.modified || 0).getTime() || 0;
    return items.slice().sort((a, b) => {
      if (ui.view === "apps" && (a.kind === "folder") !== (b.kind === "folder")) return a.kind === "folder" ? -1 : 1;
      switch (ui.sort) {
        case "name": return collator.compare(a.name, b.name);
        case "largest": return itemSize(b) - itemSize(a) || collator.compare(a.name, b.name);
        case "smallest": return itemSize(a) - itemSize(b) || collator.compare(a.name, b.name);
        case "oldest": return timestamp(a) - timestamp(b) || collator.compare(a.name, b.name);
        default: return timestamp(b) - timestamp(a) || collator.compare(a.name, b.name);
      }
    });
  }

  function renderCatalogue(force = false) {
    if (!["photos", "apps"].includes(ui.view)) return;
    const directoryRoot = ui.view === "apps" && !ui.appId;
    hidden("#app-directory-grid", !directoryRoot); hidden("#catalogue", directoryRoot);
    hidden(".filter-tabs", directoryRoot); $("#sort-select").disabled = directoryRoot;
    if (directoryRoot) renderDirectoryGrid();
    const all = libraryItems();
    $("#show-hidden-files").checked = ui.showHidden;
    hidden("#other-filter", ui.view !== "apps" && !ui.showHidden);
    const query = ui.search.trim().toLowerCase();
    const filtered = all.filter((item) => (ui.filter === "all" || item.kind === ui.filter || (ui.filter === "file" && item.kind === "folder")) && (!query || item.name?.toLowerCase().includes(query) || item.path?.toLowerCase().includes(query) || (isLivePhoto(item) && (item.livePhoto.video.name?.toLowerCase().includes(query) || item.livePhoto.video.path?.toLowerCase().includes(query)))));
    const signature = `${catalogueKey()}|${directoryRoot ? ui.appsSignature : ""}|${online()}|${ui.filter}|${ui.sort}|${ui.layout}|${ui.showHidden}|${query}|${filtered.map((item) => `${keyOf(item)}:${item.name}:${item.size}:${item.modified}:${item.kind}:${item.localPath || ""}:${item.thumbnailUrl || ""}:${isLivePhoto(item)}:${item.livePhoto?.video?.id || ""}:${item.livePhoto?.video?.localPath || ""}:${item.livePhoto?.video?.modified || ""}:${item.livePhoto?.video?.size ?? ""}:${item.durationSeconds ?? ""}:${item.livePhoto?.video?.durationSeconds ?? ""}`).join(";")}`;
    text("#filter-all-count", all.length.toLocaleString()); text("#filter-video-count", all.filter((item) => item.kind === "video").length.toLocaleString()); text("#filter-photo-count", all.filter((item) => item.kind === "photo").length.toLocaleString()); text("#filter-file-count", all.filter((item) => ["file", "folder"].includes(item.kind)).length.toLocaleString());
    const photos = ui.catalogues.get(`${deviceId() || ""}|photos||`) || state.catalogue.filter((item) => item.source !== "app" && (!item.deviceId || item.deviceId === deviceId()));
    text("#library-count", groupedAssets(photos).filter(previewable).length || "");
    text("#result-count", directoryRoot ? `${state.apps.filter((app) => !query || (app.name || app.id).toLowerCase().includes(query)).length} app directories` : `${filtered.length.toLocaleString()} ${filtered.length === 1 ? "file" : "files"}${query || ui.filter !== "all" ? ` of ${all.length.toLocaleString()}` : ""}`);
    text("#footer-files", all.length ? `${all.length.toLocaleString()} files in this catalogue · Cached for offline browsing` : "Catalogue stays available offline");
    renderEmpty(all);
    if (signature === ui.fileSignature && !force) { renderSelection(); return; }
    ui.fileSignature = signature;
    const scroll = $("#catalogue-scroll").scrollTop;
    ui.visible = sortItems(filtered);
    const existingKeys = new Set(all.map(keyOf));
    for (const key of ui.selected) if (!existingKeys.has(key)) ui.selected.delete(key);
    const catalogue = $("#catalogue");
    ui.thumbObserver?.disconnect(); ui.thumbQueue = [];
    catalogue.className = `catalogue ${ui.layout}-view`;
    const fragment = document.createDocumentFragment();
    ui.visible.forEach((item) => {
      const card = document.createElement("div");
      const key = keyOf(item);
      card.className = `file-card${ui.selected.has(key) ? " selected" : ""}`;
      card.tabIndex = 0; card.setAttribute("role", "button"); card.setAttribute("aria-pressed", String(ui.selected.has(key))); card.setAttribute("aria-label", `${item.name}, ${isLivePhoto(item) ? "Live Photo with motion video" : item.kind}, ${formatBytes(itemSize(item))}`); card.title = item.name; card.dataset.key = key;
      if (item.kind !== "folder") { card.draggable = Boolean(assetDownloaded(item) || online()); card.title = `${item.name}\nDrag to File Explorer to ${assetDownloaded(item) ? `copy the downloaded original${isLivePhoto(item) ? "s (photo + motion video)" : ""}` : "choose a download folder. The transfer starts once the folder is confirmed"}.`; }
      const check = `<span class="file-check">${icon("check")}</span>`;
      const downloaded = assetDownloaded(item) ? `<span class="downloaded-indicator" title="Downloaded · Drag to File Explorer">${icon("check")}</span>` : "";
      const thumbUrl = thumbnailOf(item);
      const thumb = thumbUrl ? `<img src="${escape(thumbUrl)}" loading="lazy" alt="" draggable="false">` : icon(typeIcon(item.kind));
      const live = isLivePhoto(item) ? `<span class="live-photo-badge" title="Live Photo · photo and paired motion video">${icon("live-photo")}LIVE</span>` : "";
      if (ui.layout === "grid") card.innerHTML = `<div class="file-preview ${escape(item.kind)}-preview">${thumb}${check}${downloaded}${live}${durationBadge(item)}<span class="file-extension">${escape(extension(item))}</span><span class="file-size-label">${item.kind === "folder" ? "Folder" : formatBytes(itemSize(item))}</span></div><div class="file-info"><strong>${escape(item.name)}</strong><div class="file-meta"><span>${escape(formatDate(item.modified))}</span>${assetDownloaded(item) ? '<span class="local-label">On PC</span>' : ""}</div></div>`;
      else card.innerHTML = `${check}<div class="file-preview ${escape(item.kind)}-preview">${thumb}${live}</div><div class="file-info"><strong>${escape(item.name)}</strong><div class="file-meta"><span>${escape(formatDate(item.modified))}</span>${assetDownloaded(item) ? '<span class="local-label">On PC</span>' : ""}</div></div><span class="file-extension">${escape(extension(item))}</span>${durationBadge(item)}<span class="file-size-label">${item.kind === "folder" ? "Folder" : formatBytes(itemSize(item))}</span>${downloaded}`;
      fragment.appendChild(card);
    });
    catalogue.replaceChildren(fragment);
    Array.from(catalogue.children).forEach((card, index) => {
      const item = ui.visible[index];
      if (["photo", "video"].includes(item.kind) && (!thumbnailOf(item) || durationOf(item.kind === "video" ? item : item.livePhoto?.video) === null && (item.kind === "video" || isLivePhoto(item)))) ui.thumbObserver?.observe(card);
    });
    hidden("#empty-state", ui.visible.length > 0 || (directoryRoot && $("#app-directory-grid").children.length > 0));
    $("#catalogue-scroll").scrollTop = scroll;
    renderSelection(); renderInspector();
  }

  function renderEmpty(all) {
    if (all.length) { text("#empty-title", "No matching files"); text("#empty-message", "Try another search or switch the file type filter."); text("#empty-action", "Clear filters"); }
    else if (state.scanning) { text("#empty-title", "Reading your library"); text("#empty-message", "Your files will appear as the catalogue becomes available. Large libraries can take a moment."); text("#empty-action", "View connection"); }
    else if (ui.view === "apps" && !ui.appId) { text("#empty-title", "Open an app directory"); text("#empty-message", state.apps.length ? "Open a directory tile or choose an app in the sidebar. Clear your search if no directories match." : "Connect your device to load apps that expose documents through Apple File Sharing."); text("#empty-action", state.apps.length ? "Clear search" : "Load supported apps"); }
    else if (online()) { text("#empty-title", ui.view === "apps" ? "Browse this app’s documents" : "Load your device library"); text("#empty-message", ui.view === "apps" ? "Refresh to list the documents in this app folder." : "Scan the library to load the original photos and videos stored on this device."); text("#empty-action", ui.view === "apps" ? "Refresh catalogue" : "Scan library"); }
    else { text("#empty-title", "No device connected"); text("#empty-message", "Connect and unlock your device, then tap Trust to load your media library."); text("#empty-action", "Check connection"); }
  }

  function observeVisibleThumbnails() {
    if (!["photos", "apps"].includes(ui.view) || !ui.thumbObserver) return;
    $$("#catalogue .file-card").forEach((card) => {
      const item = ui.visible.find((file) => keyOf(file) === card.dataset.key);
      if (item && ["photo", "video"].includes(item.kind) && (!thumbnailOf(item) || durationOf(item.kind === "video" ? item : item.livePhoto?.video) === null && (item.kind === "video" || isLivePhoto(item)))) ui.thumbObserver.observe(card);
    });
  }

  function applyThumbnail(item, url) {
    const key = thumbnailKey(item);
    ui.thumbnails.set(key, url);
    $$("#catalogue .file-card").filter((card) => card.dataset.key === keyOf(item)).forEach((card) => {
      const current = ui.visible.find((entry) => keyOf(entry) === card.dataset.key);
      if (!current || thumbnailKey(current) !== key) return;
      const container = card.querySelector(".file-preview"), existing = container?.querySelector(":scope > img, :scope > svg");
      if (!existing) return;
      const image = document.createElement("img"); image.src = url; image.alt = ""; image.loading = "lazy"; image.draggable = false;
      image.addEventListener("error", () => {
        if (ui.thumbnails.get(key) === url) ui.thumbnails.delete(key);
        const fallback = document.createElement("span"); fallback.innerHTML = icon(typeIcon(item.kind));
        if (image.isConnected) image.replaceWith(fallback.firstElementChild);
      }, { once: true });
      existing.replaceWith(image);
    });
    if (ui.selected.size === 1 && ui.selected.has(keyOf(item)) && !ui.preview?.url && !ui.previewPending) renderInspector();
  }

  function pumpThumbnails() {
    while (ui.thumbActive < 2 && ui.thumbQueue.length) {
      const task = ui.thumbQueue.shift();
      if (!["photos", "apps"].includes(ui.view) || task.deviceId !== deviceId() || thumbnailOf(task.item) || ui.thumbPending.has(task.key) || ui.thumbAttempted.has(task.key)) continue;
      ui.thumbActive++; ui.thumbPending.add(task.key); ui.thumbAttempted.add(task.key);
      getThumbnail(task.item).then((result) => {
        if (result?.url && result.available !== false && (ui.cacheGeneration.get(task.deviceId) || 0) === task.generation) applyThumbnail(task.item, result.url);
        else if (task.item.kind === "photo" && ui.selected.size === 1 && ui.selected.has(keyOf(task.item))) automaticPhotoPreview(task.item);
      }).catch(() => { /* A best-effort thumbnail never interrupts browsing or transfers. */ }).finally(() => {
        ui.thumbActive--; ui.thumbPending.delete(task.key); pumpThumbnails();
      });
    }
  }

  function getThumbnail(item) {
    const key = thumbnailKey(item);
    if (!thumbnailRequests.has(key)) {
      const pending = request("thumbnail", { item }).finally(() => thumbnailRequests.delete(key));
      thumbnailRequests.set(key, pending);
    }
    return thumbnailRequests.get(key);
  }

  function selectedItems() { return currentCatalogue().filter((item) => ui.selected.has(keyOf(item))); }

  function toggleHiddenFiles() {
    const anchorItem=ui.anchor===null?null:ui.visible[ui.anchor], anchorKey=anchorItem?keyOf(anchorItem):null;
    ui.showHidden=$("#show-hidden-files").checked;
    // Documents is only available while Photos exposes its hidden sidecars.
    if(!ui.showHidden&&ui.filter==="file") {
      ui.filter="all";
      $$(".filter-tab").forEach(button=>button.classList.toggle("active",button.dataset.filter==="all"));
    }
    ui.fileSignature="";renderCatalogue(true);
    // The same visible original remains the range anchor after rows disappear.
    const nextAnchor=anchorKey?ui.visible.findIndex(item=>keyOf(item)===anchorKey):-1;
    ui.anchor=nextAnchor>=0?nextAnchor:null;
  }
  function phoneFolderDeleteMarkup(items) {
    if (!items.length || !items.every(item => item.source === "app")) return "";
    const folders = items.filter(item => item.kind === "folder");
    const label = folders.length ? folders.length === items.length ? `Delete ${folders.length > 1 ? `${folders.length} folders` : "folder"}` : `Delete ${items.length} items from device` : `Delete ${items.length > 1 ? `${items.length} files` : "file"} from device`;
    return `<div class="phone-delete-actions"><button class="button danger" data-inspector-action="delete-phone"${online() && !ui.operationBusy ? "" : " disabled"}>${icon("trash")}${label}</button><p>${folders.length ? "Folders and everything inside them are" : "Files are"} permanently deleted from the device. This cannot be undone. PC copies remain.</p></div>`;
  }
  function renderSelection() {
    const items = selectedItems(), files = items.filter(item => item.kind !== "folder"), pending = pendingDownloads(files);
    const total = files.reduce((sum, item) => sum + itemSize(item), 0), onPc = files.length - pending.length;
    text("#selection-label", items.length ? `${items.length.toLocaleString()} ${items.length === 1 ? "file" : "files"} selected` : "No files selected");
    text("#selection-size", items.length ? `${formatBytes(total)}${onPc ? ` · ${onPc} already on PC` : items.some(item => item.kind === "folder") ? " · Open folders to select their files" : items.some(isLivePhoto) ? " · Live Photos include photo + motion video" : " · Original files"}` : "Click a file to inspect it · Ctrl-click to select more");
    hidden("#clear-selection", !items.length);
    const selectionAction = $("#download-selected"), label = ui.downloadBusy || ui.activationBusy ? "Preparing downloads…" : files.length ? pending.length ? "Download selected" : "Already on PC" : "Select all";
    if (selectionAction.dataset.mode !== label) { selectionAction.dataset.mode = label; selectionAction.innerHTML = `${ui.downloadBusy ? '<span class="spinner"></span>' : icon(files.length && pending.length ? "arrow-down" : "check")}${label}`; }
    selectionAction.disabled = ui.downloadBusy || ui.activationBusy || (files.length ? !pending.length || !online() : !ui.visible.some(item => item.kind !== "folder"));
    selectionAction.setAttribute("aria-busy", String(ui.downloadBusy || ui.activationBusy));
    hidden("#download-processing", !ui.downloadBusy && !ui.activationBusy);
    $$(".file-card").forEach(card => { const selected = ui.selected.has(card.dataset.key); card.classList.toggle("selected", selected); card.setAttribute("aria-pressed", String(selected)); });
  }

  function pcDeleteMarkup(items) {
    const count = items.filter(assetDownloaded).length;
    return count ? `<div class="pc-delete-actions"><button class="button secondary" data-inspector-action="delete-pc"${ui.operationBusy ? " disabled" : ""}>${icon("trash")}Delete ${count > 1 ? `${count} PC copies` : "from PC"}</button></div>` : "";
  }

  function updateMountedInspector(item) {
    const target=$("#inspector-content"),onPc=assetDownloaded(item);
    updatePreviewProgress();updateInspectorDuration(item);
    const subtitle=target.querySelector('.inspector-subtitle');if(subtitle)subtitle.textContent=`${extension(item)} · ${item.localPath?"Local copy available":"On device"}`;
    const download=target.querySelector('[data-inspector-action="download"]');
    if(download){const label=onPc?"Already on PC":"Download original";download.disabled=onPc||!online()||ui.downloadBusy;if(download.dataset.label!==label){download.dataset.label=label;download.innerHTML=`${icon(onPc?"check":"arrow-down")}${label}`;}}
    const actions=target.querySelector('.inspector-actions');let open=actions?.querySelector('[data-inspector-action="open"]');
    const stream=actions?.querySelector('[data-inspector-action="stream"]'),preview=actions?.querySelector('[data-inspector-action="preview"]'),compatible=actions?.querySelector('[data-inspector-action="compatible"]');
    if(stream)stream.disabled=!online()||ui.previewPending;if(preview)preview.disabled=!item.localPath||ui.previewPending;if(compatible)compatible.disabled=(!item.localPath&&!online())||ui.previewPending;
    if(onPc&&!open&&actions){actions.insertAdjacentHTML('beforeend',`<button class="button secondary" data-inspector-action="open">${icon("external")}Show in File Explorer</button>`);open=actions.lastElementChild;}if(open)open.classList.toggle('hidden',!onPc);
    const pcDelete=target.querySelector('.pc-delete-actions');if(onPc&&!pcDelete)target.insertAdjacentHTML('beforeend',pcDeleteMarkup([item]));else if(!onPc&&pcDelete)pcDelete.remove();else if(pcDelete)pcDelete.querySelector('button').disabled=ui.operationBusy;
    const status=target.querySelector('.stream-status > span');if(status)status.textContent=ui.preview?.streaming?onPc?"Live from device · Original also on this PC":"Live from device — not downloaded":onPc?"Downloaded original on this PC":"Local preview · Original no longer in the default folder";
  }

  function renderInspector() {
    const items = selectedItems();
    const target = $("#inspector-content");
    const contentKey = items.length === 1 ? keyOf(items[0]) : "";
    if (ui.previewKey && ui.previewKey !== contentKey) { releasePreview(); ui.playerSeek = null; ui.playerScrubbing = false; ui.preview = null; ui.previewKey = ""; ui.previewPending = false; ui.previewStage = null; ui.previewToken++; }
    if (!items.length) { ui.playerSeek = null; ui.playerScrubbing = false; text("#inspector-kind", "INSPECTOR"); target.innerHTML = `<div class="inspector-placeholder"><div class="inspector-preview">${icon("file")}</div><p>Select a file to see its size, date, location, and available preview.</p></div>`; return; }
    if (items.length > 1) {
      text("#inspector-kind", "SELECTION");
      const total = items.reduce((sum, item) => sum + itemSize(item), 0);
      target.innerHTML = `<div class="inspector-multi"><strong>${formatBytes(total)}</strong><span>${items.length.toLocaleString()} files selected</span>${detailRows([["Videos", items.filter((item) => item.kind === "video").length], ["Photos", items.filter((item) => item.kind === "photo").length], ["Other files", items.filter((item) => !["video", "photo"].includes(item.kind)).length]])}<div class="inspector-actions"><button class="button primary" data-inspector-action="download"${online() && pendingDownloads(items).length && !ui.downloadBusy ? "" : " disabled"}>${icon(pendingDownloads(items).length ? "arrow-down" : "check")}${pendingDownloads(items).length ? "Download selection" : "Already on PC"}</button></div>${pcDeleteMarkup(items)}${phoneFolderDeleteMarkup(items)}</div>`; return;
    }
    const item = items[0];
    ensureVideoMetadata(item);
    text("#inspector-kind", isLivePhoto(item) ? "LIVE PHOTO" : item.kind.toUpperCase());
    // Keep a playing video mounted while state/progress messages update elsewhere.
    if (target.dataset.itemKey === contentKey && target.querySelector("video") && ui.preview?.url) { target.dataset.previewUrl = ui.preview.url; bindPlayerSource(targetVideo()); updateMountedInspector(item); return; }
    target.dataset.itemKey = contentKey;
    target.dataset.previewUrl = ui.preview?.url || "";
    let preview = thumbnailOf(item) ? `<img src="${escape(thumbnailOf(item))}" alt="Preview of ${escape(item.name)}" draggable="false">` : icon(typeIcon(item.kind));
    if (ui.preview?.url) {
      const url = escape(ui.preview.url);
      if (item.kind === "video") preview = `<video controls playsinline preload="metadata" draggable="false" tabindex="0" aria-label="${ui.preview.streaming ? "Video streaming from your device" : "Downloaded video preview"}"></video>`;
      else if (item.kind === "photo") preview = `<img src="${url}" alt="Preview of ${escape(item.name)}" draggable="false">`;
    }
    const appName = state.apps.find((app) => app.id === item.appId)?.name || item.appId;
    const mediaActions = item.kind === "video"
      ? `<button class="button secondary" data-inspector-action="stream"${!online() || ui.previewPending ? " disabled" : ""}>${icon("play")}${ui.previewPending ? "Starting preview…" : "Stream from device"}</button>${item.localPath ? `<button class="button secondary" data-inspector-action="preview"${ui.previewPending ? " disabled" : ""}>${icon("play")}Play downloaded original</button>` : ""}<button class="button secondary" data-inspector-action="compatible"${(!item.localPath && !online()) || ui.previewPending ? " disabled" : ""}>${icon("video")}Play compatible preview</button>`
      : item.kind === "photo" ? `<button class="button secondary" data-inspector-action="preview"${ui.previewPending ? " disabled" : ""}>${icon("photo")}${ui.previewPending ? "Loading preview…" : "Refresh photo preview"}</button>` : "";
    const streamStatus = ui.preview?.streaming ? `<div class="stream-status">${icon("signal")}<span>${assetDownloaded(item)?"Live from device · Original also on this PC":"Live from device — not downloaded"}${ui.preview.mode === "compatible" ? "<small>Compatible preview · Original stays unchanged</small>" : ""}</span></div>` : item.localPath ? `<div class="stream-status local-status">${icon("check")}<span>${isLivePhoto(item) ? assetDownloaded(item) ? "Photo + motion video on this PC" : "Still photo on PC · motion video on device" : "Downloaded original on this PC"}</span></div>` : "";
    const seekControl = item.kind === "video" && ui.preview?.url ? playerControlsMarkup(item) : "";
    const liveDetails = isLivePhoto(item) ? [["Still photo", `${item.name} · ${formatBytes(item.size)}`], ["Motion video", `${item.livePhoto.video.name} · ${formatBytes(item.livePhoto.video.size)}`], ["Motion duration", formatDuration(durationOf(item.livePhoto.video)), true]] : [];
    const durationDetails = item.kind === "video" ? [["Duration", formatDuration(durationOf(item)), true]] : [];
    const playingVideo = item.kind === "video" && Boolean(ui.preview?.url);
    const dragHandle = item.kind !== "folder" && (assetDownloaded(item) || online()) ? `<span class="preview-drag-handle" draggable="true">${icon("file")}${isLivePhoto(item) ? "Drag 2 originals" : "Drag original"}</span>` : "";
    target.innerHTML = `<div class="inspector-preview${playingVideo ? " video-player" : ""}" draggable="${Boolean(!playingVideo && item.kind !== "folder" && (assetDownloaded(item) || online()))}" data-preview-drag="${escape(contentKey)}" title="Drag this preview to File Explorer to ${assetDownloaded(item) ? `copy the downloaded original${isLivePhoto(item) ? "s" : ""}` : "choose a download folder. The transfer starts once the folder is confirmed"}.">${preview}${durationBadge(item, true)}${dragHandle}${playingVideo ? seekControl : ""}</div>${previewProgressMarkup(item)}${isLivePhoto(item) ? `<div class="live-photo-description">${icon("live-photo")}<span>Live Photo · photo + motion video</span></div>` : ""}${streamStatus}<div class="inspector-name">${escape(item.name)}</div><div class="inspector-subtitle">${escape(item.kind === "folder" ? "App folder" : `${extension(item)} · ${item.localPath ? "Local copy available" : "On device"}`)}</div>${detailRows([["File size", item.kind === "folder" ? "Folder" : formatBytes(itemSize(item))], ...liveDetails, ...durationDetails, ["Modified", formatDate(item.modified, true)], ["Location", item.source === "app" ? appName || "App documents" : "Photos App"], ["Device", nameOf(device())], ["Device path", item.path || "Unavailable"]])}<div class="inspector-actions">${item.kind === "folder" ? `<button class="button secondary" data-inspector-action="folder">${icon("folder")}Open folder</button>` : `<button class="button primary" data-inspector-action="download"${online() && !assetDownloaded(item) && !ui.downloadBusy ? "" : " disabled"}>${icon(assetDownloaded(item) ? "check" : "arrow-down")}${assetDownloaded(item) ? "Already on PC" : isLivePhoto(item) ? "Download photo + video" : "Download original"}</button><button class="button secondary" data-inspector-action="download-to"${online() ? "" : " disabled"}>${icon("folder")}Download to folder…</button>${mediaActions}`}${item.localPath ? `<button class="button secondary" data-inspector-action="open">${icon("external")}Show in File Explorer</button>` : ""}</div>${ui.preview?.message ? `<p class="preview-message">${escape(ui.preview.message)}</p>` : item.kind !== "folder" ? `<p class="preview-message">Drag this preview or its library tile into a File Explorer folder to ${assetDownloaded(item) ? isLivePhoto(item) ? "copy both downloaded Live Photo originals." : "copy its downloaded original." : "choose its download folder. The transfer starts once the folder is confirmed."}</p>` : ""}`;
    target.insertAdjacentHTML("beforeend", pcDeleteMarkup(items) + phoneFolderDeleteMarkup(items));
    bindPlayerSource(targetVideo());
    updatePreviewProgress();
  }

  function selectItem(item, event) {
    const before = historySnapshot();
    const key = keyOf(item), index = ui.visible.findIndex((entry) => keyOf(entry) === key);
    if (event.shiftKey && ui.anchor !== null) {
      if (!event.ctrlKey && !event.metaKey) ui.selected.clear();
      const [start, end] = [ui.anchor, index].sort((a, b) => a - b);
      ui.visible.slice(start, end + 1).forEach((entry) => ui.selected.add(keyOf(entry)));
    } else if (event.ctrlKey || event.metaKey || event.target?.closest?.(".file-check")) {
      if (ui.selected.has(key)) ui.selected.delete(key); else ui.selected.add(key);
      ui.anchor = index;
    } else { ui.selected.clear(); ui.selected.add(key); ui.anchor = index; }
    renderSelection(); renderInspector();
    if (ui.selected.size === 1 && item.kind === "photo" && !thumbnailOf(item)) automaticPhotoPreview(item);
    prepareNativeDrag(dragFilesFor(item));
    recordViewChange("selection", before);
  }

  function renderApps() {
    const signature = `${deviceId()}|${ui.appId}|${state.apps.map((app) => `${app.id}:${app.name}`).join(";")}`;
    if (signature === ui.appsSignature) return;
    ui.appsSignature = signature;
    $("#app-directory-list").innerHTML = state.apps.slice().sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id)).map((app) => `<button class="directory-sidebar-button${app.id === ui.appId ? " active" : ""}" data-app-id="${escape(app.id)}" title="${escape(app.name || app.id)}">${icon("folder")}<span>${escape(app.name || app.id)}</span></button>`).join("");
    renderDirectoryGrid();
    renderAppBreadcrumbs();
  }

  function renderDirectoryGrid() {
    const query = ui.search.trim().toLowerCase();
    const apps = state.apps.filter((app) => !query || (app.name || app.id).toLowerCase().includes(query)).sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
    const signature = `${online()}|${apps.map((app) => `${app.id}|${app.name}`).join(";")}`;
    const grid = $("#app-directory-grid");
    grid.classList.toggle("list-view", ui.layout === "list");
    if (grid.dataset.signature === signature) return;
    grid.dataset.signature = signature;
    grid.innerHTML = apps.map((app) => `<button class="app-directory-tile" data-app-id="${escape(app.id)}" aria-label="Open ${escape(app.name || app.id)} directory">${icon("folder")}<div><strong>${escape(app.name || app.id)}</strong><span>App documents${online() ? "" : " · Offline cache"}</span></div>${icon("chevron")}</button>`).join("");
  }

  function renderAppBreadcrumbs() {
    const parts = ui.path.split("/").filter(Boolean), app = state.apps.find((entry) => entry.id === ui.appId);
    $("#app-breadcrumbs").innerHTML = `<button data-app-root="true">On device</button>${ui.appId ? `${icon("chevron")}<button data-app-path="/Documents">${escape(app?.name || ui.appId)}</button>${parts.slice(1).map((part, index) => `${icon("chevron")}<button data-app-path="/${escape(parts.slice(0, index + 2).join("/"))}">${escape(part)}</button>`).join("")}` : ""}`;
    $("#app-up").disabled = !ui.appId;
    $("#app-back").disabled = !(ui.history.get(deviceId()) || []).length;
  }

  function renderJobs() {
    const jobs = state.jobs || [], active = activeJobs(), clearFinished = $("#clear-finished-jobs"), batch = $("#queue-batch-action");
    clearFinished.disabled = ui.clearingFinished || ui.queueBusy || !jobs.some(job => ["completed", "failed", "cancelled"].includes(job.state) && !job.remoteCleanupPending && !job.localPartRetained);
    clearFinished.title = jobs.some(job=>job.localPartRetained) ? "Clear finished entries. Recoverable downloads stay in the queue; use Retry to finish them." : "Remove finished queue entries. Saved originals stay on your PC.";
    clearFinished.classList.toggle("hidden", active.length===0);
    const stop = active.length > 0 || ui.downloadBusy || ui.uploadBusy || ui.activationBusy, stopping = active.length > 0 && active.every(job=>job.cancelRequested), batchLabel = ui.queueBusy || stopping ? stop ? "Stopping…" : "Clearing…" : stop ? "Stop all" : "Clear all";
    if (batch.dataset.mode !== batchLabel) { batch.dataset.mode = batchLabel; batch.innerHTML = `${ui.queueBusy ? '<span class="spinner"></span>' : icon(stop ? "close" : "trash")}${batchLabel}`; }
    batch.disabled = ui.queueBusy || stopping || !jobs.length && !ui.downloadBusy && !ui.uploadBusy && !ui.activationBusy; batch.title = stop ? "Stop every transfer, including queued and paused files." : "Remove every queue entry, including files that did not transfer. Downloaded originals stay on your PC.";
    const awaitingFolder = active.filter(job => job.awaitingDestination), ready = active.filter(job => job.awaitingStart && !job.awaitingDestination), running = active.filter(job => !job.awaitingStart && !job.awaitingDestination);
    text("#queue-count", active.length || "");
    text("#queue-summary-title", running.length ? `${running.length} ${running.length === 1 ? "transfer" : "transfers"} in progress` : ready.length ? `${ready.length} ${ready.length === 1 ? "transfer" : "transfers"} ready to start` : awaitingFolder.length ? "Choose a download destination" : jobs.length ? "Your transfer history" : "Your transfer queue");
    const rate = running.reduce((total, job) => total + (number(job.speed) || 0), 0);
    text("#queue-summary-detail", [awaitingFolder.length ? `${awaitingFolder.length} ${awaitingFolder.length===1?"needs":"need"} a folder` : "", ready.length ? `${ready.length} not started` : "", running.length ? rate > 0 ? `${(rate / 1000000).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB/s combined` : "Transfers update automatically" : jobs.length ? `${jobs.filter(job => job.state === "completed").length} completed` : "Downloads and uploads appear here."].filter(Boolean).join(" · "));
    const visible = jobs.map((job, index) => ({ job, index })).filter(({ job }) => !ui.hideCompleted || job.state !== "completed");
    const time = ({job,index}) => number(job.createdAt) ?? number(job.created) ?? index;
    const ranks = { transferring: 0, queued: 1, waiting: 2, waiting_for_approval: 2, paused: 3, failed: 4, cancelled: 5, completed: 6 };
    visible.sort((a,b) => ui.queueSort === "name" ? String(a.job.name || "").localeCompare(String(b.job.name || "")) || a.index-b.index : ui.queueSort === "status" ? (ranks[a.job.state] ?? 9)-(ranks[b.job.state] ?? 9) || a.index-b.index : ui.queueSort === "oldest" ? time(a)-time(b) : time(b)-time(a));
    hidden("#jobs-empty", visible.length > 0);
    text("#jobs-empty h2", jobs.length && !visible.length ? "Completed transfers are hidden" : "No transfers");
    text("#jobs-empty p", jobs.length && !visible.length ? "Turn off Hide completed to see your transfer history." : "Downloads and uploads appear here.");
    const list = $("#job-list"), keep = new Set(visible.map(({job}) => String(job.id))), nodes = new Map(Array.from(list.children).map(node => [node.dataset.job,node]));
    for (const [id,node] of nodes) if (!keep.has(id)) { node.remove(); nodes.delete(id); }
    let cursor = list.firstElementChild;
    for (const {job} of visible) {
      const id = String(job.id); let card = nodes.get(id);
      if (!card) {
        card = document.createElement("article"); card.dataset.job = id;
        card.innerHTML = `<span class="job-icon"></span><div class="job-info"><div class="job-title-row"><strong data-job-name></strong><span class="job-state"></span></div><p class="job-destination">${icon("folder")}<span><strong>Destination</strong><span data-job-destination></span></span></p><div class="job-meter" role="progressbar" aria-valuemin="0" aria-valuemax="100"><span></span></div><div class="job-meta"><span data-job-progress></span><span class="speed"></span><span data-job-time></span></div><p class="job-note"></p><p class="job-error"></p></div><div class="job-actions"><button class="button secondary job-text-action" data-job-action="choose-folder">${icon("folder")}Choose folder…</button><button class="button primary job-text-action" data-job-action="start">${icon("play")}Start transfer</button><button class="icon-button" data-job-action="pause" title="Pause transfer" aria-label="Pause transfer">${icon("pause")}</button><button class="icon-button" data-job-action="retry"></button><button class="icon-button" data-job-action="cancel" title="Stop transfer" aria-label="Stop transfer">${icon("close")}</button><button class="icon-button" data-job-action="open" title="Show in File Explorer" aria-label="Show in File Explorer">${icon("folder")}</button></div>`;
      }
      if (card !== cursor) list.insertBefore(card, cursor); cursor = card.nextElementSibling;
      const write = (selector,value) => { const node=card.querySelector(selector); if (node.textContent!==String(value)) node.textContent=value; return node; };
      const show = (selector,visible) => card.querySelector(selector).classList.toggle("hidden", !visible);
      const transferred = Math.max(0, number(job.bytes)||0), total = Math.max(0, number(job.total)||0), percent = total ? Math.min(100,transferred/total*100) : job.state === "completed" ? 100 : 0;
      const awaitingDestination = Boolean(job.awaitingDestination), awaitingStart = Boolean(job.awaitingStart)&&!awaitingDestination, notStarted = awaitingDestination||awaitingStart, explorer = id.startsWith("explorer-"), busy = ui.jobBusy.has(id)||Boolean(job.cancelRequested);
      const labels = {queued:"Queued",transferring:job.direction==="upload"?"Uploading":"Downloading",paused:"Paused",waiting:"Waiting for device",waiting_for_approval:"Waiting for device acceptance",failed:"Transfer failed",completed:"Complete",cancelled:"Cancelled"};
      card.className = `job-card ${job.state}${notStarted ? " not-started" : ""}`;
      const iconName = job.state === "completed" ? "check" : job.direction === "upload" ? "arrow-up" : "arrow-down", iconHost=card.querySelector(".job-icon");
      if (iconHost.dataset.icon!==iconName) {iconHost.dataset.icon=iconName;iconHost.innerHTML=icon(iconName);}
      write("[data-job-name]",job.name||"File transfer").title=job.name||"File transfer";
      write(".job-state",job.cancelRequested ? "Stopping…" : awaitingDestination ? "Choose destination" : awaitingStart ? "Not started" : labels[job.state]||job.state);
      show(".job-destination",Boolean(job.destination)||awaitingDestination); const dest=write("[data-job-destination]",job.destination||"Choose a folder to start this transfer automatically."); dest.title=job.destination||"";
      show(".job-meter",!notStarted); const meter=card.querySelector(".job-meter");meter.setAttribute("aria-label",`${job.name||"File"} transfer progress`);meter.setAttribute("aria-valuenow",String(Math.round(percent)));meter.firstElementChild.style.width=`${percent}%`;
      write("[data-job-progress]",notStarted ? `${total ? `${formatBytes(total)} · ` : ""}No file data transferred` : `${formatBytes(transferred)}${total ? ` / ${formatBytes(total)}` : ""} · ${Math.round(percent)}%`);
      const transferring=!notStarted&&job.state==="transferring";show(".speed",transferring);write(".speed",number(job.speed)>0?`${(job.speed/1000000).toLocaleString(undefined,{maximumFractionDigits:1})} MB/s`:"Measuring speed…");write("[data-job-time]",transferring?formatEta(job.eta):job.direction==="upload"?"PC → Device":"Device → PC");
      write(".job-note",job.message||"");show(".job-note",Boolean(job.message));write(".job-error",job.error||"");show(".job-error",Boolean(job.error));
      const states={"choose-folder":awaitingDestination,start:awaitingStart&&job.state==="paused",pause:!notStarted&&!explorer&&["queued","transferring"].includes(job.state),retry:!notStarted&&!explorer&&["paused","failed","waiting","cancelled"].includes(job.state),cancel:notStarted||["queued","transferring","paused","waiting","waiting_for_approval"].includes(job.state),open:job.state==="completed"&&Boolean(job.localPath)};
      for(const [action,visible] of Object.entries(states)){const button=card.querySelector(`[data-job-action="${action}"]`);button.classList.toggle("hidden",!visible);button.disabled=busy&&action!=="open";}
      const retry=card.querySelector('[data-job-action="retry"]'), retryLabel=job.state==="paused"?"Resume transfer":"Retry transfer";
      if(retry.dataset.label!==retryLabel){retry.dataset.label=retryLabel;retry.title=retryLabel;retry.setAttribute("aria-label",retryLabel);retry.innerHTML=icon(job.state==="paused"?"play":"refresh");}
    }
  }

  function mergeState(incoming) {
    if (!incoming || typeof incoming !== "object") return;
    const previousDevice = deviceId();
    const wasOnline = online();
    const previouslyOnline = new Set(state.devices.filter((item) => item.online).map((item) => item.id));
    if (Array.isArray(incoming.devices)) for (const saved of incoming.devices) {
      const previous = state.devices.find(item => item.id === saved.id);
      if (!previous || previous.defaultDownloadDir === saved.defaultDownloadDir) continue;
      for (const [key, catalogue] of ui.catalogues) if (key.startsWith(`${saved.id}|`)) {
        for (const item of catalogue) { delete item.localPath; if (item.livePhoto?.video) delete item.livePhoto.video.localPath; }
        ui.hydrated.delete(key);
      }
      ui.fileSignature = "";
    }
    if (Array.isArray(incoming.catalogue)) {
      const device = typeof incoming.selectedDevice === "object" ? incoming.selectedDevice?.id : incoming.selectedDevice || previousDevice;
      const grouped = new Map();
      incoming.catalogue.forEach((item) => {
        const source = item.source === "app" ? "app" : "photos";
        const key = `${item.deviceId || device || ""}|${source}|${source === "app" ? item.appId || "" : ""}|${source === "app" ? item.parentPath || item.path?.slice(0, item.path.lastIndexOf("/")) || "/Documents" : ""}`;
        if (!grouped.has(key)) grouped.set(key, []);
        grouped.get(key).push(item);
      });
      if (incoming.currentSource) {
        const source = incoming.currentSource === "app" ? "app" : "photos";
        const scopeKey = `${device || ""}|${source}|${source === "app" ? incoming.currentAppId || "" : ""}|${source === "app" ? incoming.currentPath || "/Documents" : ""}`;
        if (!grouped.has(scopeKey)) grouped.set(scopeKey, []);
      }
      grouped.forEach((items, key) => ui.catalogues.set(key, items));
    }
    Object.assign(state, incoming);
    if (!ui.backendHistoryInitialized && Array.isArray(incoming.devices) && incoming.selectedDevice) {
      ui.backendHistoryInitialized = true; seedBackendHistory(state, "undo"); seedBackendHistory(state, "redo");
    }
    if (!Array.isArray(state.devices)) state.devices = [];
    if (!Array.isArray(state.catalogue)) state.catalogue = [];
    if (!Array.isArray(state.apps)) state.apps = [];
    if (!Array.isArray(state.jobs)) state.jobs = [];
    if (!state.selectedDevice && state.devices.length) state.selectedDevice = state.devices.find((item) => item.online)?.id || state.devices[0].id;
    state.devices.filter((item) => item.online && (!previouslyOnline.has(item.id) || (item.id === deviceId() && !wasOnline && online()))).forEach((item) => {
      const prefix = `${item.id}|`;
      for (const key of ui.thumbAttempted) if (key.startsWith(prefix)) ui.thumbAttempted.delete(key);
      for (const key of videoMetadataAttempted) if (key.startsWith(prefix)) videoMetadataAttempted.delete(key);
      observeVisibleThumbnails();
    });
    if (previousDevice !== deviceId()) { ui.navigationGeneration++; clearSelection(); restoreRoute(deviceId()); ui.fileSignature = ""; }
    renderDevices(); renderConnection(); renderApps(); renderJobs(); checkUploadCompletions(); scheduleCatalogue();
    if (["photos", "apps"].includes(ui.view)) hydrateCatalogue();
  }

  function scheduleCatalogue(force = false) {
    clearTimeout(ui.renderTimer);
    ui.renderTimer = setTimeout(() => renderCatalogue(force), 50);
  }

  function onEvent(event) {
    const payload = event?.data;
    switch (event?.event) {
      case "app_settings": applyAppSettings(payload); break;
      case "update_status": if (payload?.status) { const previous = updateStatus.status; updateStatus = payload; renderUpdateStatus(); if (payload.status === "available" && previous !== "available" && !$("#settings-dialog").open) toast(`EyeCopy ${payload.availableVersion} is available. Open Settings to update.`, false, 14000); } break;
      case "open_updates": openSettings().then(() => { if (appSettings.updateRepository) checkUpdates(); }); break;
      case "state": mergeState(payload?.state || payload); break;
      case "catalogue": {
        if (!payload || !Array.isArray(payload.items)) break;
        const source = payload.source === "app" ? "app" : "photos";
        const items = payload.items.map((item) => ({ ...item, deviceId: item.deviceId || payload.deviceId, source: item.source || source, ...(source === "app" ? { appId: item.appId || payload.appId, parentPath: item.parentPath || payload.path || "/Documents" } : {}) }));
        ui.catalogues.set(`${payload.deviceId || deviceId()}|${source}|${source === "app" ? payload.appId || "" : ""}|${source === "app" ? payload.path || "/Documents" : ""}`, items);
        if (source === "photos" && payload.deviceId === deviceId()) state.catalogue = state.catalogue.filter((item) => item.source === "app" || item.deviceId && item.deviceId !== payload.deviceId).concat(items);
        scheduleCatalogue(); break;
      }
      case "jobs": state.jobs = Array.isArray(payload) ? payload : payload?.jobs || []; renderJobs(); checkUploadCompletions(); renderUpdateStatus(); break;
      case "apps": state.apps = Array.isArray(payload) ? payload : payload?.apps || []; renderApps(); scheduleCatalogue(); break;
      case "scan": state.scanning = Boolean(payload?.scanning); hidden("#scan-banner", !state.scanning); text("#scan-message", `Reading your device library${number(payload?.count) !== null ? ` · ${Number(payload.count).toLocaleString()} files found` : ""}…`); if (payload?.error) toast(payload.error, true); renderConnection(); if (!state.scanning) { scheduleCatalogue(); if (metadataScanNeeded.has(deviceId())) hydrateCatalogue(); } break;
      case "connection": state.connection = payload || {}; renderConnection(); break;
      case "usb": updateUsb(payload); break;
      case "video_metadata": applyVideoMetadata(payload); break;
      case "error": toast(typeof payload === "string" ? payload : payload?.message || payload?.error || "A device operation failed.", true); break;
    }
  }

  function updateUsb(result) {
    if (!result) return;
    if (result.Phones) {
      const phone = result.Phones.find((item) => item.DeviceId === deviceId());
      const link = phone?.UsbConnection?.find((item) => item.ConnectionStatus === 1 && item.V2QuerySucceeded === true);
      state.usb = { deviceId: phone?.DeviceId, negotiatedSpeed: link?.NegotiatedSpeed, signalingRate: link?.SignalingRate, receiveBitsPerSecond: link?.ReceiveBitsPerSecond, transmitBitsPerSecond: link?.TransmitBitsPerSecond, verified: Boolean(link), capturedAt: result.CapturedAtUtc, message: link ? "Windows reported this device’s physical USB link. Signaling rate differs from file transfer throughput." : "No verified USB link was reported for the selected device. Reconnect it and refresh diagnostics." };
    } else if (result.usb) state.usb = result.usb;
    else state.usb = result;
    renderConnection();
  }

  async function scan() {
    if (!online()) { toast("Reconnect and unlock your device to refresh its catalogue."); return; }
    if (ui.view === "apps" && !ui.appId) { await loadApps(); return; }
    const target = { deviceId: deviceId(), source: ui.view === "apps" ? "app" : "photos", ...(ui.view === "apps" ? { appId: ui.appId, path: ui.path } : {}) };
    for (const key of videoMetadataAttempted) if (key.startsWith(`${target.deviceId}|`)) videoMetadataAttempted.delete(key);
    await action("scan", target, (result) => {
      if (result?.items) onEvent({ event: "catalogue", data: { ...target, items: result.items } });
      if (result?.devices) mergeState(result);
    });
  }

  async function loadApps() {
    if (!online()) return;
    const requestedDevice = deviceId();
    $("#list-apps-button").disabled = true;
    await action("list_apps", { deviceId: requestedDevice }, (result) => {
      if (requestedDevice !== deviceId()) return;
      if (Array.isArray(result)) state.apps = result;
      else if (result?.apps) state.apps = result.apps;
      renderApps(); renderCatalogue();
    });
    if (requestedDevice === deviceId()) $("#list-apps-button").disabled = !online();
  }

  async function hydrateCatalogue() {
    if (!deviceId() || !["photos", "apps"].includes(ui.view) || (ui.view === "apps" && !ui.appId)) return;
    const requestedDevice = deviceId(), source = ui.view === "apps" ? "app" : "photos", appId = source === "app" ? ui.appId : undefined, path = source === "app" ? ui.path : "/DCIM";
    const key = catalogueKey(), generation = ui.cacheGeneration.get(requestedDevice) || 0;
    if (!ui.hydrated.has(key)) {
      if (ui.hydrating.has(key)) return ui.hydrating.get(key);
      const work = (async () => {
        const result = await action("get_cached_catalogue", { deviceId: requestedDevice, source, ...(appId ? { appId } : {}), path });
        if ((ui.cacheGeneration.get(requestedDevice) || 0) !== generation) return;
        if (result && Array.isArray(result.items)) onEvent({ event: "catalogue", data: { deviceId: requestedDevice, source, appId, path, ...result } });
        if (source === "photos" && result?.needsLivePhotoScan) metadataScanNeeded.add(requestedDevice);
        ui.hydrated.add(key);
      })();
      ui.hydrating.set(key, work);
      try { await work; } finally { ui.hydrating.delete(key); }
    }
    if ((ui.cacheGeneration.get(requestedDevice) || 0) !== generation || requestedDevice !== deviceId() || source !== "photos" || ui.view !== "photos" || !online() || ui.autoLoaded.has(requestedDevice)) return;
    if (state.scanning || (currentCatalogue().length && !metadataScanNeeded.has(requestedDevice))) return;
    ui.autoLoaded.add(requestedDevice); metadataScanNeeded.delete(requestedDevice);
    await action("scan", { deviceId: requestedDevice, source: "photos" }, (result) => {
      if ((ui.cacheGeneration.get(requestedDevice) || 0) !== generation) return;
      if (Array.isArray(result?.items)) onEvent({ event: "catalogue", data: { deviceId: requestedDevice, source: "photos", items: result.items } });
    });
  }

  async function openFolder(item) {
    if (item.kind !== "folder" || ui.view !== "apps") return;
    await navigateDirectory(ui.appId, item.path);
  }

  async function navigateDirectory(appId = "", path = "/Documents", recordHistory = true) {
    const before = recordHistory ? historySnapshot() : null;
    if (ui.view !== "apps") setView("apps", false);
    if (ui.appId === appId && ui.path === path) return;
    rememberScroll(); ui.navigationGeneration++;
    if (recordHistory) { const history = ui.history.get(deviceId()) || []; history.push({ appId: ui.appId, path: ui.path }); ui.history.set(deviceId(), history.slice(-100)); }
    ui.appId = appId; ui.path = path; ui.search = ""; $("#search-input").value = ""; ui.filter = "all";
    $$(".filter-tab").forEach((button) => button.classList.toggle("active", button.dataset.filter === "all"));
    rememberRoute(); clearSelection(); ui.fileSignature = ""; renderApps(); renderAppBreadcrumbs(); renderCatalogue(true); $("#catalogue-scroll").scrollTop = ui.scroll.get(catalogueKey()) || 0;
    if (before) recordViewChange("folder navigation", before);
    if (!appId) return;
    await hydrateCatalogue();
    if (online()) await scan();
  }

  async function download(items = selectedItems(), destination = state.downloadDir, sync = true) {
    const files = sync ? pendingDownloads(items) : items.filter(item => item.kind !== "folder");
    if (ui.downloadBusy || !files.length || !online()) return;
    const generation=ui.enqueueGeneration,requestedDevice=deviceId();
    ui.downloadBusy = true; renderSelection(); renderInspector();renderJobs();
    try {
      // Paint the busy state before serializing or validating a large selection.
      await new Promise(resolve => { requestAnimationFrame(resolve); setTimeout(resolve,50); });
      if(generation!==ui.enqueueGeneration)return;
      const result = await request("download", { deviceId: requestedDevice, items: files.map(item=>({id:item.id})), sync, ...(destination ? { destination } : {}) });
      if (result?.jobs) state.jobs = result.jobs;
      else if (Array.isArray(result)) state.jobs = result;
      else if (result?.id && !state.jobs.some(job => job.id === result.id)) state.jobs.push(result);
      renderJobs();
      if (result?.cancelled) toast("Download preparation stopped.");
      else if (result?.skipped) toast(`${result.skipped} ${result.skipped === 1 ? "original is" : "originals are"} already in the default download folder. No duplicate downloaded.`);
      else if (result?.inProgressJobIds?.length && !result?.queued) toast("These originals are already in the transfer queue.");
      else toast("Download queued. You can keep browsing.");
      action("state", {}, mergeState);
    } catch(error) { toast(error.message || String(error),true); }
    finally { ui.downloadBusy=false; renderSelection(); renderInspector();renderJobs(); }
  }

  async function activateOriginal(item) {
    if (!item || item.kind === "folder" || ui.downloadBusy || ui.activationBusy) return;
    const generation=ui.enqueueGeneration;ui.activationBusy=true;renderSelection();renderJobs();
    try {
      const resolved = await request("resolve_transfer_items", { deviceId: item.deviceId || deviceId(), items: [{ id: item.id }] });
      if(generation!==ui.enqueueGeneration)return;
      const originals = Array.isArray(resolved) ? resolved : resolved.items || [];
      if (originals.length && originals.every(original => original.localPath)) {
        try { await request("open_path", { path: originals[0].localPath }); return; }
        catch { /* A file removed between validation and opening falls back to USB. */ }
      }
      const snapshot = await request("state"); mergeState(snapshot);
      if(generation!==ui.enqueueGeneration)return;
      const current = selectedItems().find(file => file.id === item.id) || currentCatalogue().find(file=>file.id===item.id) || item;
      // Force the authoritative backend duplicate check after a stale PC badge.
      ui.activationBusy=false;
      await download([{...current,localPath:null,livePhoto:current.livePhoto?{...current.livePhoto,video:{...current.livePhoto.video,localPath:null}}:undefined}]);
    } catch(error) { toast(error.message || String(error),true); }
    finally{ui.activationBusy=false;renderSelection();renderJobs();}
  }

  async function preview(item) {
    if (!item || !["photo", "video"].includes(item.kind)) return;
    if (item.kind === "video") return streamVideo(item, false, 0, !item.localPath, { resume: true });
    ui.playerSeek = null; ui.playerScrubbing = false;
    capturePlayerState();releasePreview(); const key = keyOf(item), token = ++ui.previewToken; ui.previewKey = key; ui.previewPending = true; ui.preview = null; ui.previewStage = { phase: "loading", label: item.kind === "photo" ? "Preparing photo preview…" : "Opening downloaded video…" }; renderInspector();
    const result = await action("preview", { item });
    if (ui.previewKey !== key || token !== ui.previewToken) { if (result?.url && ["original", "compatible"].includes(result.mode)) request("stop_stream", { url: result.url }).catch(() => {}); return; }
    ui.previewPending = false;
    ui.previewStage = { phase: result?.url ? "loading" : "error", label: result?.url ? "Loading preview…" : "Preview unavailable" };
    ui.preview = result ? { ...result, message: result.message || (!result.url ? "Download this original to your PC first, then open its local preview." : "") } : { message: "The preview could not be loaded. Try opening the downloaded file in File Explorer." };
    if (result?.url && item.kind === "photo") {
      applyThumbnail(item, result.url);
    }
    renderInspector();
  }

  async function automaticPhotoPreview(item) {
    const attempt = thumbnailKey(item);
    if (ui.previewPending || ui.photoPreviewAttempts.has(attempt) || (!online() && !item.localPath)) return;
    ui.photoPreviewAttempts.add(attempt);
    const key = keyOf(item), token = ++ui.previewToken;
    ui.previewKey = key; ui.previewPending = true; ui.previewStage = { phase: "loading", label: "Loading photo preview…" }; renderInspector();
    let result;
    try {
      result = await getThumbnail(item);
      if (!result?.url) result = await request("preview", { item });
    } catch (error) { result = { message: error.message || "A preview is unavailable for this image." }; }
    if (ui.previewKey !== key || token !== ui.previewToken) return;
    ui.previewPending = false; ui.preview = result; ui.previewStage = { phase: result?.url ? "loading" : "error", label: result?.url ? "Loading photo preview…" : "Preview unavailable" };
    if (result?.url) applyThumbnail(item, result.url);
    renderInspector();
  }

  async function streamVideo(item, compatible = false, start = 0, fromIphone = !item?.localPath, options = {}) {
    if (!item || item.kind !== "video") return;
    const resume = options.resume ?? playerResumeIntent(), video = targetVideo(), key = keyOf(item);
    capturePlayerState(); releasePreview(); const token = ++ui.previewToken;
    const retainPlayer = video && ui.previewKey === key;
    if (video) video.pause();
    ui.playerPaused = !resume;
    ui.playerSeek = { token, target: compatible ? Math.max(0, Number(start) || 0) : 0, resume, native: false, phase: "requesting" };
    ui.previewKey = key; ui.previewPending = true; if (!retainPlayer) ui.preview = null;
    ui.previewStage = { phase: "loading", label: options.seek ? "Seeking to selected position…" : compatible ? "Preparing compatible playback…" : "Connecting to video preview…" }; renderInspector();
    const localPlayback = Boolean(item.localPath && !fromIphone), localCompatible = localPlayback && compatible;
    let result;
    try { result = await request(localCompatible ? "preview" : "stream", { item, compatible, start: compatible ? start : 0, ...(!localCompatible ? { preferLocal: localPlayback } : {}) }); }
    catch (error) {
      if (ui.previewKey !== key || token !== ui.previewToken) return;
      ui.previewPending = false; ui.playerSeek = null;
      ui.previewStage = { phase: "error", label: "Preview unavailable" }; toast(error.message || String(error), true); renderInspector(); return;
    }
    if (ui.previewKey !== key || token !== ui.previewToken) { if (result?.url) request("stop_stream", { url: result.url }).catch(() => {}); return; }
    ui.previewPending = false;
    ui.previewStage = { phase: result?.url ? "loading" : "error", label: result?.url ? compatible ? "Buffering compatible preview…" : "Buffering original preview…" : "Preview unavailable" };
    ui.playerSeek.phase = "loading";
    ui.preview = result ? { ...result, start: compatible ? number(result.start) ?? start : 0, streaming: typeof result.streaming === "boolean" ? result.streaming : typeof result.downloaded === "boolean" ? !result.downloaded : !localPlayback, compatibleAttempted: compatible, message: result.message || (compatible ? "Compatible preview. The position control follows the original video; downloaded originals stay unchanged." : result.downloaded ? "Playback reads your downloaded original on this PC. Unsupported formats use a compatible preview." : "Playback reads the original over USB. Unsupported formats use a compatible preview.") } : { message: "The preview could not start. Check the connection or download the original." };
    renderInspector();
  }

  async function downloadToFolder() {
    const items = selectedItems().filter((item) => item.kind !== "folder"); if (!items.length || !online()) return;
    const result = await action("pick_folder"), path = typeof result === "string" ? result : result?.path || result?.filePaths?.[0];
    if (path) await download(items, path, false);
  }

  function dialogChoice(selector, acceptedValue) {
    const dialog = $(selector);
    if (document.querySelector("dialog[open]")) throw new Error("Finish the open dialog before starting another action.");
    dialog.returnValue = "cancel";
    return new Promise(resolve => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === acceptedValue), { once: true });
      dialog.showModal();
    });
  }

  async function chooseFilesHome(requestedDevice, forceChoice = false) {
    let home = await request("files_home", { deviceId: requestedDevice });
    if (home.requiresChoice || forceChoice) {
      const providers = (home.apps || state.apps).slice().sort((a, b) => Number(b.filesVisible !== false) - Number(a.filesVisible !== false) || (a.name || a.id).localeCompare(b.name || b.id));
      if (!providers.length) throw new Error(home.message || "No USB-accessible Files app folders were found. Install an app that supports USB File Sharing on your device.");
      text("#files-home-message", home.message || "Choose an app folder to hold EyeCopy uploads. iOS does not expose the On My iPhone root or Safari Downloads over USB.");
      $("#files-home-provider").innerHTML = providers.map(app => `<option value="${escape(app.id)}"${app.id === home.appId ? " selected" : ""}>${app.filesVisible === false ? "USB app documents" : "Files App"} / ${escape(app.name || app.id)} / EyeCopy</option>`).join("");
      if (!(await dialogChoice("#files-home-dialog", "save"))) return null;
      if (requestedDevice !== deviceId()) throw new Error("The selected device changed. Choose its upload folder again.");
      home = await request("configure_files_home", { deviceId: requestedDevice, appId: $("#files-home-provider").value });
    }
    if (!home.appId || !home.path) throw new Error(home.message || "Choose a USB-accessible folder for this device first.");
    return home;
  }

  async function refreshDirectory(target) {
    const result = await action("scan", { deviceId: target.deviceId, source: "app", appId: target.appId, path: target.path });
    if (Array.isArray(result?.items)) onEvent({ event: "catalogue", data: { ...target, source: "app", items: result.items } });
  }

  function checkUploadCompletions() {
    const completed = new Map();
    for (const job of state.jobs) {
      const target = ui.uploadTargets.get(String(job.id));
      if (!target || !["completed", "failed", "cancelled"].includes(job.state)) continue;
      ui.uploadTargets.delete(String(job.id));
      if (job.state === "completed") completed.set(`${target.deviceId}|${target.appId}|${target.path}`, target);
      else toast(`${job.name || "Upload"}: ${job.error || (job.state === "cancelled" ? "cancelled" : "upload failed. See the transfer queue for details.")}`, true);
    }
    completed.forEach(target => { refreshDirectory(target); });
  }

  async function upload(localPaths, options = {}) {
    if (!online()) { toast("Reconnect your device before uploading files."); return; }
    if (ui.uploadBusy) return;
    const requestedDevice = deviceId(), navigationGeneration = ui.navigationGeneration, generation=ui.enqueueGeneration;
    ui.uploadBusy = true; renderConnection();renderJobs();
    try {
      let paths = localPaths;
      if (!paths?.length) {
        const result = await request("pick_upload_files");
        paths = Array.isArray(result) ? result : result?.paths || result?.filePaths || [];
      }
      if (!paths.length) return;
      if(generation!==ui.enqueueGeneration)return;
      if (requestedDevice !== deviceId() || navigationGeneration !== ui.navigationGeneration) throw new Error("The workspace changed while choosing files. Upload again in the intended device folder.");
      let target = options.target || (ui.view === "apps" && ui.appId && !options.useHome ? { appId: ui.appId, path: ui.path } : null);
      if (!target) {
        const home = await chooseFilesHome(requestedDevice);
        if (!home) return;
        const created = await request("ensure_files_home", { deviceId: requestedDevice });
        target = { appId: created.appId || home.appId, path: created.path || home.path };
      }
      if (requestedDevice !== deviceId() || !online()) throw new Error("Reconnect the selected device before uploading.");
      if(generation!==ui.enqueueGeneration)return;
      target = { ...target, deviceId: requestedDevice };
      const priorJobIds = new Set(state.jobs.map(job => String(job.id)));
      const result = await request("upload", { ...target, localPaths: paths });
      if(result?.cancelled){toast("Upload preparation stopped.");return;}
      if (result?.jobs) state.jobs = result.jobs;
      else if (Array.isArray(result)) state.jobs = result;
      else if (result?.id && !state.jobs.some((job) => job.id === result.id)) state.jobs.push(result);
      const ids = result?.jobIds || (result?.id ? [result.id] : state.jobs.filter(job => !priorJobIds.has(String(job.id))).map(job => job.id));
      ids.forEach(id => ui.uploadTargets.set(String(id), target));
      if (options.useHome) { setView("apps"); await navigateDirectory(target.appId, target.path); }
      renderJobs(); checkUploadCompletions();
      toast(`Uploading ${paths.length} ${paths.length === 1 ? "file" : "files"} to ${target.path.split("/").pop() || "device"}. Keep browsing; this folder refreshes when copying finishes.`);
      action("state", {}, mergeState);
    } catch (error) { toast(error.message || String(error), true); }
    finally { ui.uploadBusy = false; renderConnection();renderJobs(); }
  }

  async function createFolder() {
    if (!online() || ui.operationBusy) return;
    const requestedDevice = deviceId();
    ui.operationBusy = true; renderConnection();
    try {
      let target = ui.appId ? { appId: ui.appId, path: ui.path } : null;
      if (!target) {
        const home = await chooseFilesHome(requestedDevice); if (!home) return;
        const created = await request("ensure_files_home", { deviceId: requestedDevice });
        target = { appId: created.appId || home.appId, path: created.path || home.path };
      }
      text("#new-folder-location", `${state.apps.find(app => app.id === target.appId)?.name || "device"} · ${target.path}`);
      $("#new-folder-name").value = "";
      const choice = dialogChoice("#new-folder-dialog", "create"); $("#new-folder-name").focus();
      if (!(await choice)) return;
      const name = $("#new-folder-name").value.trim();
      if (!name) throw new Error("Enter a folder name.");
      if (requestedDevice !== deviceId() || !online()) throw new Error("Reconnect the selected device before creating folders.");
      await request("create_folder", { deviceId: requestedDevice, ...target, name });
      if (ui.appId !== target.appId || ui.path !== target.path) await navigateDirectory(target.appId, target.path);
      await refreshDirectory({ deviceId: requestedDevice, ...target });
      toast(`Folder “${name}” created on your device.`);
    } catch (error) { toast(error.message || String(error), true); }
    finally { ui.operationBusy = false; renderConnection(); }
  }

  async function confirmDeletion(plan, title, label, location, extraWarning) {
    text("#delete-title", title); text("#delete-warning", `${plan.warning || ""}${extraWarning ? ` ${extraWarning}` : ""}`);
    text("#delete-location", location || "");
    const fileCount = Number(plan.fileCount || 0), folderCount = Number(plan.folderCount || 0);
    text("#delete-count", `${folderCount ? `${folderCount.toLocaleString()} ${folderCount === 1 ? "folder" : "folders"} · ` : ""}${fileCount.toLocaleString()} ${fileCount === 1 ? "file" : "files"} · ${formatBytes(plan.bytes)}`);
    $("#delete-file-list").innerHTML = (plan.files || []).slice(0, 100).map(file => `<li>${icon("file")}<span>${escape(file.name)}</span></li>`).join("") + (plan.fileCount > 100 ? `<li>+ ${plan.fileCount - 100} more files</li>` : "");
    text("#delete-confirm", label);
    return dialogChoice("#delete-dialog", "delete");
  }

  async function deletePhoneSelection() {
    const items = selectedItems();
    if (!items.length || !items.every(item => item.source === "app") || !online() || ui.operationBusy) return;
    const hasFolders = items.some(item => item.kind === "folder"), previewCommand = hasFolders ? "preview_phone_folder_delete" : "preview_phone_delete", confirmCommand = hasFolders ? "confirm_phone_folder_delete" : "confirm_phone_delete";
    ui.operationBusy = true; renderInspector(); renderHistory();
    try {
      const plan = await request(previewCommand, { deviceId: deviceId(), items });
      if (!(await confirmDeletion(plan, hasFolders ? "Delete device folders and contents?" : "Delete originals from device?", hasFolders ? "Permanently delete folders" : "Permanently delete from device", nameOf(device()), `${hasFolders ? "These folders and everything inside them" : "These selected files"} will be permanently deleted. Recently Deleted and Undo are unavailable for this operation. Downloaded PC copies remain unchanged.`))) return;
      const result = await request(confirmCommand, { token: plan.token });
      const removed = new Set((result.results || []).filter(file => file.deleted).map(file => file.id));
      const removedFolders = (result.results || []).filter(file => file.deleted && file.path).map(file => ({ ...file, deviceId: file.deviceId || deviceId(), appId: file.appId || items.find(item => item.path === file.path)?.appId }));
      const remains = item => !removed.has(item.id) && !removedFolders.some(folder => item.source === "app" && (item.deviceId || deviceId()) === folder.deviceId && item.appId === folder.appId && (item.path === folder.path || item.path?.startsWith(folder.path + "/")));
      for (const [key, catalogue] of ui.catalogues) ui.catalogues.set(key, catalogue.filter(remains));
      state.catalogue = state.catalogue.filter(remains);
      removed.forEach(id => ui.selected.delete(String(id)));
      renderCatalogue(true); renderInspector();
      toast(`${result.deleted || 0} ${hasFolders ? "items" : result.deleted === 1 ? "file" : "files"} permanently deleted. PC copies remain.${result.failed ? ` ${result.failed} could not be deleted; see the remaining items.` : ""}`, Boolean(result.failed));
      mergeSaveWarning(result);
      action("state", {}, mergeState);
    } catch (error) { toast(error.message || String(error), true); }
    finally { ui.operationBusy = false; renderInspector(); renderHistory(); }
  }

  async function cleanupDeviceDownloads(requestedDevice) {
    if (ui.operationBusy) return;
    ui.operationBusy = true; renderHistory();
    try {
      const saved = state.devices.find(item => item.id === requestedDevice);
      const plan = await request("preview_pc_cleanup", { deviceId: requestedDevice });
      if (!plan.fileCount) { toast("No downloaded originals were found in this device’s default PC folder."); return; }
      if (!(await confirmDeletion(plan, "Delete downloaded PC copies?", plan.permanent ? "Delete PC copies permanently" : "Move PC copies to Recycle Bin", plan.path, `The originals on ${nameOf(saved)} remain unchanged. Only EyeCopy downloads in this default folder are included; other files and custom destinations are kept.`))) return;
      const result = await request("confirm_pc_cleanup", { token: plan.token });
      if (result.undoToken) addHistory({ type: "pc-cleanup", label: `${nameOf(saved)} PC cleanup`, token: result.undoToken });
      const deletedPaths = new Set((result.results || []).filter(file => file.deleted).map(file => file.path));
      for (const catalogue of ui.catalogues.values()) for (const item of catalogue) {
        if (deletedPaths.has(item.localPath)) delete item.localPath;
        if (item.livePhoto?.video && deletedPaths.has(item.livePhoto.video.localPath)) delete item.livePhoto.video.localPath;
      }
      for (const item of state.catalogue) {
        if (deletedPaths.has(item.localPath)) delete item.localPath;
        if (item.livePhoto?.video && deletedPaths.has(item.livePhoto.video.localPath)) delete item.livePhoto.video.localPath;
      }
      renderCatalogue(true); renderInspector();
      toast(`${result.deleted || 0} ${result.deleted === 1 ? "PC copy" : "PC copies"} removed.${result.failed ? ` ${result.failed} could not be removed.` : ""} Device originals remain unchanged.`, Boolean(result.failed));
      if (result.receiptWarning) toast(result.receiptWarning, true);
      mergeSaveWarning(result);
      action("state", {}, mergeState);
    } catch (error) { toast(error.message || String(error), true); }
    finally { ui.operationBusy = false; renderHistory(); }
  }

  async function deletePcSelection() {
    const items=selectedItems().filter(assetDownloaded); if(!items.length||ui.operationBusy) return;
    ui.operationBusy=true;renderInspector();renderHistory();
    try {
      const plan=await request("preview_pc_delete",{deviceId:deviceId(),items:items.map(item=>({id:item.id}))});
      if(!plan.fileCount){toast("These PC copies are no longer available in the default download folder.");mergeState(await request("state"));return;}
      if(!(await confirmDeletion(plan,"Delete selected PC copies?",plan.permanent?"Delete PC copies permanently":"Move PC copies to Recycle Bin",plan.path))) return;
      const result=await request("confirm_pc_delete",{token:plan.token});
      if(result.undoToken)addHistory({type:"pc-cleanup",label:"selected PC downloads",token:result.undoToken});
      const paths=new Set((result.results||[]).filter(file=>file.deleted).map(file=>file.path));
      const forget=item=>{if(paths.has(item.localPath))delete item.localPath;if(item.livePhoto?.video&&paths.has(item.livePhoto.video.localPath))delete item.livePhoto.video.localPath;};
      state.catalogue.forEach(forget);for(const catalogue of ui.catalogues.values())catalogue.forEach(forget);
      mergeSaveWarning(result);renderCatalogue(true);renderInspector();
      toast(`${result.deleted||0} PC ${result.deleted===1?"copy":"copies"} removed.${result.failed?` ${result.failed} could not be removed.`:""} Device originals remain unchanged.`,Boolean(result.failed));
      if(result.receiptWarning)toast(result.receiptWarning,true);action("state",{},mergeState);
    } catch(error){toast(error.message||String(error),true);}
    finally{ui.operationBusy=false;renderInspector();renderHistory();}
  }

  async function setDefaultFolder() {
    if (ui.operationBusy || ui.historyBusy) return;
    const original = state.defaultBaseDir || (state.downloadRoot || state.rootPath || "").replace(/[\\/][^\\/]+[\\/]?$/, "");
    $("#default-folder-path").value = original;
    $("#save-default-folder").disabled = !original;
    if (!(await dialogChoice("#default-folder-dialog", "save"))) return;
    const path = $("#default-folder-path").value.trim(); if (!path) return;
    ui.operationBusy = true; renderConnection();
    try {
      const result = await action("set_default_download_dir", { path });
      if (!result) return;
      if (result.changed) addHistory({ type: "destination", label: "default folder for all devices", historyId: result.historyId, permanent: true });
      if (result?.devices) mergeState(result); else await action("state", {}, mergeState);
      toast("Default folder saved for all devices. Existing downloads stay where they are.");
    } finally { ui.operationBusy = false; renderConnection(); }
  }

  function bindEvents() {
    $$('[data-close-dialog]').forEach(button => button.addEventListener("click", () => button.closest("dialog").close("cancel")));
    $("#undo-action").addEventListener("click", () => runHistory("undo"));
    $("#redo-action").addEventListener("click", () => runHistory("redo"));
    $("#settings-button").addEventListener("click",openSettings);
    $("#settings-theme").addEventListener("change",event=>saveAppSettings({theme:event.target.value}));
    $("#settings-background").addEventListener("change",event=>saveAppSettings({stayInBackground:event.target.checked}));
    $("#save-update-repository").addEventListener("click", () => saveAppSettings({updateRepository: $("#settings-repository").value}));
    $("#settings-auto-updates").addEventListener("change", event => saveAppSettings({autoCheckUpdates:event.target.checked}));
    $("#check-updates").addEventListener("click",checkUpdates);
    $("#install-update").addEventListener("click",installUpdate);
    $("#cancel-update").addEventListener("click",async () => { const value = await action("cancel_update"); if (value?.status) { updateStatus = value; renderUpdateStatus(); } });
    $("#change-default-folder").addEventListener("click",async()=>{
      const returnToSettings=$("#settings-dialog").open;
      if(returnToSettings)$("#settings-dialog").close("folder");
      await setDefaultFolder();
      if(returnToSettings&&!document.querySelector("dialog[open]"))await openSettings();
    });
    $("#choose-default-folder").addEventListener("click", async () => {
      const result = await action("pick_folder"), path = typeof result === "string" ? result : result?.path || result?.filePaths?.[0];
      if (!path || !$("#default-folder-dialog").open) return;
      $("#default-folder-path").value = path; $("#save-default-folder").disabled = false;
    });
    $("#retry-state-save").addEventListener("click", async () => {
      if (ui.saveWarningBusy) return;
      ui.saveWarningBusy = true; renderSaveWarning();
      try { await action("state", {}, mergeState); }
      finally { ui.saveWarningBusy = false; renderSaveWarning(); }
    });
    ui.thumbObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting || !["photos", "apps"].includes(ui.view)) continue;
        const card = entry.target, item = ui.visible.find((file) => keyOf(file) === card.dataset.key);
        ui.thumbObserver.unobserve(card);
        if (item) ensureVideoMetadata(item);
        if (!item || !["photo", "video"].includes(item.kind)) continue;
        const key = thumbnailKey(item);
        if (thumbnailOf(item) || ui.thumbAttempted.has(key) || ui.thumbPending.has(key) || ui.thumbQueue.some((task) => task.key === key)) continue;
        ui.thumbQueue.push({ item, key, deviceId: item.deviceId || deviceId(), generation: ui.cacheGeneration.get(item.deviceId || deviceId()) || 0 });
      }
      pumpThumbnails();
    }, { root: $("#catalogue-scroll"), rootMargin: "100px", threshold: 0.01 });
    document.addEventListener("click", (event) => { const target = event.target.closest("[data-view]"); if (target) setView(target.dataset.view); });
    $("#device-list").addEventListener("click", async (event) => {
      const edit = event.target.closest("[data-device-edit]");
      if (edit) {
        const saved = state.devices.find(item => item.id === edit.dataset.deviceEdit); if (!saved) return;
        ui.renameDeviceId = saved.id; $("#rename-input").value = nameOf(saved); $("#rename-dialog").showModal(); $("#rename-input").focus(); $("#rename-input").select(); return;
      }
      const folder = event.target.closest("[data-device-folder]");
      if (folder) { const saved = state.devices.find(item => item.id === folder.dataset.deviceFolder); if (saved?.defaultDownloadDir) action("open_path", { path: saved.defaultDownloadDir }); else toast("Connect this device once to assign its downloads folder."); return; }
      const cleanup = event.target.closest("[data-device-cleanup]");
      if (cleanup) { cleanupDeviceDownloads(cleanup.dataset.deviceCleanup); return; }
      const button = event.target.closest("[data-device]");
      if (!button || button.dataset.device === deviceId()) return;
      const before = historySnapshot();
      rememberScroll(); ui.navigationGeneration++; const result = await action("select_device", { id: button.dataset.device }); if (result === null) return;
      state.selectedDevice = button.dataset.device; state.apps = []; restoreRoute(deviceId()); clearSelection(); ui.fileSignature = "";
      if (result?.devices) mergeState(result); else { renderDevices(); renderConnection(); renderApps(); renderCatalogue(true); action("state", {}, mergeState); }
      $("#catalogue-scroll").scrollTop = ui.scroll.get(catalogueKey()) || 0; hydrateCatalogue();
      recordViewChange("device navigation", before);
    });
    $("#search-input").addEventListener("input", (event) => { ui.search = event.target.value; scheduleCatalogue(); });
    $("#show-hidden-files").addEventListener("change",toggleHiddenFiles);
    $("#queue-sort").addEventListener("change",event=>{ui.queueSort=event.target.value;renderJobs();});
    $("#hide-completed").addEventListener("change",event=>{ui.hideCompleted=event.target.checked;renderJobs();});
    $("#queue-batch-action").addEventListener("click",async()=>{if(ui.queueBusy)return;const stop=activeJobs().length>0||ui.downloadBusy||ui.uploadBusy||ui.activationBusy;ui.enqueueGeneration++;ui.queueBusy=true;renderJobs();try{const result=await request(stop?"cancel_all_jobs":"clear_all_jobs");if(result?.jobs)state.jobs=result.jobs;if(result?.retained)toast(`${result.retained} entries are waiting for transfer cleanup. Try Clear all again when stopping finishes.`,true);}catch(error){toast(error.message||String(error),true);}finally{ui.queueBusy=false;renderJobs();}});
    $("#sort-select").addEventListener("change", (event) => { ui.sort = event.target.value; renderCatalogue(); });
    $$(".filter-tab").forEach((button) => button.addEventListener("click", () => { ui.filter = button.dataset.filter; $$(".filter-tab").forEach((entry) => entry.classList.toggle("active", entry === button)); renderCatalogue(); }));
    ["grid", "list"].forEach((layout) => $(`#${layout}-view-button`).addEventListener("click", () => { ui.layout = layout; ["grid", "list"].forEach((entry) => $(`#${entry}-view-button`).classList.toggle("active", entry === layout)); hidden(".tile-size-control", layout !== "grid"); renderCatalogue(); }));
    $("#tile-size").addEventListener("input", (event) => document.documentElement.style.setProperty("--tile-size", `${event.target.value}px`));
    $("#refresh-library").addEventListener("click", scan); $("#list-apps-button").addEventListener("click", loadApps);
    ["#app-directory-grid", "#app-directory-list"].forEach((selector) => $(selector).addEventListener("click", (event) => { const button = event.target.closest("[data-app-id]"); if (button) navigateDirectory(button.dataset.appId); }));
    $("#app-breadcrumbs").addEventListener("click", (event) => { const root = event.target.closest("[data-app-root]"); if (root) navigateDirectory(); else { const button = event.target.closest("[data-app-path]"); if (button) navigateDirectory(ui.appId, button.dataset.appPath); } });
    $("#app-up").addEventListener("click", () => ui.path === "/Documents" ? navigateDirectory() : navigateDirectory(ui.appId, ui.path.slice(0, ui.path.lastIndexOf("/")) || "/Documents"));
    $("#app-back").addEventListener("click", () => { const route = (ui.history.get(deviceId()) || []).pop(); if (route) navigateDirectory(route.appId, route.path, false); });
    $("#catalogue").addEventListener("click", (event) => { const card = event.target.closest("[data-key]"); const item = card && ui.visible.find((entry) => keyOf(entry) === card.dataset.key); if (item) selectItem(item, event); });
    $("#catalogue").addEventListener("dblclick", (event) => { const card = event.target.closest("[data-key]"); const item = card && ui.visible.find((entry) => keyOf(entry) === card.dataset.key); if (item?.kind === "folder") openFolder(item); else if (item) { if(event.target.closest(".file-preview"))activateOriginal(item);else preview(item); } });
    $("#catalogue").addEventListener("keydown", (event) => { if (!["Enter", " "].includes(event.key)) return; const card = event.target.closest("[data-key]"); const item = card && ui.visible.find((entry) => keyOf(entry) === card.dataset.key); if (!item) return; event.preventDefault(); if (event.key === "Enter" && item.kind === "folder") openFolder(item); else selectItem(item, event); });
    $("#catalogue").addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      const card = event.target.closest("[data-key]"), item = card && ui.visible.find((entry) => keyOf(entry) === card.dataset.key);
      if (item) prepareNativeDrag(dragFilesFor(item));
    });
    $("#catalogue").addEventListener("dragstart", (event) => {
      const card = event.target.closest("[data-key]"); const item = card && ui.visible.find((entry) => keyOf(entry) === card.dataset.key);
      if (!item) { event.preventDefault(); return; }
      startNativeDrag(event, dragFilesFor(item));
    });
    $("#clear-selection").addEventListener("click", () => { const before = historySnapshot(); clearSelection(); recordViewChange("selection", before); }); $("#download-selected").addEventListener("click", () => {
      if (selectedItems().some((item) => item.kind !== "folder")) download();
      else { const before = historySnapshot(); ui.visible.forEach((item) => ui.selected.add(keyOf(item))); renderSelection(); renderInspector(); recordViewChange("selection", before); }
    });
    $("#inspector-content").addEventListener("dblclick", event => { if (event.target.closest(".inspector-preview") && !event.target.closest("video,button,input,form")) { event.preventDefault(); activateOriginal(selectedItems()[0]); } });
    $("#inspector-content").addEventListener("keydown", event => {
      const video = event.target.closest("video") || (event.target.id === "video-position" && [" ", "f", "F", "m", "M"].includes(event.key) ? targetVideo() : null); if (!video) return;
      if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.preventDefault(); event.stopPropagation();
        const position = ui.playerSeek && ui.playerSeek.phase !== "ready" ? ui.playerSeek.target : playerPosition(video);
        seekPlayer(position + (event.key === "ArrowRight" ? 5 : -5));
      } else if (["ArrowUp", "ArrowDown"].includes(event.key)) {
        event.preventDefault(); event.stopPropagation(); video.volume = Math.max(0, Math.min(1, video.volume + (event.key === "ArrowUp" ? .05 : -.05)));
      } else if (event.key.toLowerCase() === "m") {
        event.preventDefault(); event.stopPropagation(); video.muted = !video.muted;
      } else if (event.key === " ") {
        event.preventDefault(); event.stopPropagation();
        if (ui.playerSeek && ui.playerSeek.phase !== "ready") { ui.playerSeek.resume = !ui.playerSeek.resume; ui.playerPaused = !ui.playerSeek.resume; }
        else if (video.paused) video.play().catch(() => {}); else video.pause();
      } else if (event.key.toLowerCase() === "f") { event.preventDefault(); event.stopPropagation(); togglePlayerFullscreen(); }
    }, true);
    $("#inspector-content").addEventListener("click", (event) => {
      const button = event.target.closest("[data-inspector-action]"); const item = selectedItems()[0]; if (!button || !item) return;
      switch (button.dataset.inspectorAction) { case "cancel-preview": cancelPreview(); break; case "download": download(); break; case "download-to": downloadToFolder(); break; case "preview": preview(item); break; case "stream": streamVideo(item, false, 0, true); break; case "compatible": streamVideo(item, true, 0, Boolean(ui.preview?.streaming || !item.localPath)); break; case "folder": openFolder(item); break; case "open": activateOriginal(item); break; case "delete-phone": deletePhoneSelection(); break; case "delete-pc": deletePcSelection(); break; case "fullscreen": togglePlayerFullscreen(); break; }
    });
    $("#inspector-content").addEventListener("pointerdown", event => {
      if (event.target.id === "video-position") { ui.playerScrubResume = playerResumeIntent(); ui.playerScrubbing = true; }
      if (event.button === 0 && !event.target.closest("video,button,input,form") && event.target.closest("[data-preview-drag]")) prepareNativeDrag(dragFilesFor());
    });
    $("#inspector-content").addEventListener("dragstart", event => { if (!event.target.closest("video,button,input,form") && event.target.closest("[data-preview-drag]")) startNativeDrag(event, dragFilesFor()); });
    $("#inspector-content").addEventListener("submit", event => { if (event.target.id !== "stream-seek-form") return; event.preventDefault(); seekPlayer(Number($("#stream-start").value) || 0); });
    $("#inspector-content").addEventListener("input", event => {
      if (event.target.id !== "video-position") return;
      if (!ui.playerScrubbing) ui.playerScrubResume = playerResumeIntent();
      ui.playerScrubbing = true; setPlayerTimeline(Number(event.target.value) || 0);
    });
    $("#inspector-content").addEventListener("change", event => { if (event.target.id === "video-position") seekPlayer(Number(event.target.value) || 0); });
    $("#inspector-content").addEventListener("pointercancel", event => { if (event.target.id === "video-position") { ui.playerScrubbing = false; ui.playerScrubResume = null; updatePlayerPosition(); } });
    $("#inspector-content").addEventListener("timeupdate",event=>{if(event.target===targetVideo())updatePlayerPosition();},true);
    $("#inspector-content").addEventListener("volumechange",event=>{if(event.target===targetVideo()){ui.playerVolume=event.target.volume;ui.playerMuted=event.target.muted;}},true);
    $("#inspector-content").addEventListener("error", (event) => {
      if (event.target.tagName === "IMG") { if (selectedItems()[0]?.kind === "photo" && event.target === $("#inspector-content .inspector-preview img") && ui.previewStage) { ui.previewStage = { phase: "error", label: "Image preview unavailable" }; updatePreviewProgress(); } return; }
      if (event.target.tagName !== "VIDEO" || !currentMediaEvent(event.target)) return;
      const target = $("#inspector-content");
      let message = target.querySelector(".preview-message");
      if (!message) { message = document.createElement("p"); message.className = "preview-message"; target.appendChild(message); }
      const item = selectedItems()[0];
      if (item?.kind === "video" && ui.preview?.url && !ui.preview.compatibleAttempted && !ui.previewPending && (item.localPath || online())) {
        message.textContent = "This video uses an unsupported format, such as ProRes. Starting a compatible playback preview…";
        streamVideo(item, true, playerTargetPosition(event.target), Boolean(ui.preview.streaming || !item.localPath)); return;
      }
      message.textContent = "Playback could not continue. Reconnect your device or use Download original and open the file in your video editor.";
      ui.previewStage = { phase: "error", label: "Video preview interrupted" }; updatePreviewProgress();
    }, true);
    $("#inspector-content").addEventListener("load", (event) => { if (event.target.tagName === "IMG" && selectedItems()[0]?.kind === "photo" && event.target === $("#inspector-content .inspector-preview img") && !ui.previewPending && ui.preview?.url && ui.previewStage?.phase === "loading") { ui.previewStage = { phase: "ready", label: "Photo preview ready" }; updatePreviewProgress(); } }, true);
    ["loadstart", "progress", "durationchange", "loadeddata", "canplay", "canplaythrough", "playing", "waiting", "stalled", "seeked"].forEach((type) => $("#inspector-content").addEventListener(type, (event) => {
      const video = event.target; if (video.tagName !== "VIDEO" || !currentMediaEvent(video)) return;
      if (["loadeddata", "canplay", "canplaythrough", "playing", "seeked"].includes(type)) finishPlayerSeek(video);
      if (["loadeddata", "canplay", "canplaythrough", "playing", "seeked"].includes(type) && video.readyState >= 2) ui.previewStage = { phase: "ready", label: "Preview ready to play" };
      else if (type === "waiting" || (type === "stalled" && video.readyState < 3)) ui.previewStage = { phase: "buffering", label: online() || !ui.preview.streaming ? "Buffering video preview…" : "Device disconnected · waiting for video data" };
      updatePreviewProgress(video);
    }, true));
    $("#inspector-content").addEventListener("loadedmetadata", (event) => {
      if (event.target.tagName !== "VIDEO" || !currentMediaEvent(event.target)) return;
      const video = event.target, list = $("#inspector-content .detail-list");
      const item = selectedItems()[0];
      // Chromium can accept a MOV's AAC track while its ProRes video track is
      // unsupported. That is successful audio playback, with no media error,
      // so detect the missing picture when metadata becomes available.
      if (!video.videoWidth && !video.videoHeight && item?.kind === "video" && ui.preview?.url && !ui.preview.compatibleAttempted && ui.preview.mode !== "compatible" && !ui.previewPending && (item.localPath || online())) {
        streamVideo(item, true, playerTargetPosition(video), Boolean(ui.preview.streaming || !item.localPath)); return;
      }
      if (!list) return;
      const row = list.querySelector("[data-video-details]") || document.createElement("div"); row.className = "detail-row"; row.dataset.videoDetails = "true";
      row.innerHTML = `<dt>Preview resolution</dt><dd>${video.videoWidth && video.videoHeight ? `${video.videoWidth} × ${video.videoHeight}` : "Unavailable"}</dd>`;
      if (!row.parentElement) list.appendChild(row);
      if (!ui.preview?.streaming && ui.preview?.mode !== "compatible") ui.previewStage = { phase: "ready", label: "Video metadata ready · press Play" };
      updatePreviewProgress(video);
    }, true);
    $("#job-list").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-job-action]"), card = button?.closest("[data-job]"); if (!card) return;
      const job = state.jobs.find((item) => String(item.id) === card.dataset.job); if (!job) return;
      if (button.dataset.jobAction === "open") { action("open_path", { path: job.localPath }); return; }
      const id = String(job.id); if (ui.jobBusy.has(id) || job.cancelRequested) return;
      ui.jobBusy.add(id); renderJobs();
      try {
        await action(button.dataset.jobAction === "choose-folder" ? "choose_job_folder" : `${button.dataset.jobAction === "start" ? "retry" : button.dataset.jobAction}_job`, { id: job.id }, (result) => { if (result?.jobs) state.jobs = result.jobs; });
      } finally { ui.jobBusy.delete(id); renderJobs(); }
    });
    $("#upload-main").addEventListener("click", () => upload(undefined, { useHome: true }));
    $("#new-folder-button").addEventListener("click", createFolder);
    $("#files-home-button").addEventListener("click", () => chooseFilesHome(deviceId(), true).catch(error => toast(error.message, true)));
    $("#reset-destination").addEventListener("click", () => action("reset_download_dir", {}, result => { state.downloadDir = result.path; if (result.changed) addHistory({ type: "destination", label: "queue destination reset", historyId: result.historyId }); renderConnection(); }));
    $("#open-downloads").addEventListener("click", () => state.downloadDir ? action("open_path", { path: state.downloadDir }) : $("#change-destination").click());
    $("#clear-finished-jobs").addEventListener("click", async () => {
      if (ui.clearingFinished) return;
      ui.clearingFinished = true; renderJobs();
      try { await action("clear_finished_jobs", {}, (result) => { if (Array.isArray(result?.jobs)) state.jobs = result.jobs; }); }
      finally { ui.clearingFinished = false; renderJobs(); }
    });
    $("#change-destination").addEventListener("click", async () => {
      const result = await action("pick_folder"); const path = typeof result === "string" ? result : result?.path || result?.filePaths?.[0]; if (!path) return;
      await action("set_download_dir", { path }, result => { state.downloadDir = path; if (result.changed) addHistory({ type: "destination", label: "queue destination", historyId: result.historyId }); renderConnection(); });
    });
    $("#clear-cache-button").addEventListener("click", () => {
      const requestedDevice = deviceId();
      action("clear_cache", { deviceId: requestedDevice }, () => {
        ui.cacheGeneration.set(requestedDevice, (ui.cacheGeneration.get(requestedDevice) || 0) + 1);
        ui.autoLoaded.add(requestedDevice);
        const prefix = `${requestedDevice}|`; for (const key of ui.catalogues.keys()) if (key.startsWith(prefix)) ui.catalogues.delete(key);
        for (const key of ui.thumbnails.keys()) if (key.startsWith(prefix)) ui.thumbnails.delete(key);
        for (const key of ui.thumbAttempted) if (key.startsWith(prefix)) ui.thumbAttempted.delete(key);
        state.catalogue = state.catalogue.filter((item) => item.deviceId && item.deviceId !== requestedDevice);
        if (requestedDevice === deviceId()) { clearSelection(); ui.fileSignature = ""; renderCatalogue(true); }
        toast("Catalogue cache cleared. Downloaded originals remain in your destination folder.");
      });
    });
    $("#run-diagnostics").addEventListener("click", async () => { $("#run-diagnostics").disabled = true; const result = await action("diagnostics"); if (result) updateUsb(result); $("#run-diagnostics").disabled = false; });
    $("#pair-device").addEventListener("click", async () => { $("#pair-device").disabled = true; toast("Unlock your device and accept Trust on its screen if prompted.", false, 12000); await action("pair", { deviceId: deviceId() }, () => action("state", {}, mergeState)); $("#pair-device").disabled = false; });
    $("#open-apple-devices").addEventListener("click", () => action("launch_apple_devices"));
    ["#guide-button", "#full-guide-button"].forEach((selector) => $(selector).addEventListener("click", () => action("show_guide")));
    $("#empty-action").addEventListener("click", () => {
      if (currentCatalogue().length) { ui.search = ""; ui.filter = "all"; $("#search-input").value = ""; $$(".filter-tab").forEach((button) => button.classList.toggle("active", button.dataset.filter === "all")); renderCatalogue(); }
      else if (ui.view === "apps" && !ui.appId) { if (state.apps.length) { ui.search = ""; $("#search-input").value = ""; renderCatalogue(true); } else loadApps(); } else if (online() && !state.scanning) scan(); else setView("connection");
    });
    $("#save-name-button").addEventListener("click", (event) => {
      const name = $("#rename-input").value.trim(); if (!name) { event.preventDefault(); $("#rename-input").focus(); return; }
      const requestedDevice = ui.renameDeviceId;
      if (!requestedDevice || !state.devices.some(item => item.id === requestedDevice)) { event.preventDefault(); toast("This saved device is no longer available.", true); return; }
      action("rename_device", { id: requestedDevice, name }, (result) => {
        if (result.changed && result.historyId) addHistory({ type: "destination", label: "device name and folder", historyId: result.historyId, deviceId: requestedDevice });
        if (result?.devices) mergeState(result); else { const saved = state.devices.find((item) => item.id === requestedDevice); if (saved) saved.alias = name; renderDevices(); renderConnection(); renderInspector(); }
      });
    });
    $("#scroll-top").addEventListener("click", () => (["photos", "apps"].includes(ui.view) ? $("#catalogue-scroll") : $("main")).scrollTo({ top: 0, behavior: "smooth" }));
    $("#scroll-bottom").addEventListener("click", () => { const node = ["photos", "apps"].includes(ui.view) ? $("#catalogue-scroll") : $("main"); node.scrollTo({ top: node.scrollHeight, behavior: "smooth" }); });
    $("#queue-scroll-top").addEventListener("click", () => $("main").scrollTo({ top: 0, behavior: "smooth" }));
    $("#queue-scroll-bottom").addEventListener("click", () => { const node = $("main"); node.scrollTo({ top: node.scrollHeight, behavior: "smooth" }); });
    $("#catalogue-scroll").addEventListener("scroll", () => ui.scroll.set(catalogueKey(), $("#catalogue-scroll").scrollTop), { passive: true });
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && !event.target.closest("input,textarea,select,[contenteditable],dialog") && ["z", "y"].includes(event.key.toLowerCase())) { event.preventDefault(); runHistory(event.key.toLowerCase() === "y" || event.shiftKey ? "redo" : "undo"); return; }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); if (!["photos", "apps"].includes(ui.view)) setView("photos"); $("#search-input").focus(); }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a" && ["photos", "apps"].includes(ui.view) && !event.target.closest("input,textarea,select,dialog")) { event.preventDefault(); const before = historySnapshot(); ui.visible.forEach((item) => ui.selected.add(keyOf(item))); renderSelection(); renderInspector(); recordViewChange("selection", before); }
      if (event.key === "Escape" && !document.querySelector("dialog[open]")) { const before = historySnapshot(); clearSelection(); recordViewChange("selection", before); }
    });
    function dropTarget(event) {
      const directory = event.target.closest?.("[data-app-id]");
      if (directory) return { appId: directory.dataset.appId, path: "/Documents" };
      const card = event.target.closest?.("[data-key]");
      const item = card && ui.visible.find(file => keyOf(file) === card.dataset.key);
      if (ui.view === "apps" && ui.appId) return { appId: ui.appId, path: item?.kind === "folder" ? item.path : ui.path };
      return null;
    }
    document.addEventListener("dragenter", (event) => { if (!event.dataTransfer?.types.includes("Files")) return; event.preventDefault(); ui.dragDepth++; hidden("#drop-overlay", false); });
    document.addEventListener("dragover", (event) => {
      if (!event.dataTransfer?.types.includes("Files")) return;
      event.preventDefault(); event.dataTransfer.dropEffect = online() ? "copy" : "none";
      const target = dropTarget(event);
      text("#drop-message", !online() ? "Reconnect your device before uploading." : target ? `Upload to ${state.apps.find(app => app.id === target.appId)?.name || "device"} · ${target.path}` : "Upload to this device’s EyeCopy folder. Choose an available Files app location if prompted.");
      $$(".upload-drop-target").forEach(node => node.classList.remove("upload-drop-target"));
      const node = event.target.closest?.("[data-app-id], [data-key]");
      if (node && target && (node.dataset.appId || ui.visible.find(file => keyOf(file) === node.dataset.key)?.kind === "folder")) node.classList.add("upload-drop-target");
    });
    document.addEventListener("dragleave", () => { ui.dragDepth = Math.max(0, ui.dragDepth - 1); if (!ui.dragDepth) hidden("#drop-overlay", true); });
    document.addEventListener("drop", async (event) => {
      event.preventDefault(); ui.dragDepth = 0; hidden("#drop-overlay", true);
      const target = dropTarget(event);
      $$(".upload-drop-target").forEach(node => node.classList.remove("upload-drop-target"));
      const files = Array.from(event.dataTransfer?.files || []); if (!files.length) return;
      let paths = files.map((file) => file.path).filter(Boolean);
      if (bridge?.pathsForFiles) { try { paths = await bridge.pathsForFiles(files); } catch (error) { toast(error.message || "Could not read dropped file paths.", true); return; } }
      if (!paths?.length) { toast("Choose those files in the upload file picker to give EyeCopy access to their paths."); if (online()) upload(undefined, { target, useHome: !target }); return; }
      upload(paths, { target });
    });
    window.addEventListener("blur", () => { ui.dragDepth = 0; hidden("#drop-overlay", true); });
    window.addEventListener("beforeunload", () => { rememberScroll(); releasePreview(); });
  }

  async function initialize() {
    try { const theme=localStorage.getItem("eyecopy.theme");if(themeNames.has(theme))document.documentElement.dataset.theme=theme; } catch { /* Settings also load from the desktop profile. */ }
    bindEvents(); renderDevices(); renderConnection(); renderApps(); renderJobs(); renderCatalogue(true); renderInspector();
    if (!bridge?.request) { state.connection = { status: "error", message: "Open EyeCopy through its desktop launcher to enable device connections and native file transfers.", serviceAvailable: false }; renderConnection(); return; }
    bridge.onEvent?.(onEvent);
    const settingsRequest=loadAppSettings();
    const result = await action("state");
    if (result) mergeState(result.state || result);
    await settingsRequest;
  }
  initialize();
})();
