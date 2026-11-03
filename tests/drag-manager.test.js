'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture({ failBegin = false, failStreamAt = 0, expandedItems = null, resolveItemsHook = null, offlineDeviceIds = [], beforeLocalCapture = null, cancelThrows = false, localCaptureSucceeded = true, phoneDragMode = 'virtual', failMarkerBegin = false, downloadDeferred = null, markerRegistration = undefined, startDeferred = null, cleanupDeferred = null, failStart = false, shellDrop = { dropped: true, effect: 1, hresult: 0x40100, point: { x: 10, y: 20 } }, dropFolderResult = { verified: false } } = {}) {
  const starts = [], revoked = [], snapshots = [], cancelled = [], logs = [], localCaptures = [], markerBegins = [], markerCancelled = [], ticketCleanup = [], downloads = [], queues = [], engineCancelled = [], engineStarted = [];
  const ticketPrepared = [], ticketReleased = [], shellGestures = [], folderChecks = [], markerCandidates = [];
  let serial = 0;
  let resolutions = 0;
  let markerSerial = 0;
  class FakeMarkerDrop {
    constructor() { this.jobs = new Map(); }
    async start() {}
    prepare(args) { const prepared = { id: `marker-${++markerSerial}`, basename: `_EyeCopy_drop_${String(markerSerial).padStart(32, '0')}.txt`, markerPath: __filename, ready: markerRegistration }; this.jobs.set(prepared.id, args); return prepared; }
    has(id) { return this.jobs.has(id); }
    begin(id) { if (failMarkerBegin) throw new Error('Open a local Explorer folder first.'); markerBegins.push(id); }
    cancel(id) { markerCancelled.push(id); this.jobs.delete(id); }
    cleanupTicket(basename, folder) { ticketCleanup.push({ basename, folder }); return cleanupDeferred || Promise.resolve(true); }
    candidate(message) { markerCandidates.push(message); }
    close() { this.jobs.clear(); }
  }
  const helper = {
    ready: async () => 'test-native-helper.exe',
    begin: async (files, id, options) => {
      starts.push({ id, files, options });
      if (failBegin) throw new Error('Native helper lost its input pipe.');
      return id;
    },
    cancel: id => { cancelled.push(id); if (cancelThrows) throw new Error('Broken helper pipe.'); },
    close: () => {}
  };
  const module = { exports: {} };
  const source = fs.readFileSync(path.resolve(__dirname, '../desktop/drag-manager.js'), 'utf8');
  const timers = new Set();
  const context = {
    module, Buffer, URL, console,
    setTimeout: callback => { const timer = { callback, unref() {} }; timers.add(timer); return timer; },
    clearTimeout: timer => timers.delete(timer),
    require: name => {
      if (name === 'electron') return { nativeImage: { createFromBitmap: () => ({ fakeIcon: true }) }, BrowserWindow: { fromWebContents: sender => sender.testOwnerWindow } };
      if (name === './native-drag/broker') return { createDragBroker: () => helper };
      if (name === './native-drag/foreground') return { createForegroundHandoff: () => ({ releaseSourceCapture: options => { localCaptures.push(options); beforeLocalCapture?.(options); return { succeeded: localCaptureSucceeded }; } }) };
      if (name === './native-drag/local-ticket') return { createLocalTicketDrag: () => ({ prepare: file => { const prepared = { file }; ticketPrepared.push(prepared); return prepared; }, drag: (prepared, options) => { shellGestures.push({ prepared, options }); return shellDrop; }, release: prepared => { if (!prepared.released) { prepared.released = true; ticketReleased.push(prepared); } } }) };
      if (name === './marker-drop') return { MarkerDrop: FakeMarkerDrop, buildMarkerProbe: async () => 'fake-marker-probe.exe' };
      if (name === '../scripts/DropFolderProbe') return { buildDropFolderProbe: async () => 'fake-folder-probe.exe', resolveDropFolder: async (_executable, point) => { folderChecks.push(point); return dropFolderResult; } };
      return require(name);
    }
  };
  vm.runInNewContext(source, context, { filename: 'desktop/drag-manager.js' });
  const request = async (command, args) => {
    if (command === 'resolve_transfer_items') { resolutions++; return resolveItemsHook?.(args, resolutions) || { items: expandedItems || args.items, offlineDeviceIds }; }
    if (command === 'stream_file') {
      serial++;
      if (serial === failStreamAt) throw new Error('The phone disconnected during selection preparation.');
      return { name: args.item.name, size: args.item.size, modified: args.item.modified, url: `http://127.0.0.1:49152/${String(serial).padStart(32, '0')}/original` };
    }
    if (command === 'stop_stream') { revoked.push(args.url); return { stopped: true }; }
    if (command === 'download') { downloads.push(args); return downloadDeferred || { jobIds: ['engine-copy-1'] }; }
    if (command === 'queue_download') { queues.push(args); return downloadDeferred || { jobIds: ['engine-copy-1'] }; }
    if (command === 'start_queued_job') { engineStarted.push(args.id); if (failStart) throw new Error('Connection to the worker interrupted.'); return startDeferred || { changed: true }; }
    if (command === 'cancel_job') { engineCancelled.push(args.id); return { changed: true }; }
    throw new Error(`Unexpected backend command ${command}`);
  };
  const manager = new module.exports.DragManager({ root: 'test-root', dataDir: 'test-data', request, emit: jobs => snapshots.push(jobs), log: message => logs.push(message), phoneDragMode });
  const payload = { items: [{ id: 'iphone-original-1', deviceId: 'phone-1', name: 'large.mov', size: 1000, modified: 123456, kind: 'video', source: 'app:BlackmagicCamera' }] };
  const sender = { startDrag: () => { throw new Error('Remote files must use the native stream helper.'); } };
  return { manager, payload, sender, starts, revoked, snapshots, cancelled, logs, timers, localCaptures, markerBegins, markerCancelled, ticketCleanup, downloads, queues, engineCancelled, engineStarted, ticketPrepared, ticketReleased, shellGestures, folderChecks, markerCandidates, requestCount: () => serial, resolutionCount: () => resolutions };
}

function temporaryOriginals(t, count = 1) {
  const scope = path.resolve(__dirname, '../test-output/drag-originals');
  fs.mkdirSync(scope, { recursive: true });
  const directory = fs.mkdtempSync(path.join(scope, 'eyecopy-'));
  const files = Array.from({ length: count }, (_, index) => path.join(directory, `original-${index}.mov`));
  for (const file of files) fs.writeFileSync(file, 'owned local-original fixture');
  t.after(() => {
    for (const file of files) {
      try { if (fs.lstatSync(file).isDirectory()) fs.rmdirSync(file); else fs.unlinkSync(file); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
    fs.rmdirSync(directory);
  });
  return files;
}

test('two copies of the same selection own independent tokens and completion revokes only its copy', async () => {
  const f = fixture();
  await f.manager.prepare(f.payload);
  await f.manager.start(f.payload, f.sender);
  await f.manager.start(f.payload, f.sender);
  assert.equal(f.starts.length, 2);
  const [first, second] = f.starts;
  assert.notEqual(first.id, second.id);
  assert.notEqual(first.files[0].url, second.files[0].url);
  f.manager.onMessage({ event: 'completed', id: first.id });
  assert.deepEqual(f.revoked, [first.files[0].url]);
  assert.equal(f.manager.transfers.get(first.id).state, 'completed');
  assert.equal(f.manager.transfers.get(second.id).state, 'queued');
  f.manager.onMessage({ event: 'progress', id: second.id, bytes: 400, speedBps: 50 });
  assert.equal(f.manager.transfers.get(second.id).state, 'transferring');
  assert.equal(f.manager.transfers.get(second.id).bytes, 400);
  assert.ok(!f.revoked.includes(second.files[0].url));
});

test('overlapping starts while token preparation is in flight cannot share or revoke each other\'s token', async () => {
  const f = fixture();
  await Promise.all([f.manager.start(f.payload, f.sender), f.manager.start(f.payload, f.sender)]);
  assert.equal(f.starts.length, 2);
  const [first, second] = f.starts;
  assert.notEqual(first.files[0].url, second.files[0].url, 'Concurrent start calls shared their in-flight preparation result.');
  f.manager.onMessage({ event: 'cancelled', id: first.id, message: 'First copy cancelled.' });
  assert.deepEqual(f.revoked, [first.files[0].url]);
  assert.ok(!f.revoked.includes(second.files[0].url));
});

test('native begin rejection ends the queued job and releases its tokens', async () => {
  const f = fixture({ failBegin: true });
  await assert.rejects(f.manager.start(f.payload, f.sender), /lost its input pipe/);
  assert.equal(f.starts.length, 1);
  const started = f.starts[0], job = f.manager.transfers.get(started.id);
  assert.equal(job.state, 'failed');
  assert.match(job.error, /lost its input pipe/);
  assert.deepEqual(f.revoked, [started.files[0].url]);
  assert.equal(f.snapshots.at(-1)[0].state, 'failed');
  assert.equal(f.manager.prepared.size, 0);
});

test('a failed batch preparation releases the streams already issued for that selection', async () => {
  const f = fixture({ failStreamAt: 2 });
  f.payload.items.push({ ...f.payload.items[0], id: 'second-original', name: 'second.mov' });
  await assert.rejects(f.manager.prepare(f.payload), /phone disconnected/);
  assert.equal(f.revoked.length, 1);
  assert.equal(f.manager.prepared.size, 0);
  assert.equal(f.manager.inflight.size, 0);
  assert.equal(f.starts.length, 0);
});

test('expired idle preparations release their old tokens before issuing a fresh stream', async () => {
  const f = fixture();
  const old = await f.manager.prepare(f.payload);
  old.created = Date.now() - 16 * 60 * 1000;
  const current = await f.manager.prepare(f.payload);
  assert.notEqual(current.files[0].url, old.files[0].url);
  assert.deepEqual(f.revoked, [old.files[0].url]);
  assert.ok(!f.revoked.includes(current.files[0].url));
});

test('progress uses measured byte speed and reports ETA without counting drag hover time', async () => {
  const f = fixture();
  await f.manager.start(f.payload, f.sender);
  const id = f.starts[0].id, job = f.manager.transfers.get(id);
  job.started = Date.now() - 300000;
  f.manager.onMessage({ event: 'progress', id, bytes: 600, speedBps: 200 });
  assert.equal(job.speed, 200);
  assert.equal(job.eta, 2);
  assert.equal(job.bytes, 600);
  f.manager.onMessage({ event: 'progress', id, bytes: 1200, speedBps: 250 });
  assert.equal(job.bytes, 1000);
  assert.equal(job.eta, 0);
});

test('Explorer skip and cancellation explanations remain visible on the transfer job', async () => {
  const f = fixture();
  await f.manager.start(f.payload, f.sender);
  const id = f.starts[0].id;
  f.manager.onMessage({ event: 'cancelled', id, message: 'Explorer skipped an existing file. Check the chosen folder.' });
  const job = f.manager.transfers.get(id);
  assert.equal(job.state, 'cancelled');
  assert.match(job.error, /skipped an existing file/);
  assert.equal(job.bytes, 0);
});

test('cancel updates the queue and revokes the source immediately without a helper acknowledgement', async () => {
  const f = fixture({ cancelThrows: true });
  await f.manager.start(f.payload, f.sender);
  const { id, files } = f.starts[0];
  assert.equal(f.manager.control(id, 'cancel_job'), true);
  assert.equal(f.manager.transfers.get(id).state, 'cancelled');
  assert.deepEqual(f.revoked, [files[0].url]);
  assert.equal(f.manager.watchdogs.size, 0);
  f.manager.onMessage({ event: 'begin', id });
  f.manager.onMessage({ event: 'progress', id, bytes: 999, speedBps: 100 });
  f.manager.onMessage({ event: 'completed', id });
  assert.equal(f.manager.transfers.get(id).state, 'cancelled');
  assert.equal(f.manager.transfers.get(id).bytes, 0);
  assert.deepEqual(f.revoked, [files[0].url]);
});

test('a received request that never begins fails promptly and releases its streams', async () => {
  const f = fixture();
  await f.manager.start(f.payload, f.sender);
  const { id, files } = f.starts[0];
  f.manager.onMessage({ event: 'accepted', id });
  f.manager.watchdogs.get(id).callback();
  assert.equal(f.manager.transfers.get(id).state, 'failed');
  assert.match(f.manager.transfers.get(id).error, /drag did not start/);
  assert.deepEqual(f.cancelled, [id]);
  assert.deepEqual(f.revoked, [files[0].url]);
});

test('stalled accepted copies fail and progress refreshes the deadline', async () => {
  const f = fixture();
  await f.manager.start(f.payload, f.sender);
  const { id } = f.starts[0];
  f.manager.onMessage({ event: 'begin', id });
  assert.equal(f.manager.watchdogs.size, 0, 'A held drag gesture must not expire.');
  f.manager.onMessage({ event: 'drag_end', id, dropped: true });
  const firstTimer = f.manager.watchdogs.get(id);
  f.manager.onMessage({ event: 'progress', id, bytes: 200, speedBps: 100 });
  assert.notEqual(f.manager.watchdogs.get(id), firstTimer);
  assert.ok(!f.timers.has(firstTimer));
  f.manager.watchdogs.get(id).callback();
  assert.equal(f.manager.transfers.get(id).state, 'failed');
  assert.match(f.manager.transfers.get(id).error, /stopped reporting progress/);
});

test('dragging a Live Photo uses the authoritative still and motion originals', async () => {
  const still = { id: 'still', deviceId: 'phone-1', kind: 'photo', name: 'IMG_0001.JPG', modified: 'today', size: 10 };
  const motion = { id: 'motion', deviceId: 'phone-1', kind: 'video', name: 'IMG_0001.MOV', modified: 'today', size: 20 };
  const f = fixture({ expandedItems: [still, motion] });
  f.payload.items = [{ ...still, livePhoto: { verified: true, video: { ...motion, id: 'forged', name: 'wrong.mov' } } }];
  await f.manager.start(f.payload, f.sender);
  assert.deepEqual(Array.from(f.starts[0].files, file => file.name), [still.name, motion.name]);
  assert.equal(f.manager.jobs()[0].total, 30);
});

test('native drag receives the trusted Electron window handle without truncating 64 bits', async () => {
  const f = fixture();
  const handle = Buffer.alloc(8); handle.writeBigUInt64LE(0x123456789abcdef0n);
  f.sender.testOwnerWindow = { getNativeWindowHandle: () => handle };
  f.payload.sourceWindow = '999';
  await f.manager.start(f.payload, f.sender);
  assert.equal(f.starts[0].options.sourceWindow, '1311768467463790320');
});

test('an unaccepted drop preserves the native explanation and the next attempt owns fresh streams', async () => {
  const f = fixture();
  await f.manager.start(f.payload, f.sender);
  const first = f.starts[0];
  f.manager.onMessage({ event: 'begin', id: first.id });
  f.manager.onMessage({ event: 'drag_end', id: first.id, dropped: false, message: 'The gesture was cancelled before a folder accepted it.' });
  assert.equal(f.manager.transfers.get(first.id).state, 'cancelled');
  assert.match(f.manager.transfers.get(first.id).error, /gesture was cancelled/);
  await f.manager.start(f.payload, f.sender);
  const second = f.starts[1];
  assert.notEqual(second.files[0].url, first.files[0].url);
  f.manager.onMessage({ event: 'begin', id: second.id });
  f.manager.onMessage({ event: 'progress', id: second.id, bytes: 1000 });
  f.manager.onMessage({ event: 'completed', id: second.id });
  assert.equal(f.manager.transfers.get(second.id).state, 'completed');
  assert.equal(f.manager.transfers.get(first.id).state, 'cancelled');
});

test('download completion invalidates a prepared phone route in favor of its local original', async () => {
  const f = fixture();
  const phone = await f.manager.prepare(f.payload);
  f.payload.items[0].localPath = __filename;
  const local = await f.manager.prepare(f.payload);
  assert.notEqual(local, phone);
  assert.deepEqual(Array.from(local.localPaths), [__filename]);
  assert.equal(local.files, undefined);
  assert.equal(f.requestCount(), 1, 'Preparing an already downloaded original must not open a phone stream.');
});

test('a newly downloaded Live Photo companion invalidates the partially local route', async () => {
  const still = { id: 'still', deviceId: 'phone-1', kind: 'photo', name: 'live.jpg', size: 10, modified: 123, localPath: __filename };
  const motion = { id: 'motion', deviceId: 'phone-1', kind: 'video', name: 'live.mov', size: 20, modified: 123 };
  const f = fixture({ expandedItems: [still, motion] });
  f.payload.items = [{ ...still, livePhoto: { verified: true, video: motion } }];
  const partial = await f.manager.prepare(f.payload);
  assert.equal(partial.files.length, 2);
  motion.localPath = __filename;
  const local = await f.manager.prepare(f.payload);
  assert.notEqual(local, partial);
  assert.equal(local.localPaths.length, 2);
  assert.equal(f.requestCount(), 2, 'A fully downloaded pair must reuse both PC originals.');
});

test('every repeated local drag releases only its trusted source capture before Electron starts', async () => {
  const f = fixture();
  f.payload.items[0].localPath = __filename;
  const handle = Buffer.alloc(8); handle.writeBigUInt64LE(0x123456789abcdef0n);
  const localStarts = [];
  f.sender.testOwnerWindow = { getNativeWindowHandle: () => handle };
  f.sender.startDrag = args => localStarts.push(args);
  await f.manager.prepare(f.payload);
  await f.manager.start(f.payload, f.sender);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarts.length, 2);
  assert.equal(f.localCaptures.length, 2);
  assert.equal(f.localCaptures[0].sourceWindow, '1311768467463790320');
  assert.deepEqual(Array.from(localStarts[0].files), [__filename]);
  assert.equal(f.starts.length, 0);
  assert.equal(f.requestCount(), 0);
});

test('local drags refuse a lost or unrelated mouse capture without starting an invisible drag', async () => {
  const f = fixture({ localCaptureSucceeded: false });
  f.payload.items[0].localPath = __filename;
  let attempted = false;
  f.sender.startDrag = () => { attempted = true; };
  await assert.rejects(f.manager.start(f.payload, f.sender), /keep its mouse gesture/);
  assert.equal(attempted, false);
  assert.equal(f.requestCount(), 0);
});

test('phone marker drag starts the ordinary queue only after the destination is verified', async () => {
  const f = fixture({ phoneDragMode: 'marker' }), localStarts = [];
  f.sender.startDrag = args => localStarts.push(args);
  await f.manager.prepare(f.payload);
  await f.manager.start(f.payload, f.sender);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(localStarts.length, 0, 'Phone tickets use the native Shell file object.');
  assert.equal(f.ticketReleased.length, 1);
  assert.equal(f.starts.length, 0);
  assert.equal(f.requestCount(), 0, 'A placeholder drag must not predownload or open phone streams.');
  assert.equal(f.downloads.length, 0, 'Phone bytes must wait for a verified destination.');
  assert.equal(f.queues.length, 0);
  const job = f.manager.jobs()[0], markerId = f.markerBegins[0];
  assert.equal(job.state, 'queued');
  assert.equal(job.items, undefined, 'Private item paths must not leak into the queue row.');
  await f.manager.onMarkerMessage({ event: 'dropped', id: markerId, destination: path.dirname(__filename), markerConsumed: true, items: [{ id: 'forged' }] });
  assert.equal(f.queues.length, 1);
  assert.equal(f.downloads.length, 0, 'The handoff must first persist deferred entries.');
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  assert.equal(f.queues[0].destination, path.dirname(__filename));
  assert.equal(f.queues[0].items[0].id, f.payload.items[0].id, 'Use the authoritative prepared files, never event-supplied items.');
  assert.equal(job.markerBasename, undefined, 'The cleanup ticket is private to the manager.');
  assert.equal(f.ticketCleanup[0].folder, path.dirname(__filename));
  assert.equal(f.manager.jobs().length, 0, 'The engine queue must replace the folder-selection row.');
});

test('repeated marker gestures own different placeholders and expand both Live Photo originals', async () => {
  const still = { id: 'still', deviceId: 'phone-1', kind: 'photo', name: 'live.jpg', size: 10 };
  const motion = { id: 'motion', deviceId: 'phone-1', kind: 'video', name: 'live.mov', size: 20 };
  const f = fixture({ phoneDragMode: 'marker', expandedItems: [still, motion] });
  f.sender.startDrag = () => {};
  f.payload.items = [{ ...still, livePhoto: { verified: true, video: motion } }];
  await f.manager.start(f.payload, f.sender);
  await f.manager.start(f.payload, f.sender);
  assert.notEqual(f.markerBegins[0], f.markerBegins[1]);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[1], destination: path.dirname(__filename), markerConsumed: true });
  assert.deepEqual(Array.from(f.queues[0].items, item => item.id), ['still', 'motion']);
  assert.equal(f.downloads.length, 0);
  assert.equal(f.manager.jobs().length, 1, 'The other pending drop must remain independent.');
});

test('cancelled or unverified marker drops never enqueue a phone download', async () => {
  const f = fixture({ phoneDragMode: 'marker' }); f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  f.manager.control(f.manager.jobs()[0].id, 'cancel_job');
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.downloads.length, 0);
  assert.equal(f.queues.length, 0);
  assert.ok(f.markerCancelled.includes(f.markerBegins[0]));
  await f.manager.start(f.payload, f.sender);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[1], destination: path.dirname(__filename), markerConsumed: false });
  assert.equal(f.downloads.length, 0);
  assert.equal(f.queues.length, 0);
  assert.equal(f.manager.jobs().at(-1).state, 'failed');
});

test('cancel during marker handoff cancels engine jobs as soon as they are acknowledged', async () => {
  let acknowledge;
  const f = fixture({ phoneDragMode: 'marker', downloadDeferred: new Promise(resolve => { acknowledge = resolve; }) });
  f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  const handoff = f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.queues.length, 1, 'Cancellation must occur during the queue RPC, after ticket cleanup.');
  f.manager.control(f.manager.jobs()[0].id, 'cancel_job');
  acknowledge({ jobIds: ['engine-1', 'engine-2'] }); await handoff;
  assert.deepEqual(f.engineCancelled, ['engine-1', 'engine-2']);
  assert.equal(f.engineStarted.length, 0);
  assert.equal(f.manager.jobs()[0].state, 'cancelled');
});

test('missing Explorer folder and expired placeholders fail or reprepare without frozen jobs', async () => {
  const failed = fixture({ phoneDragMode: 'marker', failMarkerBegin: true }); failed.sender.startDrag = () => {};
  await assert.rejects(failed.manager.start(failed.payload, failed.sender), /Open a local Explorer folder/);
  assert.equal(failed.manager.jobs()[0].state, 'failed');
  const f = fixture({ phoneDragMode: 'marker' }); f.sender.startDrag = () => {};
  const original = await f.manager.prepare(f.payload);
  f.manager.marker.cancel(original.marker.id);
  await f.manager.start(f.payload, f.sender);
  assert.notEqual(f.markerBegins[0], original.marker.id);
});

test('detector failure invalidates idle markers so a retry restarts folder discovery', async () => {
  const f = fixture({ phoneDragMode: 'marker' }); f.sender.startDrag = () => {};
  const previous = await f.manager.prepare(f.payload);
  await f.manager.onMarkerMessage({ event: 'detector-error', message: 'Folder detector exited.' });
  assert.equal(f.manager.prepared.size, 0);
  assert.equal(f.manager.markerStartup, null);
  assert.ok(f.markerCancelled.includes(previous.marker.id));
  await f.manager.start(f.payload, f.sender);
  assert.notEqual(f.markerBegins[0], previous.marker.id);
  assert.equal(f.manager.jobs()[0].state, 'queued');
});

test('phone drag waits for Shell notification registration before starting the mouse gesture', async () => {
  let register;
  const f = fixture({ phoneDragMode: 'marker', markerRegistration: new Promise(resolve => { register = resolve; }) });
  const localStarts = []; f.sender.startDrag = args => localStarts.push(args);
  const starting = f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(localStarts.length, 0);
  assert.equal(f.manager.prepared.size, 0);
  register(); await starting;
  assert.equal(f.shellGestures.length, 1);
  assert.equal(f.markerBegins.length, 1);
});

test('failed marker registration removes its source without creating a frozen transfer', async () => {
  let rejectRegistration;
  const f = fixture({ phoneDragMode: 'marker', markerRegistration: new Promise((_resolve, reject) => { rejectRegistration = reject; }) });
  let attempted = false; f.sender.startDrag = () => { attempted = true; };
  const failed = assert.rejects(f.manager.start(f.payload, f.sender), /Shell registration failed/);
  await new Promise(resolve => setImmediate(resolve));
  rejectRegistration(new Error('Shell registration failed')); await failed;
  assert.equal(attempted, false);
  assert.equal(f.manager.jobs().length, 0);
  assert.equal(f.manager.prepared.size, 0);
  assert.equal(f.markerCancelled.length, 1);
});

test('missing drop receipt waits without phone IO then starts after manual folder confirmation', async () => {
  const f = fixture({ phoneDragMode: 'marker' }); f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  const id = f.manager.jobs()[0].id;
  f.manager.watchdogs.get(id).callback();
  const selector = f.manager.jobs()[0];
  assert.equal(selector.state, 'queued');
  assert.equal(selector.awaitingDestination, true);
  assert.match(selector.message, /Choose folder/);
  assert.equal(f.downloads.length, 0);
  assert.equal(f.queues.length, 0);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.manager.marker.has(f.markerBegins[0]), true, 'Showing the folder picker must not delete the drag source.');
  assert.equal(f.markerCancelled.includes(f.markerBegins[0]), false);
  assert.equal(await f.manager.setDestination(id, path.dirname(__filename)), true);
  assert.equal(f.queues.length, 1);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  assert.equal(f.ticketCleanup[0].basename, '_EyeCopy_drop_00000000000000000000000000000001.txt', 'The original private ticket survives the detection timeout.');
  assert.equal(f.downloads.length, 0);
  assert.equal(f.manager.jobs().length, 0);
});

test('a late Explorer receipt after the folder prompt still starts once in the actual dropped folder', async () => {
  const f = fixture({ phoneDragMode: 'marker' }); f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  const id = f.manager.jobs()[0].id;
  f.manager.watchdogs.get(id).callback();
  assert.equal(f.manager.jobs()[0].awaitingDestination, true);
  assert.equal(f.manager.marker.has(f.markerBegins[0]), true);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.queues.length, 1);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  assert.equal(f.queues[0].destination, path.dirname(__filename));
  assert.equal(f.manager.jobs().length, 0);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.queues.length, 1);
});

test('an accepted native Shell drop seeds only an exact ticket check from the verified release point', async () => {
  const f = fixture({ phoneDragMode: 'marker', dropFolderResult: { verified: true, directory: path.dirname(__filename) } });
  const handle = Buffer.alloc(8); handle.writeBigUInt64LE(0x123456789abcdef0n);
  f.sender.testOwnerWindow = { getNativeWindowHandle: () => handle };
  await f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.shellGestures[0].options.sourceWindow, '1311768467463790320');
  assert.equal(f.ticketReleased.length, 1);
  assert.deepEqual(f.folderChecks, [{ x: 10, y: 20 }]);
  assert.equal(f.markerCandidates[0].destination, path.dirname(__filename));
  assert.equal(f.queues.length, 0, 'A folder path alone must never begin phone copying.');
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
});

test('a rejected native Shell drop cancels promptly without querying or starting a destination', async () => {
  const f = fixture({ phoneDragMode: 'marker', shellDrop: { dropped: false, effect: 0, hresult: 0x40101, point: { x: 10, y: 20 } }, dropFolderResult: { verified: true, directory: path.dirname(__filename) } });
  await f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.manager.jobs()[0].state, 'cancelled');
  assert.equal(f.manager.jobs()[0].awaitingDestination, false);
  assert.equal(f.ticketReleased.length, 1);
  assert.equal(f.folderChecks.length, 0);
  assert.equal(f.queues.length, 0);
  assert.equal(f.engineStarted.length, 0);
});

test('a folder lookup completing after Cancel cannot consume a ticket or start a transfer', async () => {
  let resolveFolder;
  const f = fixture({ phoneDragMode: 'marker', dropFolderResult: new Promise(resolve => { resolveFolder = resolve; }) });
  await f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  f.manager.control(f.manager.jobs()[0].id, 'cancel_job');
  resolveFolder({ verified: true, directory: path.dirname(__filename) });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.markerCandidates.length, 0);
  assert.equal(f.queues.length, 0);
});

test('cancel during automatic start stops every acknowledged engine job without starting the rest', async () => {
  let acknowledge;
  const f = fixture({ phoneDragMode: 'marker', downloadDeferred: { jobIds: ['engine-1', 'engine-2'] }, startDeferred: new Promise(resolve => { acknowledge = resolve; }) });
  f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  const id = f.manager.jobs()[0].id;
  const handoff = f.manager.setDestination(id, path.dirname(__filename));
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.engineStarted, ['engine-1']);
  f.manager.control(id, 'cancel_job');
  acknowledge({ changed: true });
  assert.equal(await handoff, false);
  assert.deepEqual([...new Set(f.engineCancelled)], ['engine-1', 'engine-2']);
  assert.equal(f.manager.jobs()[0].state, 'cancelled');
});

test('automatic start failure preserves the existing paused entries without duplicating the selection', async () => {
  const f = fixture({ phoneDragMode: 'marker', failStart: true }); f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  assert.equal(await f.manager.setDestination(f.manager.jobs()[0].id, path.dirname(__filename)), true);
  assert.equal(f.queues.length, 1);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  assert.equal(f.manager.jobs().length, 0);
  assert.match(f.logs.at(-2), /remaining paused entry/);
});

test('cancel during ticket cleanup does not create or start a transfer', async () => {
  let cleaned;
  const f = fixture({ phoneDragMode: 'marker', cleanupDeferred: new Promise(resolve => { cleaned = resolve; }) }); f.sender.startDrag = () => {};
  await f.manager.start(f.payload, f.sender);
  const id = f.manager.jobs()[0].id;
  const handoff = f.manager.setDestination(id, path.dirname(__filename));
  f.manager.control(id, 'cancel_job'); cleaned(true);
  assert.equal(await handoff, false);
  assert.equal(f.queues.length, 0);
  assert.equal(f.engineStarted.length, 0);
});

test('clearing completed drag listings preserves pending requests and prevents late callbacks from reviving removed rows', async () => {
  const f = fixture({ phoneDragMode: 'marker' });
  await f.manager.start(f.payload, f.sender);
  const firstId = f.manager.jobs()[0].id;
  f.manager.control(firstId, 'cancel_job');
  await f.manager.start(f.payload, f.sender);
  const secondId = f.manager.jobs()[1].id;
  f.manager.clearFinished();
  assert.deepEqual(Array.from(f.manager.jobs(), job => job.id), [secondId]);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.queues.length, 0);
  assert.equal(f.engineStarted.length, 0);
});

test('prewarming again discards a deleted cached local original and prepares a phone ticket without file IO', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  f.payload.items[0].localPath = original;
  const cached = await f.manager.prepare(f.payload);
  assert.deepEqual(Array.from(cached.localPaths), [original]);
  fs.unlinkSync(original);
  const current = await f.manager.prepare(f.payload);
  assert.notEqual(current, cached);
  assert.equal(current.localPaths, undefined);
  assert.ok(current.marker);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.queues.length, 0);
  assert.equal(f.shellGestures.length, 0);
});

test('deletion between preparation and start selects the phone route and waits for an exact drop receipt', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  let localStarted = false; f.sender.startDrag = () => { localStarted = true; };
  f.payload.items[0].localPath = original;
  await f.manager.prepare(f.payload);
  fs.unlinkSync(original);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarted, false);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(f.requestCount(), 0, 'Recovering a missing local original must not open an original stream.');
  assert.equal(f.queues.length, 0, 'No iPhone job is created before receipt of the exact ticket.');
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.queues.length, 1);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
});

test('a Live Photo with one deleted local companion transfers both authoritative originals by phone ticket', async t => {
  const [stillPath, motionPath] = temporaryOriginals(t, 2);
  const still = { id: 'still', deviceId: 'phone-1', kind: 'photo', name: 'live.jpg', size: 10, modified: 123, localPath: stillPath };
  const motion = { id: 'motion', deviceId: 'phone-1', kind: 'video', name: 'live.mov', size: 20, modified: 123, localPath: motionPath };
  const f = fixture({ phoneDragMode: 'marker', expandedItems: [still, motion] });
  let localStarted = false; f.sender.startDrag = () => { localStarted = true; };
  f.payload.items = [{ ...still, livePhoto: { verified: true, video: { ...motion, id: 'renderer-forged' } } }];
  const cached = await f.manager.prepare(f.payload);
  assert.equal(cached.localPaths.length, 2);
  fs.unlinkSync(motionPath);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarted, false);
  const selector = f.manager.selection(f.manager.jobs()[0].id);
  assert.deepEqual(Array.from(selector.items, item => item.id), ['still', 'motion']);
  assert.equal(selector.total, 30);
  assert.ok(fs.existsSync(stillPath), 'The surviving original must remain untouched.');
  assert.equal(f.requestCount(), 0);
  assert.equal(f.queues.length, 0);
});

test('a deleted local original with an offline phone fails before capture handoff or a native Shell gesture', async t => {
  const [original] = temporaryOriginals(t);
  const offlineDeviceIds = [];
  const f = fixture({ phoneDragMode: 'marker', offlineDeviceIds });
  f.payload.items[0].localPath = original;
  await f.manager.prepare(f.payload);
  fs.unlinkSync(original); offlineDeviceIds.push('phone-1');
  await assert.rejects(f.manager.start(f.payload, f.sender), /local original.*no longer available.*iPhone is offline/);
  assert.equal(f.localCaptures.length, 0);
  assert.equal(f.shellGestures.length, 0);
  assert.equal(f.ticketPrepared.length, 0);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.manager.prepared.size, 0);
  assert.equal(f.manager.jobs().length, 0);
});

test('existing local originals stay usable offline and repeated starts reuse the fast local preparation', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker', offlineDeviceIds: ['phone-1'] });
  const localStarts = []; f.sender.startDrag = args => localStarts.push(args);
  f.payload.items[0].localPath = original;
  await f.manager.prepare(f.payload);
  const resolutions = f.resolutionCount();
  await f.manager.start(f.payload, f.sender);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarts.length, 2);
  assert.deepEqual(Array.from(localStarts[0].files), [original]);
  assert.equal(f.resolutionCount(), resolutions, 'An existing local route needs no connection lookup while starting.');
  assert.equal(f.shellGestures.length, 0);
  assert.equal(f.requestCount(), 0);
});

test('deletion during source capture handoff recovers once instead of passing a missing path to the native helper', async t => {
  const [original] = temporaryOriginals(t);
  let deleted = false;
  const f = fixture({ phoneDragMode: 'marker', beforeLocalCapture: () => { if (!deleted) { deleted = true; fs.unlinkSync(original); } } });
  let localStarted = false; f.sender.startDrag = () => { localStarted = true; };
  f.payload.items[0].localPath = original;
  await f.manager.prepare(f.payload);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarted, false);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.queues.length, 0);
});

test('a cached ticket is discarded when its missing local original phone becomes offline before the gesture', async t => {
  const [original] = temporaryOriginals(t);
  fs.unlinkSync(original);
  const offlineDeviceIds = [];
  const f = fixture({ phoneDragMode: 'marker', offlineDeviceIds });
  f.payload.items[0].localPath = original;
  const cached = await f.manager.prepare(f.payload);
  assert.ok(cached.marker);
  offlineDeviceIds.push('phone-1');
  await assert.rejects(f.manager.start(f.payload, f.sender), /iPhone is offline/);
  assert.equal(f.localCaptures.length, 0);
  assert.equal(f.shellGestures.length, 0);
  assert.ok(f.markerCancelled.includes(cached.marker.id));
  assert.equal(f.ticketReleased.length, 1);
  assert.equal(f.manager.prepared.size, 0);
  assert.equal(f.manager.jobs().length, 0);
});

test('authoritative download completion promotes a prewarmed phone ticket to its local original without renderer changes', async t => {
  const [original] = temporaryOriginals(t);
  const authoritative = { id: 'iphone-original-1', deviceId: 'phone-1', name: 'large.mov', size: 1000, modified: 123456, kind: 'video' };
  const f = fixture({ phoneDragMode: 'marker', expandedItems: [authoritative] });
  const localStarts = []; f.sender.startDrag = args => localStarts.push(args);
  const cached = await f.manager.prepare(f.payload);
  assert.ok(cached.marker);
  authoritative.localPath = original;
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarts.length, 1);
  assert.deepEqual(Array.from(localStarts[0].files), [original]);
  assert.ok(f.markerCancelled.includes(cached.marker.id));
  assert.equal(f.ticketReleased.length, 1);
  assert.equal(f.shellGestures.length, 0);
  assert.equal(f.requestCount(), 0);
});

test('a local path without an iPhone identity cannot be dragged after deletion', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  fs.unlinkSync(original);
  await assert.rejects(f.manager.start(original, f.sender), /local original.*no longer available.*iPhone library/);
  assert.equal(f.localCaptures.length, 0);
  assert.equal(f.shellGestures.length, 0);
  assert.equal(f.queues.length, 0);
});

test('a synchronous native missing-file rejection recovers through the phone ticket instead of repeating the stale path', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  let localAttempts = 0;
  f.sender.startDrag = () => { localAttempts++; fs.unlinkSync(original); throw new Error('Native source file is missing.'); };
  f.payload.items[0].localPath = original;
  await f.manager.prepare(f.payload);
  await f.manager.start(f.payload, f.sender);
  assert.equal(localAttempts, 1);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.queues.length, 0);
});

test('an unrelated native local-drag rejection retains its explanation and does not start a phone transfer', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  f.sender.startDrag = () => { throw new Error('Native window no longer exists.'); };
  f.payload.items[0].localPath = original;
  await assert.rejects(f.manager.start(f.payload, f.sender), /Native window no longer exists/);
  assert.ok(fs.existsSync(original));
  assert.equal(f.shellGestures.length, 0);
  assert.equal(f.requestCount(), 0);
  assert.equal(f.queues.length, 0);
});

test('changing the permanent default invalidates a warm local drag even while its old file exists', async t => {
  const [original] = temporaryOriginals(t);
  const authoritative = { id: 'default-library-item', deviceId: 'phone-1', name: 'original.mov', kind: 'video', size: 30, modified: 123, localPath: original };
  const f = fixture({ phoneDragMode: 'marker', expandedItems: [authoritative] });
  f.payload.items = [{ ...authoritative }];
  let localStarted = false; f.sender.startDrag = () => { localStarted = true; };
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.dirname(original) }], catalogue: [authoritative] });
  const cached = await f.manager.prepare(f.payload);
  assert.deepEqual(Array.from(cached.localPaths), [original]);
  delete authoritative.localPath;
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.join(path.dirname(original), 'new-default') }], catalogue: [authoritative] });
  assert.equal(fs.existsSync(original), true, 'Changing the default never moves or deletes the previous copy.');
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarted, false);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(f.requestCount(), 0, 'A new phone ticket does not predownload bytes.');
});

test('a refreshed removed default-copy badge invalidates idle local preparations without deleting the file', async t => {
  const [original] = temporaryOriginals(t);
  const authoritative = { id: 'edited-library-item', deviceId: 'phone-1', name: 'original.mov', kind: 'video', size: 30, modified: 123, localPath: original };
  const f = fixture({ phoneDragMode: 'marker', expandedItems: [authoritative] });
  f.payload.items = [{ ...authoritative }];
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.dirname(original) }], catalogue: [authoritative] });
  await f.manager.prepare(f.payload);
  delete authoritative.localPath;
  f.manager.observeState({ catalogue: [authoritative] });
  f.sender.startDrag = () => assert.fail('An edited/unverified old copy must not be used as the phone original.');
  await f.manager.start(f.payload, f.sender);
  assert.equal(f.shellGestures.length, 1);
  assert.equal(fs.existsSync(original), true);
});

test('unchanged authoritative state and another phone folder preserve the warm local preparation', async t => {
  const [original] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  f.payload.items[0].localPath = original;
  const state = { devices: [{ id: 'phone-1', defaultDownloadDir: path.dirname(original) }], catalogue: f.payload.items };
  f.manager.observeState(state);
  await f.manager.prepare(f.payload);
  const resolutions = f.resolutionCount();
  f.manager.observeState(state);
  f.manager.observeState({ devices: [{ id: 'different-phone', defaultDownloadDir: path.join(path.dirname(original), 'other') }] });
  let localStarted = false; f.sender.startDrag = () => { localStarted = true; };
  await f.manager.start(f.payload, f.sender);
  assert.equal(localStarted, true);
  assert.equal(f.resolutionCount(), resolutions, 'The unchanged warm local route adds no asynchronous lookup at gesture handoff.');
});

test('an old local resolution completing after a default change cannot repopulate the warm cache', async t => {
  const [original] = temporaryOriginals(t);
  const oldItem = { id: 'racing-library-item', deviceId: 'phone-1', name: 'original.mov', kind: 'video', size: 30, modified: 123, localPath: original };
  const currentItem = { ...oldItem }; delete currentItem.localPath;
  let resolveOld;
  const firstReply = new Promise(resolve => { resolveOld = resolve; });
  const f = fixture({ phoneDragMode: 'marker', resolveItemsHook: (_args, count) => count === 1 ? firstReply : { items: [currentItem], offlineDeviceIds: [] } });
  f.payload.items = [{ ...oldItem }];
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.dirname(original) }], catalogue: [oldItem] });
  const preparing = f.manager.prepare(f.payload);
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.join(path.dirname(original), 'new-default') }], catalogue: [currentItem] });
  resolveOld({ items: [oldItem], offlineDeviceIds: [] });
  const prepared = await preparing;
  assert.equal(prepared.localPaths, undefined);
  assert.ok(prepared.marker);
  assert.equal(f.resolutionCount(), 2);
  assert.equal(f.requestCount(), 0);
});

test('completed external queue paths never promote an On iPhone item or disturb its current target drop', async t => {
  const [external] = temporaryOriginals(t);
  const f = fixture({ phoneDragMode: 'marker' });
  f.manager.observeState({ devices: [{ id: 'phone-1', defaultDownloadDir: path.join(path.dirname(external), 'default-library') }], catalogue: f.payload.items,
    jobs: [{ id: 'external-completed', deviceId: 'phone-1', state: 'completed', direction: 'download', localPath: external }] });
  f.sender.startDrag = () => assert.fail('Completed external queue history is not a default-library copy.');
  await f.manager.start(f.payload, f.sender);
  const markerId = f.markerBegins[0];
  await f.manager.onMarkerMessage({ event: 'dropped', id: markerId, destination: path.dirname(external), markerConsumed: true });
  assert.equal(f.queues.length, 1);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  assert.equal(f.manager.jobs().length, 0);
});

test('Stop all revokes a late native gesture result before it can select a destination', async () => {
  let finish;
  const f = fixture({ phoneDragMode: 'marker', shellDrop: new Promise(resolve => { finish = resolve; }) });
  const starting = f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.shellGestures.length, 1);
  const stopped = f.manager.cancelAll();
  assert.equal(stopped.count, 1);
  assert.equal(f.manager.jobs()[0].state, 'cancelled');
  finish({ dropped: true, point: { x: 5, y: 5 } });
  await starting;
  assert.equal(f.queues.length, 0);
  assert.equal(f.folderChecks.length, 0);
});

test('destination acknowledgement before asynchronous Shell return cannot recreate its completed handoff', async () => {
  let finish;
  const f = fixture({ phoneDragMode: 'marker', shellDrop: new Promise(resolve => { finish = resolve; }) });
  const starting = f.manager.start(f.payload, f.sender);
  await new Promise(resolve => setImmediate(resolve));
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.manager.jobs().length, 0);
  assert.deepEqual(f.engineStarted, ['engine-copy-1']);
  finish({ dropped: true, point: { x: 5, y: 5 } });
  await starting;
  assert.equal(f.manager.watchdogs.size, 0);
  assert.equal(f.folderChecks.length, 0);
  assert.equal(f.manager.jobs().length, 0);
});

test('Stop all invalidates a late preparation without preventing a fresh selection', async () => {
  let answer;
  const delayed = new Promise(resolve => { answer = resolve; });
  const f = fixture({ resolveItemsHook: (args, count) => count === 1 ? delayed : { items: args.items, offlineDeviceIds: [] } });
  const previous = f.manager.prepare(f.payload);
  const rejected = assert.rejects(previous, /cancelled/);
  f.manager.cancelAll();
  answer({ items: f.payload.items, offlineDeviceIds: [] });
  await rejected;
  assert.equal(f.manager.prepared.size, 0);
  const fresh = await f.manager.prepare(f.payload);
  assert.equal(fresh.files.length, 1);
  assert.equal(f.requestCount(), 1, 'The revoked preparation must not open a phone stream.');
});

test('Clear all retains a stopped pending engine acknowledgement until its new jobs are cancelled', async () => {
  let acknowledge;
  const f = fixture({ phoneDragMode: 'marker', downloadDeferred: new Promise(resolve => { acknowledge = resolve; }) });
  await f.manager.start(f.payload, f.sender);
  const handoff = f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  await new Promise(resolve => setImmediate(resolve));
  f.manager.cancelAll();
  const retained = f.manager.clearAll();
  assert.equal(retained.blocked, true); assert.equal(retained.retained, 1);
  acknowledge({ jobIds: ['late-new-job'] });
  await handoff;
  assert.deepEqual(f.engineCancelled, ['late-new-job']);
  assert.deepEqual(f.engineStarted, []);
  assert.equal(f.manager.clearAll().cleared, 1);
  assert.equal(f.manager.jobs().length, 0);
});

test('Clear all includes deferred folder choices only after Stop all has revoked their tickets', async () => {
  const f = fixture({ phoneDragMode: 'marker' });
  await f.manager.start(f.payload, f.sender);
  assert.equal(f.manager.clearAll().blocked, true);
  assert.equal(f.manager.jobs().length, 1);
  f.manager.cancelAll();
  assert.ok(f.markerCancelled.includes(f.markerBegins[0]));
  assert.equal(f.manager.clearAll().cleared, 1);
  await f.manager.onMarkerMessage({ event: 'dropped', id: f.markerBegins[0], destination: path.dirname(__filename), markerConsumed: true });
  assert.equal(f.queues.length, 0);
});
