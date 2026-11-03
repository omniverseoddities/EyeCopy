'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { createLocalTicketDrag, loadShellBindings, createNativeTicketReader } = require('./local-ticket');

function nativePrepareProbe() {
  const appData = process.argv.includes('--appdata');
  const directory = appData ? path.resolve(__dirname, '../../test-output/local-ticket-prepare/appdata-layout/EyeCopy', crypto.randomUUID()) : path.resolve(__dirname, '../../test-output/local-ticket-prepare', crypto.randomUUID());
  fs.mkdirSync(directory, { recursive: true });
  const nonce = crypto.randomBytes(16).toString('hex');
  const file = path.join(directory, `_EyeCopy_drop_${nonce}.txt`);
  const payload = Buffer.from(`EyeCopy transfer ticket v1\n${nonce}\n`, 'ascii');
  fs.writeFileSync(file, payload);
  let initialized = 0, uninitialized = 0, parsed = 0, freed = 0, released = 0, data = 0n;
  const real = loadShellBindings();
  const api = { ...real,
    oleInitialize(...args) { initialized++; return real.oleInitialize(...args); },
    oleUninitialize() { uninitialized++; real.oleUninitialize(); },
    parsePIDL(value) { parsed++; return real.parsePIDL(value); },
    freePIDL(value) { freed++; real.freePIDL(value); },
    createDataObject(...args) { const result = real.createDataObject(...args); data = result.dataObject; return result; },
    releaseDataObject(value) { released++; return real.releaseDataObject(value); },
    dragDrop() { throw new Error('Native diagnostic must never enter a mouse drag.'); },
  };
  const adapter = createLocalTicketDrag({ getBindings: () => api });
  const prepared = adapter.prepare(file);
  assert.ok(data !== 0n);
  assert.deepEqual(real.readLocalFile(file).contents, fs.readFileSync(file));
  assert.equal(prepared.sourceValidation.nativeReadable, true);
  assert.equal(prepared.sourceValidation.noncePayloadVerified, true);
  // Query the real Shell object, without clipboard access or a drag. Its
  // filesystem format must contain exactly the existing diagnostic ticket.
  const koffi = require('koffi'), pointerSize = koffi.sizeof('void *');
  const getDataType = koffi.proto('__stdcall', 'EyeCopyTicketDiagnosticGetData', 'int32_t', ['void *', 'void *', 'void *']);
  const format = Buffer.alloc(pointerSize === 8 ? 32 : 20);
  format.writeUInt16LE(15, 0); // CF_HDROP; DVASPECT_CONTENT=1; TYMED_HGLOBAL=1.
  format.writeUInt32LE(1, pointerSize === 8 ? 16 : 8);
  format.writeInt32LE(-1, pointerSize === 8 ? 20 : 12);
  format.writeUInt32LE(1, pointerSize === 8 ? 24 : 16);
  const ole = koffi.load('ole32.dll'), shell = koffi.load('shell32.dll');
  const releaseMedium = ole.func('__stdcall', 'ReleaseStgMedium', 'void', ['void *']);
  function verifyDataObject(pointer) {
    const vtable = koffi.decode(pointer, 'void *'), getData = koffi.decode(vtable, 3 * pointerSize, 'void *');
    const medium = Buffer.alloc(pointerSize === 8 ? 24 : 12);
    assert.equal(koffi.call(getData, getDataType, pointer, format, medium), 0);
    try {
    assert.equal(medium.readUInt32LE(0), 1);
    const drop = pointerSize === 8 ? medium.readBigUInt64LE(8) : BigInt(medium.readUInt32LE(4));
    const queryFile = shell.func('__stdcall', 'DragQueryFileW', 'uint32_t', ['void *', 'uint32_t', 'void *', 'uint32_t']);
    assert.equal(queryFile(drop, 0xffffffff, null, 0), 1);
    const characters = queryFile(drop, 0, null, 0), filename = Buffer.alloc((characters + 1) * 2);
    assert.equal(queryFile(drop, 0, filename, characters + 1), characters);
    assert.equal(filename.toString('utf16le', 0, characters * 2).toLowerCase(), file.toLowerCase());
    } finally { releaseMedium(medium); }
  }
  verifyDataObject(data);
  assert.equal(adapter.release(prepared), true);
  assert.equal(adapter.release(prepared), false);
  assert.equal(initialized, 1); assert.equal(uninitialized, 1);
  assert.equal(parsed, 2); assert.equal(freed, 2); assert.equal(released, 1);
  assert.ok(fs.existsSync(file)); // The adapter never removes/copies a ticket.
  // A separate raw Shell object proves its first CF_HDROP query remains exact
  // after the caller's input PIDLs are gone. No actual mouse/copy is invoked.
  assert.ok(real.oleInitialize(null) >= 0);
  let secondObject = 0n;
  const inputPIDLs = [];
  try {
    const parent = real.parsePIDL(path.dirname(file)), child = real.parsePIDL(file);
    inputPIDLs.push(parent.pidl, child.pidl);
    assert.equal(parent.hresult, 0); assert.equal(child.hresult, 0);
    const object = real.createDataObject(parent.pidl, real.lastID(child.pidl));
    secondObject = object.dataObject; assert.equal(object.hresult, 0);
    for (const pidl of inputPIDLs.splice(0)) real.freePIDL(pidl);
    verifyDataObject(secondObject);
  } finally {
    if (secondObject) real.releaseDataObject(secondObject);
    for (const pidl of inputPIDLs) if (pidl) real.freePIDL(pidl);
    real.oleUninitialize();
  }
  console.log(JSON.stringify({ passed: true, sourceKind: appData ? 'workspace-appdata-layout' : 'workspace', nodeSourceExists: fs.existsSync(file), nativeReadable: true, nativeNoncePayloadMatchesNode: true, nativeSourceSize: payload.length, realShellDataObject: true, exactCFHDropTicket: true, shellObjectRetainsPathAfterInputPIDLsFreed: true, balancedOle: true, ownedPIDLsFreed: true, nativeDragCalled: false, clipboardUntouched: true, phoneDataUntouched: true, nodeVersion: process.versions.node, koffiVersion: koffi.version }));
}

if (process.argv.includes('--native-prepare-probe')) {
  nativePrepareProbe();
} else {
  const test = require('node:test');
  const file = 'C:\\EyeCopy\\marker-sources\\_EyeCopy_drop_0123456789abcdef0123456789abcdef.txt';
  function fixture({ initResult = 0, parseFailure = 0, dataFailure = false, lastIDMissing = false, dataThrown = false, releaseThrown = false, dropThrown = false, hresult = 0x40100, effect = 1, sourceOwned = true, foreground = true, held = true, pointAvailable = true, nativeSourceFailure = 0, wrongNativePayload = false, payloadBrand = null } = {}) {
    const source = 9007199254741001n, dataObject = 9007199254741010n;
    let thread = 88, parseCount = 0, changed = false, nativeReads = 0;
    const calls = [], pidls = [];
    const api = {
      readLocalFile(value) {
        calls.push(['native-read', value]);
        if (++nativeReads === nativeSourceFailure) throw new Error('Windows cannot read the drag ticket: CreateFileW failed (Win32 2).');
        const match = /^_(EyeCopy)_drop_([0-9a-f]{32})\.txt$/.exec(path.win32.basename(value));
        const contents = Buffer.from(`${payloadBrand || match[1]} transfer ticket v1\n${match[2]}\n`, 'ascii');
        if (wrongNativePayload) contents[0] = 0;
        return { attributes: 128, size: contents.length, contents };
      },
      threadId: () => thread,
      oleInitialize(value) { calls.push(['initialize', value]); return initResult; },
      oleUninitialize() { calls.push(['uninitialize']); },
      parsePIDL(value) { calls.push(['parse', value]); const count = ++parseCount, pidl = BigInt(100 + count); pidls.push(pidl); return { hresult: count === parseFailure ? -2147467259 : 0, pidl }; },
      freePIDL(value) { calls.push(['free', value]); },
      lastID(value) { calls.push(['lastID', value]); return lastIDMissing ? 0n : value + 8n; },
      createDataObject(parent, child) { calls.push(['create', parent, child]); if (dataThrown) throw new Error('Object creation threw'); return { hresult: dataFailure ? -2147467259 : 0, dataObject }; },
      releaseDataObject(value) { calls.push(['release', value]); if (releaseThrown) throw new Error('Release threw'); },
      processFor: () => sourceOwned ? 123 : 999,
      currentWindow: () => foreground ? source : source + 1n,
      keyState: () => held ? -32768 : 0,
      dragDrop(window, object, dropSource, allowedEffect, output) { calls.push(['drag', window, object, dropSource, allowedEffect]); if (dropThrown) throw new Error('Native drag threw'); output.writeUInt32LE(effect); return hresult; },
      cursorPosition() { calls.push(['point']); return pointAvailable ? { x: -1200, y: 800 } : null; },
    };
    const adapter = createLocalTicketDrag({ getBindings: () => api, parentPid: 123, stat: () => ({ isFile: () => true, isSymbolicLink: () => false, size: 55n, dev: 1n, ino: 3n, mtimeNs: changed ? 2n : 1n }) });
    return { api, adapter, calls, source, dataObject, pidls, change: () => { changed = true; }, setThread: value => { thread = value; } };
  }
  const named = (fixture, name) => fixture.calls.filter(call => call[0] === name);
  test('Shell COPY uses exact existing file PIDLs, null native source and captures point before release', () => {
    const f = fixture(), prepared = f.adapter.prepare(file);
    assert.deepEqual(named(f, 'parse').map(call => call[1]), [path.win32.dirname(file), file]);
    assert.deepEqual(named(f, 'create')[0].slice(1), [101n, 110n]);
    assert.equal(named(f, 'free').length, 0);
    assert.deepEqual(f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), { dropped: true, effect: 1, hresult: 0x40100, point: { x: -1200, y: 800 }, cancelled: false, sourceValidation: { nativeReadable: true, attributes: 128, size: 60, noncePayloadVerified: true } });
    assert.deepEqual(named(f, 'drag')[0].slice(1), [f.source, f.dataObject, null, 1]);
    const point = f.calls.findIndex(call => call[0] === 'point'), release = f.calls.findIndex(call => call[0] === 'release');
    assert.equal(point + 1, release);
    assert.deepEqual(named(f, 'free').map(call => call[1]), [101n, 102n]);
    assert.equal(named(f, 'release').length, 1); assert.equal(named(f, 'uninitialize').length, 1);
    assert.equal(f.adapter.release(prepared), false);
    assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), /fresh/);
  });
  for (const options of [{ hresult: 0x40101, effect: 1 }, { hresult: 0, effect: 1 }, { hresult: -2147467259, effect: 1 }, { effect: 0 }, { effect: 2 }, { effect: 4 }, { effect: 3 }]) test(`does not accept failed/cancelled/non-copy native result ${JSON.stringify(options)}`, () => {
    const f = fixture(options), prepared = f.adapter.prepare(file);
    assert.equal(f.adapter.drag(prepared, { sourceWindow: f.source.toString() }).dropped, false);
    assert.equal(named(f, 'release').length, 1); assert.equal(named(f, 'uninitialize').length, 1);
  });
  test('SCROLL|COPY is accepted and failed cursor retrieval is explicit', () => {
    const f = fixture({ effect: 0x80000001, pointAvailable: false });
    const result = f.adapter.drag(f.adapter.prepare(file), { sourceWindow: f.source.toString() });
    assert.equal(result.dropped, true); assert.equal(result.point, null);
  });
  test('S_FALSE OLE initialization is balanced exactly once', () => {
    const f = fixture({ initResult: 1 }), prepared = f.adapter.prepare(file);
    f.adapter.release(prepared); f.adapter.release(prepared);
    assert.equal(named(f, 'initialize').length, 1); assert.equal(named(f, 'uninitialize').length, 1);
  });
  test('changed COM apartment is refused without uninitializing somebody else\'s COM', () => {
    const f = fixture({ initResult: -2147417850 });
    assert.throws(() => f.adapter.prepare(file), /0x80010106/);
    assert.equal(named(f, 'parse').length, 0); assert.equal(named(f, 'uninitialize').length, 0);
  });
  for (const options of [{ parseFailure: 1 }, { parseFailure: 2 }, { lastIDMissing: true }, { dataFailure: true }, { dataThrown: true }]) test(`partial preparation failure frees only owned resources ${JSON.stringify(options)}`, () => {
    const f = fixture(options);
    assert.throws(() => f.adapter.prepare(file));
    assert.equal(named(f, 'uninitialize').length, 1);
    assert.deepEqual(named(f, 'free').map(call => call[1]), f.pidls);
    assert.equal(named(f, 'release').length, options.dataFailure ? 1 : 0);
  });
  for (const options of [{ sourceOwned: false }, { foreground: false }, { held: false }]) test(`expired or foreign gestures never enter native loop ${JSON.stringify(options)}`, () => {
    const f = fixture(options), prepared = f.adapter.prepare(file);
    assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), /held/);
    assert.equal(named(f, 'drag').length, 0); assert.equal(named(f, 'release').length, 1);
  });
  test('changed ticket and invalid HWND refuse the drag and release preparation', () => {
    const f = fixture(), prepared = f.adapter.prepare(file); f.change();
    assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), /changed/);
    const invalid = fixture();
    assert.throws(() => invalid.adapter.drag(invalid.adapter.prepare(file), { sourceWindow: '0' }), /Invalid/);
    assert.equal(named(f, 'drag').length, 0); assert.equal(named(invalid, 'drag').length, 0);
    assert.equal(named(f, 'release').length, 1); assert.equal(named(invalid, 'release').length, 1);
  });
  test('native inaccessible source is distinguished from Node existence before any Shell parsing', () => {
    const f = fixture({ nativeSourceFailure: 1 });
    assert.throws(() => f.adapter.prepare(file), /Windows cannot read.*CreateFileW.*Win32 2/);
    assert.equal(named(f, 'parse').length, 0); assert.equal(named(f, 'initialize').length, 0);
  });
  test('native source disappearance after preparation prevents drag and releases resources', () => {
    const f = fixture({ nativeSourceFailure: 2 }), prepared = f.adapter.prepare(file);
    assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), /CreateFileW.*Win32 2/);
    assert.equal(named(f, 'drag').length, 0); assert.equal(named(f, 'release').length, 1);
    assert.equal(named(f, 'free').length, 2); assert.equal(named(f, 'uninitialize').length, 1);
  });
  test('a native read of a different nonce payload cannot enter the Shell pipeline', () => {
    const f = fixture({ wrongNativePayload: true });
    assert.throws(() => f.adapter.prepare(file), /native file bytes do not match/);
    assert.equal(named(f, 'parse').length, 0); assert.equal(named(f, 'initialize').length, 0);
  });
  test('native loop exceptions and COM Release exceptions still balance PIDLs/OLE', () => {
    for (const options of [{ dropThrown: true }, { releaseThrown: true }]) {
      const f = fixture(options), prepared = f.adapter.prepare(file);
      assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }));
      assert.equal(named(f, 'free').length, 2); assert.equal(named(f, 'uninitialize').length, 1);
      assert.equal(f.adapter.release(prepared), false);
    }
  });
  test('cross-thread use cannot free apartment-owned objects and correct thread can close afterward', () => {
    const f = fixture(), prepared = f.adapter.prepare(file); f.setThread(99);
    assert.throws(() => f.adapter.drag(prepared, { sourceWindow: f.source.toString() }), /owning UI thread/);
    assert.throws(() => f.adapter.release(prepared), /owning UI thread/);
    assert.equal(named(f, 'release').length, 0);
    f.setThread(88); assert.equal(f.adapter.release(prepared), true);
  });
  test('a failing cleanup operation cannot skip the remaining owned PIDL or OLE balance', () => {
    const f = fixture(), prepared = f.adapter.prepare(file);
    const free = f.api.freePIDL;
    f.api.freePIDL = value => { free(value); if (value === 101n) throw new Error('First free threw'); };
    assert.throws(() => f.adapter.release(prepared), /First free threw/);
    assert.deepEqual(named(f, 'free').map(call => call[1]), [101n, 102n]);
    assert.equal(named(f, 'uninitialize').length, 1);
    assert.equal(f.adapter.release(prepared), false);
  });
  test('only bounded ordinary local EyeCopy tickets can be prepared', () => {
    const f = fixture();
    for (const bad of ['C:\\photos\\original.mov', '\\\\server\\share\\' + path.win32.basename(file), 'relative.txt', file + ':ads', file + '\0']) assert.throws(() => f.adapter.prepare(bad));
    assert.equal(named(f, 'initialize').length, 0);
    for (const [isFile, isSymbolicLink, size] of [[false, false, 55n], [true, true, 55n], [true, false, 129n]]) {
      const adapter = createLocalTicketDrag({ getBindings: () => { throw new Error('Invalid tickets must not load Shell'); }, stat: () => ({ isFile: () => isFile, isSymbolicLink: () => isSymbolicLink, size }) });
      assert.throws(() => adapter.prepare(file), /ordinary small/);
    }
  });
  test('an unrelated ticket name is rejected before loading Windows drag APIs', () => {
    const other = file.replaceAll('EyeCopy', 'OtherApplication'), f = fixture();
    assert.throws(() => f.adapter.prepare(other), /Only an EyeCopy transfer ticket/);
    assert.equal(named(f, 'initialize').length, 0);
  });
  function readerFixture({ attributes = 0x80, handle = 123n, infoSucceeds = true, handleAttributes = 0x80, size = 62, readSucceeds = true, readCount = 62 } = {}) {
    const calls = [], native = {
      invalidHandle: 0xffffffffffffffffn,
      fileAttributes(value) { calls.push(['attributes', value]); return attributes; },
      openFile(...args) { calls.push(['open', ...args]); return handle; },
      fileInformation(value, output) { calls.push(['information', value]); output.writeUInt32LE(handleAttributes, 0); output.writeUInt32LE(size, 36); return infoSucceeds ? 1 : 0; },
      readFile(value, output, limit, count, overlapped) { calls.push(['read', value, limit, overlapped]); output.fill(65); count.writeUInt32LE(readCount); return readSucceeds ? 1 : 0; },
      closeFile(value) { calls.push(['close', value]); return 1; },
      lastError() { return 5; },
    };
    return { calls, read: createNativeTicketReader(native) };
  }
  test('native ticket read is bounded read-only OPEN_EXISTING with all sharing and closes handle', () => {
    const f = readerFixture(), result = f.read(file);
    assert.equal(result.size, 62); assert.equal(result.contents.length, 62);
    assert.deepEqual(f.calls.find(call => call[0] === 'open'), ['open', file, 0x80000000, 7, null, 3, 0x00200080, 0n]);
    assert.deepEqual(f.calls.find(call => call[0] === 'read'), ['read', 123n, 128, null]);
    assert.deepEqual(f.calls.at(-1), ['close', 123n]);
  });
  for (const options of [{ attributes: 0xffffffff }, { attributes: 0x410 }, { handle: 0xffffffffffffffffn }]) test(`native attribute/open failure cannot leak a handle ${JSON.stringify(options, (_, v) => typeof v === 'bigint' ? String(v) : v)}`, () => {
    const f = readerFixture(options); assert.throws(() => f.read(file), /Windows cannot read/);
    assert.equal(f.calls.filter(call => call[0] === 'close').length, 0);
    assert.equal(f.calls.filter(call => call[0] === 'read').length, 0);
  });
  for (const options of [{ infoSucceeds: false }, { handleAttributes: 0x410 }, { size: 129 }, { readSucceeds: false }, { readCount: 61 }, { readCount: 129 }]) test(`opened native source failure always closes its handle ${JSON.stringify(options)}`, () => {
    const f = readerFixture(options); assert.throws(() => f.read(file), /Windows cannot read/);
    assert.equal(f.calls.filter(call => call[0] === 'close').length, 1);
    assert.deepEqual(f.calls.at(-1), ['close', 123n]);
  });
  test('real Electron ABI prepares exact Shell CF_HDROP and releases without any mouse drag', () => {
    const result = spawnSync(require('electron'), [__filename, '--native-prepare-probe'], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, timeout: 15000, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const report = JSON.parse(result.stdout.trim());
    assert.equal(report.exactCFHDropTicket, true); assert.equal(report.nativeDragCalled, false);
    assert.equal(report.nativeNoncePayloadMatchesNode, true);
    assert.equal(report.shellObjectRetainsPathAfterInputPIDLsFreed, true);
  });
  test('real Electron ticket in an app-data layout is readable through Node and Windows native APIs', () => {
    const result = spawnSync(require('electron'), [__filename, '--native-prepare-probe', '--appdata'], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, timeout: 15000, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const report = JSON.parse(result.stdout.trim());
    assert.equal(report.sourceKind, 'workspace-appdata-layout'); assert.equal(report.nativeReadable, true);
    assert.equal(report.nativeNoncePayloadMatchesNode, true);
    assert.equal(report.nativeDragCalled, false);
    assert.equal(report.shellObjectRetainsPathAfterInputPIDLsFreed, true);
  });
}
