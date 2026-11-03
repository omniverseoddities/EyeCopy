const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { nativeImage, BrowserWindow } = require('electron');
const { createDragBroker } = require('./native-drag/broker');
const { createForegroundHandoff } = require('./native-drag/foreground');
const { createLocalTicketDrag } = require('./native-drag/local-ticket');
const { MarkerDrop, buildMarkerProbe } = require('./marker-drop');
const { buildDropFolderProbe, resolveDropFolder } = require('../scripts/DropFolderProbe');

function dragIcon() {
  // A transparent file silhouette and copy badge remain legible over both
  // Explorer themes. createFromBitmap consumes BGRA pixels on Windows.
  const width = 64, height = 64, pixels = Buffer.alloc(width * height * 4);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const offset = (y * width + x) * 4;
    let color;
    if (x >= 10 && x <= 44 && y >= 4 && y <= 53 && !(x > 33 && y < x - 29)) color = [245, 245, 248, 255];
    if (x >= 16 && x <= 35 && [25, 26, 33, 34, 41, 42].includes(y)) color = [55, 45, 235, 255];
    if ((x - 46) ** 2 + (y - 48) ** 2 <= 13 ** 2) color = [55, 45, 235, 255];
    if ((x >= 44 && x <= 48 && y >= 40 && y <= 56) || (x >= 38 && x <= 54 && y >= 46 && y <= 50)) color = [255, 255, 255, 255];
    if (color) for (let component = 0; component < 4; component++) pixels[offset + component] = color[component];
  }
  return nativeImage.createFromBitmap(pixels, { width, height });
}
function normalizeItems(payload) {
  const items = typeof payload === 'string' ? [{ localPath: payload, name: path.basename(payload) }] : payload?.items;
  if (!Array.isArray(items) || !items.length || items.length > 100 || items.some(item => !item || item.kind === 'folder')) throw new Error('Select up to 100 files to drag into Explorer. Open folders to select their files.');
  return items;
}
function localFilesAvailable(files) {
  return Array.isArray(files) && files.length > 0 && files.every(file => {
    if (typeof file !== 'string' || !path.isAbsolute(file)) return false;
    try { return fs.statSync(file).isFile(); } catch { return false; }
  });
}
function sourceWindowFor(sender) {
  const handle = BrowserWindow.fromWebContents(sender)?.getNativeWindowHandle();
  if (!Buffer.isBuffer(handle)) return undefined;
  if (handle.length >= 8) return handle.readBigUInt64LE().toString();
  if (handle.length >= 4) return handle.readUInt32LE().toString();
  return undefined;
}
const keyFor = items => JSON.stringify(items.map(item => [item.id || item.localPath, item.deviceId, item.modified, item.size, item.localPath,
  item.livePhoto?.verified ? [item.livePhoto.video?.id, item.livePhoto.video?.modified, item.livePhoto.video?.size, item.livePhoto.video?.localPath] : null]));
function validatedStreamURL(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || !/^\/[A-Za-z0-9_-]{20,128}\/original$/.test(url.pathname) || url.search || url.username || url.password) throw new Error('The phone provided an invalid drag stream.');
  return value;
}

class DragManager {
  constructor({ root, dataDir, request, emit, log, beginTimeoutMs = 4000, stallTimeoutMs = 45000, phoneDragMode = 'marker' }) {
    this.root = root; this.dataDir = dataDir; this.request = request; this.emit = emit; this.log = log;
    this.helper = null; this.ready = false; this.prepared = new Map(); this.inflight = new Map(); this.transfers = new Map(); this.lastEmit = new Map(); this.watchdogs = new Map(); this.icon = dragIcon();
    this.beginTimeoutMs = beginTimeoutMs; this.stallTimeoutMs = stallTimeoutMs;
    this.foreground = createForegroundHandoff();
    this.localTicket = createLocalTicketDrag(); this.dropProbeStartup = null;
    this.phoneDragMode = phoneDragMode; this.marker = null; this.markerStartup = null; this.closed = false;
    this.defaultDirectories = new Map(); this.localAuthority = new Map(); this.authorityRevision = 0;
    this.preparationGeneration = 0; this.pendingCancellations = new Map();
  }
  observeState(state) {
    // Engine state is the authority for default-library copies. Completed queue
    // paths are only result links and must never promote an external download.
    const changedDevices = new Set(); let changed = false;
    for (const device of state?.devices || []) {
      if (!device?.id || typeof device.defaultDownloadDir !== 'string') continue;
      const directory = this.pathKey(device.defaultDownloadDir);
      if (this.defaultDirectories.get(device.id) !== directory) {
        changedDevices.add(device.id); this.defaultDirectories.set(device.id, directory); changed = true;
      }
    }
    const components = [];
    for (const item of state?.catalogue || []) {
      components.push(item);
      if (item?.livePhoto?.verified && item.livePhoto.video) components.push(item.livePhoto.video);
    }
    for (const item of components) {
      if (!item?.id || !item.deviceId) continue;
      const authority = this.authorityKey(item);
      if (this.localAuthority.get(item.id) !== authority) {
        this.localAuthority.set(item.id, authority); changed = true;
      }
    }
    if (!changed) return;
    this.authorityRevision++;
    for (const [key, prepared] of this.prepared) {
      // Keep active drops and phone tickets independent. Only idle preparations
      // which could reuse an old local copy need invalidating.
      if (prepared.claimed || !(prepared.localPaths || prepared.items?.some(item => item.localPath))) continue;
      if (prepared.items?.some(item => changedDevices.has(item.deviceId)) || !this.localAuthorityMatches(prepared.items, prepared.localPaths)) {
        this.prepared.delete(key); this.releasePrepared(prepared);
      }
    }
  }
  pathKey(value) {
    const normalized = path.resolve(value);
    return path.sep === '\\' ? normalized.toLowerCase() : normalized;
  }
  authorityKey(item) {
    return JSON.stringify([item.localPath ? this.pathKey(item.localPath) : null, item.modified, item.size]);
  }
  localAuthorityMatches(items, localPaths = null) {
    return items.every((original, index) => {
      const item = localPaths?.[index] ? { ...original, localPath: localPaths[index] } : original;
      if (!item?.localPath || !item.deviceId || !item.id) return true;
      const directory = this.defaultDirectories.get(item.deviceId);
      const authority = this.localAuthority.get(item.id);
      return (!directory || this.pathKey(path.dirname(item.localPath)) === directory)
        && (!authority || this.authorityKey(item) === authority);
    });
  }
  startHelper() {
    if (this.phoneDragMode === 'marker') {
      this.startMarkerHelper().catch(error => this.log(`Explorer folder detection unavailable: ${error.message}`));
      return;
    }
    if (this.helper) return;
    this.helper = createDragBroker({ dataDir: this.dataDir, onEvent: message => this.onMessage(message), onError: error => {
      this.log(`Native drag unavailable: ${error.message}`); this.ready = false;
      for (const transfer of this.transfers.values()) if (!this.isTerminal(transfer)) this.fail(transfer, 'The Explorer copy helper stopped responding. Check the destination for a partial file, then drag again.');
    } });
    this.helper.ready().then(() => { this.ready = true; }).catch(error => { this.ready = false; this.log(error.message); });
  }
  async startMarkerHelper() {
    if (this.closed) throw new Error('EyeCopy is closing.');
    if (this.markerStartup) return this.markerStartup;
    this.markerStartup = (async () => {
      if (!this.marker) {
        const executable = await buildMarkerProbe(this.dataDir);
        if (this.closed) throw new Error('EyeCopy is closing.');
        this.marker = new MarkerDrop({ dataDir: this.dataDir, sourceDir: path.join(this.dataDir, 'drag-tickets'), executable, onEvent: message => this.onMarkerMessage(message) });
      }
      await this.marker.start();
      this.startDropProbe().catch(error => this.log(`Explorer target check unavailable: ${error.message}`));
      if (this.closed) { this.marker.close(); throw new Error('EyeCopy is closing.'); }
      this.ready = true; return this.marker;
    })();
    try { return await this.markerStartup; }
    catch (error) { this.markerStartup = null; this.ready = false; throw error; }
  }
  startDropProbe() {
    if (!this.dropProbeStartup) this.dropProbeStartup = buildDropFolderProbe(this.dataDir).catch(error => { this.dropProbeStartup = null; throw error; });
    return this.dropProbeStartup;
  }
  async checkDroppedFolder(transfer, point) {
    if (!point || this.isTerminal(transfer) || transfer.handoffPending || this.transfers.get(transfer.id) !== transfer) return;
    try {
      const executable = await this.startDropProbe();
      const result = await resolveDropFolder(executable, point);
      if (this.isTerminal(transfer) || transfer.handoffPending || this.transfers.get(transfer.id) !== transfer) return;
      if (result?.verified && typeof result.directory === 'string' && path.isAbsolute(result.directory)) {
        this.log(`Explorer marker ${transfer.id}: checking exact ticket in the visible drop folder.`);
        // The visible folder is a candidate only. The SDK must find and
        // exclusively consume our exact ticket before any phone transfer.
        this.marker?.candidate({ basename: transfer.markerBasename, destination: result.directory, shellEvent: 0 });
      } else this.log(`Explorer marker ${transfer.id}: target folder was not verified: ${result?.reason || 'No visible local folder matched the release point.'}`);
    } catch (error) { this.log(`Explorer marker ${transfer.id}: target check unavailable: ${error.message}`); }
  }
  releasePrepared(prepared) {
    if (!prepared || prepared.claimed) return Promise.resolve();
    if (prepared.shellTicket) this.localTicket.release(prepared.shellTicket);
    if (prepared.marker) this.marker?.cancel(prepared.marker.id);
    return Promise.allSettled((prepared.files || []).map(file => this.request('stop_stream', { url: file.url })));
  }
  async resolveItems(items) {
    // The backend resolves original identities and Live Photo companions from
    // cached metadata, and reports remembered connection state without phone IO.
    let resolution;
    for (let attempt = 0; attempt < 3; attempt++) {
      const revision = this.authorityRevision;
      resolution = items.every(item => typeof item.id === 'string' && typeof item.deviceId === 'string')
        ? await this.request('resolve_transfer_items', { items }) : { items };
      // A folder change during prewarming must not publish an obsolete local
      // preparation after the state event already invalidated the cache.
      if (revision === this.authorityRevision) break;
      if (attempt === 2) throw new Error('The download library changed while preparing this drag. Select the file again.');
    }
    const resolved = resolution?.items;
    if (!Array.isArray(resolved) || !resolved.length || resolved.length > 100 || resolved.some(item => !item || item.kind === 'folder')) throw new Error('Choose up to 100 original files, including both parts of each Live Photo.');
    if (resolved.some(item => item?.localPath && this.defaultDirectories.has(item.deviceId)
        && this.pathKey(path.dirname(item.localPath)) !== this.defaultDirectories.get(item.deviceId))) {
      throw new Error('The downloaded copy is no longer in this iPhone’s default library. Select the file again to prepare its iPhone transfer.');
    }
    this.observeState({ catalogue: resolved });
    return resolution;
  }
  requirePhoneSource(resolution, supplied) {
    if (resolution.items.some(item => typeof item.id !== 'string' || typeof item.deviceId !== 'string')) throw new Error('The local original is no longer available. Select it in the iPhone library to download it again.');
    const offline = new Set(Array.isArray(resolution.offlineDeviceIds) ? resolution.offlineDeviceIds : []);
    if (resolution.items.some(item => offline.has(item.deviceId))) {
      const hadLocalCopy = supplied.some(item => item.localPath || item.livePhoto?.video?.localPath);
      throw new Error(hadLocalCopy ? 'The local original is no longer available and the iPhone is offline. Connect and unlock the iPhone to download it again.' : 'The iPhone is offline. Connect and unlock it before dragging an original to Explorer.');
    }
  }
  async prepare(payload) {
    if (this.closed) throw new Error('EyeCopy is closing.');
    const generation = this.preparationGeneration;
    const current = () => {
      if (this.closed || generation !== this.preparationGeneration) throw new Error('This file drag was cancelled. Select the file again when ready.');
    };
    const items = normalizeItems(payload), key = keyFor(items);
    const cached = this.prepared.get(key);
    if (cached && Date.now() - cached.created < 15 * 60 * 1000 && (!cached.marker || this.marker?.has(cached.marker.id)) && (!cached.localPaths || localFilesAvailable(cached.localPaths))) return cached;
    if (cached) { this.prepared.delete(key); await this.releasePrepared(cached); }
    if (this.inflight.has(key)) return this.inflight.get(key);
    const work = (async () => {
      // Resolve Live Photo companions from the engine catalogue, never from
      // renderer-supplied nested file paths. This is a cache-only operation.
      const resolution = await this.resolveItems(items), resolved = resolution.items;
      current();
      const allLocal = localFilesAvailable(resolved.map(item => item.localPath));
      if (allLocal) {
        const localPaths = resolved.map(item => item.localPath);
        const result = { created: Date.now(), localPaths, items: resolved };
        this.prepared.set(key, result); return result;
      }
      this.requirePhoneSource(resolution, items);
      if (this.phoneDragMode === 'marker') {
        const detector = await this.startMarkerHelper();
        while (Array.from(this.prepared.values()).filter(entry => entry.marker).length >= 12) {
          const oldest = Array.from(this.prepared.keys()).find(entryKey => this.prepared.get(entryKey).marker);
          const old = this.prepared.get(oldest); this.prepared.delete(oldest); await this.releasePrepared(old);
        }
        const name = resolved.length === 1 ? resolved[0].name : `${resolved.length} originals`;
        const marker = detector.prepare({ name, items: resolved });
        try { await marker.ready; }
        catch (error) { detector.cancel(marker.id); throw error; }
        try { current(); } catch (error) { detector.cancel(marker.id); throw error; }
        let shellTicket;
        try { shellTicket = await this.localTicket.prepare(marker.markerPath); current(); }
        catch (error) { if (shellTicket) this.localTicket.release(shellTicket); detector.cancel(marker.id); throw error; }
        const result = { created: Date.now(), marker, shellTicket, items: resolved };
        this.prepared.set(key, result); return result;
      }
      this.startHelper();
      await this.helper.ready(); this.ready = true;
      current();
      // Keep preparation tokens bounded; active copies have already consumed
      // their entries and own their streams independently.
      const count = () => Array.from(this.prepared.values()).reduce((sum, entry) => sum + (entry.files?.length || 0), 0);
      while (this.prepared.size && count() + resolved.length > 120) {
        const oldest = this.prepared.keys().next().value, old = this.prepared.get(oldest);
        this.prepared.delete(oldest); await this.releasePrepared(old);
      }
      const files = [];
      try {
        for (const item of resolved) {
          const stream = await this.request('stream_file', { item, preferLocal: true });
          files.push({ name: stream.name || item.name, size: Number(stream.size ?? item.size), modified: stream.modified || item.modified, url: validatedStreamURL(stream.url) });
          current();
        }
      } catch (error) { await this.releasePrepared({ files }); throw error; }
      const result = { created: Date.now(), files, items: resolved };
      this.prepared.set(key, result);
      if (this.prepared.size > 128) { const oldest = this.prepared.keys().next().value, old = this.prepared.get(oldest); this.prepared.delete(oldest); await this.releasePrepared(old); }
      return result;
    })();
    this.inflight.set(key, work);
    try { return await work; } finally { if (this.inflight.get(key) === work) this.inflight.delete(key); }
  }
  async start(payload, sender, localRecoveryAttempts = 0) {
    const generation = this.preparationGeneration;
    const current = () => { if (this.closed || generation !== this.preparationGeneration) throw new Error('This file drag was cancelled. Select the file again when ready.'); };
    current();
    if (localRecoveryAttempts > 1) throw new Error('The local copy changed while the drag was starting. Select the file again to prepare its iPhone transfer.');
    const items = normalizeItems(payload), key = keyFor(items);
    let prepared = this.prepared.get(key);
    if (!prepared || Date.now() - prepared.created > 15 * 60 * 1000 || (prepared.marker && !this.marker?.has(prepared.marker.id)) || (prepared.localPaths && !localFilesAvailable(prepared.localPaths))) prepared = await this.prepare(payload);
    current();
    if (prepared.marker) {
      // A prewarmed ticket can outlive a connection change or a download. Check
      // current cached authority before taking the gesture; never probe AFC here.
      let current;
      try { current = await this.resolveItems(items); }
      catch (error) { if (this.prepared.get(key) === prepared) this.prepared.delete(key); await this.releasePrepared(prepared); throw error; }
      if (localFilesAvailable(current.items.map(item => item.localPath))) {
        if (this.prepared.get(key) === prepared) this.prepared.delete(key);
        await this.releasePrepared(prepared);
        const localPaths = current.items.map(item => item.localPath);
        prepared = { created: Date.now(), localPaths, items: current.items };
        this.prepared.set(key, prepared);
      } else {
        try { this.requirePhoneSource(current, items); }
        catch (error) { if (this.prepared.get(key) === prepared) this.prepared.delete(key); await this.releasePrepared(prepared); throw error; }
        prepared.items = current.items;
      }
    }
    if (this.closed || generation !== this.preparationGeneration) throw new Error('This file drag was cancelled.');
    if (prepared.localPaths) {
      const handoff = this.foreground.releaseSourceCapture({ sourceWindow: sourceWindowFor(sender) });
      this.log(`Local Explorer drag: ${prepared.localPaths.length} originals; capture ${JSON.stringify(handoff)}.`);
      if (!handoff.succeeded) throw new Error('The file drag could not keep its mouse gesture. Keep the left mouse button held while dragging into an open Explorer folder and try again.');
      // Recheck immediately before the native helper handoff. If deletion occurred
      // during capture handoff, discard the cached local route and recover once.
      if (!localFilesAvailable(prepared.localPaths)) {
        if (this.prepared.get(key) === prepared) this.prepared.delete(key);
        await this.releasePrepared(prepared);
        return this.start(payload, sender, localRecoveryAttempts + 1);
      }
      try { sender.startDrag({ file: prepared.localPaths[0], files: prepared.localPaths, icon: this.icon }); }
      catch (error) {
        // Electron can synchronously reject a path deleted between the final
        // stat and its native call. Recover only when that path is now missing;
        // unrelated Shell/capture errors must keep their original explanation.
        if (localFilesAvailable(prepared.localPaths)) throw error;
        if (this.prepared.get(key) === prepared) this.prepared.delete(key);
        await this.releasePrepared(prepared);
        return this.start(payload, sender, localRecoveryAttempts + 1);
      }
      return;
    }
    if (prepared.marker) {
      while (prepared.claimed) {
        if (this.prepared.get(key) === prepared) this.prepared.delete(key);
        prepared = await this.prepare(payload);
        current();
        if (!prepared.marker) return this.start(payload, sender, localRecoveryAttempts);
      }
      prepared.claimed = true; if (this.prepared.get(key) === prepared) this.prepared.delete(key);
      const id = 'explorer-marker-' + prepared.marker.id;
      const transfer = { id, markerId: prepared.marker.id, markerBasename: prepared.marker.basename, name: prepared.items.length === 1 ? prepared.items[0].name : `${prepared.items.length} originals to Explorer`, direction: 'download', state: 'queued', bytes: 0, total: prepared.items.reduce((sum, item) => sum + Number(item.size || 0), 0), speed: 0, eta: null, error: null, route: 'Explorer folder selection', awaitingDestination: true, destination: null, started: Date.now(), files: [], items: prepared.items, message: 'Drop into a local Explorer folder to choose the destination. The transfer starts automatically once the folder is confirmed.' };
      this.transfers.set(id, transfer); this.emitJobs();
      try {
        const handoff = this.foreground.releaseSourceCapture({ sourceWindow: sourceWindowFor(sender) });
        this.log(`Explorer marker ${id}: capture ${JSON.stringify(handoff)}.`);
        this.log(`Explorer marker ${id}: native source validation ${JSON.stringify(prepared.shellTicket.sourceValidation || {})}.`);
        if (!handoff.succeeded) throw new Error('Keep the left mouse button held while dragging into Explorer, then try again.');
        this.marker.begin(prepared.marker.id);
        this.log(`Explorer marker ${id}: local gesture started for ${prepared.items.length} originals; marker=${prepared.marker.basename}.`);
        const result = await this.localTicket.drag(prepared.shellTicket, { sourceWindow: sourceWindowFor(sender), id });
        this.log(`Explorer marker ${id}: Shell gesture returned; dropped=${Boolean(result.dropped)}; effect=${result.effect}; hresult=${result.hresult}; point=${JSON.stringify(result.point)}; sourceExists=${fs.existsSync(prepared.marker.markerPath)}; elapsedMs=${Date.now() - transfer.started}.`);
        if (!result.dropped) {
          this.marker.cancel(prepared.marker.id);
          this.onMessage({ event: 'cancelled', id, message: 'Windows did not accept the ticket drop. Drag again into the files area of an Explorer folder.' });
        } else if (this.transfers.get(id) === transfer && !this.isTerminal(transfer) && !transfer.handoffPending) {
          this.watchDestination(transfer);
          this.checkDroppedFolder(transfer, result.point);
        }
      } catch (error) { this.marker.cancel(prepared.marker.id); this.onMessage({ event: 'error', id, message: error.message }); throw error; }
      finally { this.localTicket.release(prepared.shellTicket); }
      return;
    }
    if (!this.ready) throw new Error('The Explorer drag helper is starting. Keep the file selected and try dragging again in a moment.');
    // Each copy owns its token set, including when the same selection is
    // dragged again while Explorer is still copying the first drop.
    while (prepared.claimed) {
      if (this.prepared.get(key) === prepared) this.prepared.delete(key);
      prepared = await this.prepare(payload);
      current();
    }
    prepared.claimed = true;
    if (this.prepared.get(key) === prepared) this.prepared.delete(key);
    const id = 'explorer-' + crypto.randomUUID();
    const transfer = { id, name: prepared.files.length === 1 ? prepared.files[0].name : `${prepared.files.length} files to Explorer`, direction: 'download', state: 'queued', bytes: 0, total: prepared.files.reduce((sum, file) => sum + file.size, 0), speed: 0, eta: null, error: null, destination: 'File Explorer drop folder', started: Date.now(), files: prepared.files };
    this.transfers.set(id, transfer);
    this.watch(transfer, this.beginTimeoutMs, 'The Explorer drag did not start. Keep the mouse button held while dragging and try again.');
    this.emitJobs();
    try { await this.helper.begin(prepared.files, id, { sourceWindow: sourceWindowFor(sender) }); }
    catch (error) {
      this.onMessage({ event: 'error', id, message: error.message });
      throw error;
    }
  }
  async onMarkerMessage(message) {
    if (message.event === 'marker-registered' || message.event === 'marker-candidate') {
      this.log(`Explorer marker ${message.id}: ${message.event} ${JSON.stringify({ basename: message.basename, shellEvent: message.shellEvent, destination: message.destination })}`);
      return;
    }
    if (message.event === 'detector-error') {
      this.markerStartup = null; this.ready = false;
      for (const [key, prepared] of this.prepared) if (prepared.marker) {
        this.prepared.delete(key); this.releasePrepared(prepared);
      }
      for (const transfer of this.transfers.values()) if (transfer.markerId && !this.isTerminal(transfer)) this.requireDestination(transfer, 'Automatic folder detection stopped. Choose folder to start the transfer.');
      return;
    }
    const transfer = this.transfers.get('explorer-marker-' + message.id);
    if (!transfer || this.isTerminal(transfer)) return;
    if (message.event === 'timeout') { this.requireDestination(transfer); return; }
    if (message.event !== 'dropped' || transfer.handoffPending) return;
    if (!message.markerConsumed || typeof message.destination !== 'string' || !path.isAbsolute(message.destination)) { this.fail(transfer, 'The Explorer destination could not be verified. Use Download to folder.'); return; }
    await this.setDestination(transfer.id, message.destination);
  }
  selection(id) {
    const transfer = this.transfers.get(id);
    return transfer?.markerId && transfer.state !== 'cancelled' ? transfer : null;
  }
  async setDestination(id, destination) {
    const transfer = this.selection(id);
    if (!transfer || transfer.handoffPending) return false;
    if (typeof destination !== 'string' || !path.isAbsolute(destination)) throw new Error('Choose an absolute destination folder.');
    this.unwatch(transfer.id); this.marker?.cancel(transfer.markerId);
    transfer.state = 'queued'; transfer.error = null; transfer.handoffPending = true; transfer.destination = destination; transfer.message = 'Destination selected. Preparing the transfer…'; this.emitJobs();
    try {
      if (transfer.markerBasename) {
        try { await this.marker?.cleanupTicket(transfer.markerBasename, destination); }
        catch { this.log(`Explorer marker ${transfer.id}: ticket cleanup could not finish.`); }
      }
      if (this.isTerminal(transfer)) return false;
      const result = await this.request('queue_download', { deviceId: transfer.items[0].deviceId, items: transfer.items, destination });
      if (!Array.isArray(result?.jobIds) || !result.jobIds.length) throw new Error('The paused queue entries could not be prepared. Choose folder and try again.');
      transfer.engineJobIds = result.jobIds;
      if (this.isTerminal(transfer)) {
        await Promise.allSettled(result.jobIds.map(id => this.request('cancel_job', { id })));
        return false;
      }
      // Persist before starting, and compare-and-start only untouched deferred
      // jobs. A queue Cancel must never be reversed by an automatic retry.
      for (const jobId of result.jobIds) {
        if (this.isTerminal(transfer)) break;
        try { await this.request('start_queued_job', { id: jobId }); }
        catch (error) { this.log(`Explorer marker ${transfer.id}: automatic start failed; use Start transfer for the remaining paused entry. ${error.message}`); }
      }
      if (this.isTerminal(transfer)) {
        await Promise.allSettled(result.jobIds.map(id => this.request('cancel_job', { id })));
        return false;
      }
      this.log(`Explorer marker ${transfer.id}: destination selected; automatic start requested for ${result.jobIds.length} transfer jobs.`);
      this.transfers.delete(transfer.id); this.lastEmit.delete(transfer.id); this.emitJobs();
      return true;
    } catch (error) {
      transfer.handoffPending = false;
      if (transfer.state !== 'cancelled') this.requireDestination(transfer, error.message);
      return false;
    } finally {
      transfer.handoffPending = false;
      this.emitJobs();
    }
  }
  requireDestination(transfer, message = 'Windows did not report the drop folder. Choose folder to start the transfer automatically.') {
    if (this.isTerminal(transfer) || transfer.handoffPending) return;
    // Showing a responsive fallback is not cancellation. Explorer may still
    // be finishing the drop, so preserve the source and nonce until the
    // detector's bounded timeout, explicit Cancel, or confirmed destination.
    this.unwatch(transfer.id);
    transfer.state = 'queued'; transfer.awaitingDestination = true; transfer.destination = null; transfer.message = message; transfer.error = null;
    this.emitJobs();
  }
  watchDestination(transfer) {
    this.unwatch(transfer.id);
    const timer = setTimeout(() => this.requireDestination(transfer), 6000);
    timer.unref?.(); this.watchdogs.set(transfer.id, timer);
  }
  onMessage(message) {
    if (message.event === 'ready') { this.ready = true; return; }
    const transfer = this.transfers.get(message.id);
    if (!transfer) return;
    if (this.isTerminal(transfer)) return;
    if (message.event !== 'progress') {
      const detail = Object.fromEntries(['sourceForeground', 'sourceProvided', 'hostOwned', 'hostActive', 'hostFocused', 'inputAttached', 'buttonHeld', 'hardwareHeld', 'threadButtonHeldBefore', 'threadButtonHeld', 'threadButtonSynced', 'sourceCaptureOwned', 'captureReleased', 'captureTransferred', 'attempted', 'succeeded', 'privateFormats', 'helperForeground', 'ownCapture', 'keys', 'escape', 'effect', 'hresult', 'stage'].filter(key => message[key] !== undefined).map(key => [key, message[key]]));
      this.log(`Explorer ${transfer.id}: ${message.event}${message.event === 'drag_end' ? ` dropped=${Boolean(message.dropped)}` : ''} bytes=${transfer.bytes}/${transfer.total} ${JSON.stringify(detail)}`);
    }
    if (message.event === 'accepted') transfer.message = 'The drag helper received the files. Starting the Explorer drag…';
    if (message.event === 'begin') { this.unwatch(transfer.id); transfer.state = 'transferring'; transfer.message = 'Drop the selected originals into an Explorer folder.'; }
    if (message.event === 'copying') { transfer.state = 'transferring'; transfer.message = 'Explorer is copying the originals.'; this.watchCopy(transfer); }
    if (message.event === 'drag_end' && message.dropped) { transfer.message = 'Explorer accepted the drop. Waiting for file data…'; this.watchCopy(transfer); }
    if (message.event === 'drag_end' && !message.dropped) { this.onMessage({ event: 'cancelled', id: transfer.id, message: message.message || 'Explorer did not accept this drop. Drag again into the files area inside an open folder.' }); return; }
    if (message.event === 'progress') {
      transfer.state = 'transferring'; transfer.bytes = Math.min(transfer.total, Number(message.bytes) || 0);
      transfer.speed = Math.max(0, Number(message.speedBps) || 0);
      transfer.eta = transfer.speed ? (transfer.total - transfer.bytes) / transfer.speed : null;
      transfer.message = 'Explorer is copying the originals.'; this.watchCopy(transfer);
    }
    if (['completed', 'cancelled', 'error'].includes(message.event)) {
      transfer.state = message.event === 'completed' ? 'completed' : message.event === 'cancelled' ? 'cancelled' : 'failed';
      if (message.event === 'cancelled' && transfer.markerId) transfer.awaitingDestination = false;
      if (message.event === 'completed') transfer.bytes = transfer.total;
      transfer.eta = null; transfer.error = message.event === 'completed' ? null : message.message || (message.event === 'error' ? 'Explorer could not finish copying this phone file. Reconnect and try again.' : null);
      transfer.speed = 0; transfer.message = null; this.unwatch(transfer.id);
      for (const file of transfer.files) this.request('stop_stream', { url: file.url }).catch(() => {});
    }
    const last = this.lastEmit.get(transfer.id) || 0;
    if (Date.now() - last > 150 || message.event !== 'progress') { this.lastEmit.set(transfer.id, Date.now()); this.emitJobs(); }
  }
  emitJobs() { this.emit(this.jobs()); }
  jobs() { return Array.from(this.transfers.values()).map(({ started, files, items, markerId, markerBasename, engineJobIds, handoffPending, ...job }) => ({ ...job, ...(handoffPending || this.pendingCancellations.has(job.id) ? { cancelRequested: this.isTerminal(job) } : {}) })); }
  clearFinished() {
    for (const [id, transfer] of this.transfers) {
      if (!this.isTerminal(transfer) || transfer.handoffPending || this.pendingCancellations.has(id)) continue;
      this.unwatch(id); this.transfers.delete(id); this.lastEmit.delete(id);
    }
    this.emitJobs();
  }
  cancelAll() {
    // Revoke not-yet-published preparations before awaiting any service reply.
    // Late preparation/queue acknowledgements must not revive stopped work.
    this.preparationGeneration++;
    this.inflight.clear();
    for (const prepared of this.prepared.values()) this.releasePrepared(prepared);
    this.prepared.clear();
    let count = 0;
    for (const transfer of this.transfers.values()) if (!this.isTerminal(transfer)) {
      this.control(transfer.id, 'cancel_job'); count++;
    }
    this.emitJobs();
    return { changed: count > 0, count, jobs: this.jobs() };
  }
  clearAll() {
    let cleared = 0, retained = 0;
    for (const [id, transfer] of this.transfers) {
      if (!this.isTerminal(transfer) || transfer.handoffPending || this.pendingCancellations.has(id)) { retained++; continue; }
      this.unwatch(id); this.transfers.delete(id); this.lastEmit.delete(id); cleared++;
    }
    this.emitJobs();
    return { cleared, retained, blocked: retained > 0, jobs: this.jobs() };
  }
  isTerminal(transfer) { return ['completed', 'cancelled', 'failed'].includes(transfer.state); }
  unwatch(id) { const timer = this.watchdogs.get(id); if (timer) clearTimeout(timer); this.watchdogs.delete(id); }
  watch(transfer, timeout, message) {
    this.unwatch(transfer.id);
    const timer = setTimeout(() => { this.watchdogs.delete(transfer.id); if (!this.isTerminal(transfer)) this.fail(transfer, message); }, timeout);
    timer.unref?.(); this.watchdogs.set(transfer.id, timer);
  }
  watchCopy(transfer) { this.watch(transfer, this.stallTimeoutMs, 'Explorer stopped reporting progress. Check its copy or filename-conflict dialog and the iPhone connection, then drag again.'); }
  fail(transfer, message) {
    this.onMessage({ event: 'error', id: transfer.id, message });
    if (transfer.markerId) this.marker?.cancel(transfer.markerId);
    try { this.helper?.cancel(transfer.id); } catch { this.log('Explorer helper could not acknowledge the stopped copy.'); }
  }
  control(identifier, command) {
    const transfer = this.transfers.get(identifier);
    if (!transfer) return false;
    if (this.isTerminal(transfer)) return true;
    if (command === 'cancel_job') {
      // The queue and phone stream stop immediately, independently of an
      // Explorer/COM thread acknowledging cancellation.
      this.onMessage({ event: 'cancelled', id: identifier, message: transfer.markerId ? 'Cancelled this drop request. Any started phone copy will be stopped.' : 'Cancelled. Explorer may retain a partial file in the destination folder.' });
      if (transfer.markerId) this.marker?.cancel(transfer.markerId);
      if (transfer.engineJobIds?.length) {
        const cancellation = Promise.allSettled(transfer.engineJobIds.map(id => this.request('cancel_job', { id })));
        this.pendingCancellations.set(identifier, cancellation);
        cancellation.finally(() => { if (this.pendingCancellations.get(identifier) === cancellation) this.pendingCancellations.delete(identifier); this.emitJobs(); });
      }
      try { this.helper?.cancel(identifier); } catch { this.log('Explorer helper could not acknowledge cancellation.'); }
    }
    else throw new Error('Explorer manages this copy. Cancel it and drag again, or use Download to folder for pause and resume.');
    return true;
  }
  close() {
    this.closed = true;
    this.cancelAll();
    for (const id of this.watchdogs.keys()) this.unwatch(id);
    for (const prepared of this.prepared.values()) this.releasePrepared(prepared);
    this.prepared.clear();
    this.helper?.close();
    this.marker?.close();
  }
}
module.exports = { DragManager, validatedStreamURL, normalizeItems, sourceWindowFor };
