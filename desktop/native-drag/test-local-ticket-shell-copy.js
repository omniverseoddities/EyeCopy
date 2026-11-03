'use strict';
// Isolated protocol diagnostic. A Shell folder target consumes this adapter's
// actual Shell-created IDataObject and copies only our generated 60-byte ticket.
// No DoDragDrop, window targeting, input, clipboard access or iPhone services.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');

function guid(value) {
  const parts = value.split('-'), result = Buffer.alloc(16);
  result.writeUInt32LE(parseInt(parts[0], 16), 0);
  result.writeUInt16LE(parseInt(parts[1], 16), 4);
  result.writeUInt16LE(parseInt(parts[2], 16), 6);
  Buffer.from(parts[3] + parts[4], 'hex').copy(result, 8);
  return result;
}
function checked(hresult, operation) {
  if ((Number(hresult) | 0) < 0) throw new Error(`${operation}: HRESULT 0x${(Number(hresult) >>> 0).toString(16)}`);
  return hresult;
}

async function shellCopyProbe() {
  const koffi = require('koffi');
  const { createLocalTicketDrag, loadShellBindings } = require('./local-ticket');
  const api = loadShellBindings(), shell = koffi.load('shell32.dll'), ole = koffi.load('ole32.dll'), kernel = koffi.load('kernel32.dll'), user = koffi.load('user32.dll');
  const pointerSize = koffi.sizeof('void *');
  const readPointer = (buffer, offset = 0) => pointerSize === 8 ? buffer.readBigUInt64LE(offset) : BigInt(buffer.readUInt32LE(offset));
  const method = (object, slot) => koffi.decode(koffi.decode(object, 'void *'), slot * pointerSize, 'void *');
  const POINT = koffi.struct('EyeCopyTicketShellProbePoint', { x: 'int32_t', y: 'int32_t' });
  const bindType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeBind', 'int32_t', ['void *', 'void *', 'void *', 'void *', 'void *']);
  const enterType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeEnter', 'int32_t', ['void *', 'void *', 'uint32_t', POINT, 'void *']);
  const dropType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeDrop', 'int32_t', ['void *', 'void *', 'uint32_t', POINT, 'void *']);
  const getType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeGetData', 'int32_t', ['void *', 'void *', 'void *']);
  const queryType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeQueryInterface', 'int32_t', ['void *', 'void *', 'void *']);
  const setAsyncType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeSetAsync', 'int32_t', ['void *', 'int']);
  const getAsyncType = koffi.proto('__stdcall', 'EyeCopyTicketShellProbeGetAsync', 'int32_t', ['void *', 'void *']);
  const createItem = shell.func('__stdcall', 'SHCreateItemFromParsingName', 'int32_t', ['str16', 'void *', 'void *', 'void *']);
  const pathFromID = shell.func('__stdcall', 'SHGetPathFromIDListEx', 'int', ['void *', 'void *', 'uint32_t', 'uint32_t']);
  const combineIDs = shell.func('__stdcall', 'ILCombine', 'void *', ['void *', 'void *']);
  const registerFormat = user.func('__stdcall', 'RegisterClipboardFormatW', 'uint32_t', ['str16']);
  const queryFile = shell.func('__stdcall', 'DragQueryFileW', 'uint32_t', ['void *', 'uint32_t', 'void *', 'uint32_t']);
  const lock = kernel.func('__stdcall', 'GlobalLock', 'void *', ['void *']);
  const unlock = kernel.func('__stdcall', 'GlobalUnlock', 'int', ['void *']);
  const sizeOf = kernel.func('__stdcall', 'GlobalSize', 'uintptr_t', ['void *']);
  const releaseMedium = ole.func('__stdcall', 'ReleaseStgMedium', 'void', ['void *']);
  const shellItemIID = guid('43826D1E-E718-42EE-BC55-A1E261C37BFE');
  const targetIID = guid('00000122-0000-0000-C000-000000000046');
  const handler = guid('3981E225-F559-11D3-8E3A-00C04F6837D5');
  const asyncIID = guid('3D8B0590-F691-11D2-8EA9-006097DF5BD4');
  const cidaFormat = registerFormat('Shell IDList Array');
  assert.ok(cidaFormat >= 0xc000);
  const root = path.resolve(__dirname, '../..');
  const directory = path.join(root, 'test-output', 'local-ticket-shell-copy', crypto.randomUUID());
  const sourceDirectory = path.join(directory, 'drag-tickets');
  fs.mkdirSync(directory, { recursive: true }); fs.mkdirSync(sourceDirectory, { recursive: true });
  const cases = [];

  function mediumFor(data, formatId) {
    const format = Buffer.alloc(pointerSize === 8 ? 32 : 20), medium = Buffer.alloc(pointerSize === 8 ? 24 : 12);
    format.writeUInt16LE(formatId, 0);
    format.writeUInt32LE(1, pointerSize === 8 ? 16 : 8);
    format.writeInt32LE(-1, pointerSize === 8 ? 20 : 12);
    format.writeUInt32LE(1, pointerSize === 8 ? 24 : 16);
    checked(koffi.call(method(data, 3), getType, data, format, medium), 'GetData');
    assert.equal(medium.readUInt32LE(0), 1);
    return medium;
  }
  function pathForID(pidl) {
    const text = Buffer.alloc(32768 * 2);
    assert.ok(pathFromID(pidl, text, 32768, 0), 'PIDL must resolve to an existing filesystem path');
    return text.toString('utf16le').split('\0')[0];
  }
  function verifyFormats(data, source) {
    const drop = mediumFor(data, 15);
    let hdropPath, cidaPath, cidaParent;
    try {
      const memory = readPointer(drop, pointerSize === 8 ? 8 : 4);
      assert.equal(queryFile(memory, 0xffffffff, null, 0), 1);
      const characters = queryFile(memory, 0, null, 0), filename = Buffer.alloc((characters + 1) * 2);
      assert.equal(queryFile(memory, 0, filename, characters + 1), characters);
      hdropPath = filename.toString('utf16le', 0, characters * 2);
      assert.equal(hdropPath.toLowerCase(), source.toLowerCase());
      assert.equal(api.readLocalFile(hdropPath).size, 60);
    } finally { releaseMedium(drop); }
    const cida = mediumFor(data, cidaFormat);
    const memory = readPointer(cida, pointerSize === 8 ? 8 : 4);
    let locked;
    try {
      const length = Number(sizeOf(memory));
      assert.ok(length >= 12 && length <= 65536);
      locked = lock(memory); assert.ok(locked);
      const bytes = Buffer.from(koffi.decode(locked, 'uint8_t', length));
      assert.equal(bytes.readUInt32LE(0), 1);
      const parentOffset = bytes.readUInt32LE(4), childOffset = bytes.readUInt32LE(8);
      function validatePIDL(offset, childOnly) {
        assert.ok(offset >= 12 && offset + 2 <= length);
        let items = 0, cursor = offset;
        while (true) {
          assert.ok(cursor + 2 <= length);
          const size = bytes.readUInt16LE(cursor);
          if (size === 0) break;
          assert.ok(size >= 2 && cursor + size <= length);
          cursor += size; items++;
        }
        if (childOnly) assert.equal(items, 1);
      }
      validatePIDL(parentOffset, false); validatePIDL(childOffset, true);
      const parent = BigInt(locked) + BigInt(parentOffset), child = BigInt(locked) + BigInt(childOffset);
      cidaParent = pathForID(parent);
      assert.equal(cidaParent.toLowerCase(), path.dirname(source).toLowerCase());
      const combined = combineIDs(parent, child); assert.ok(combined);
      try { cidaPath = pathForID(combined); } finally { api.freePIDL(combined); }
      assert.equal(cidaPath.toLowerCase(), source.toLowerCase());
      assert.equal(api.readLocalFile(cidaPath).size, 60);
    } finally { if (locked) unlock(memory); releaseMedium(cida); }
    return { cidaParent, cidaPath, hdropPath, nativeSourceReadable: true };
  }
  function configureAsync(data, requested) {
    const result = Buffer.alloc(pointerSize);
    const queryResult = koffi.call(method(data, 0), queryType, data, asyncIID, result);
    if (queryResult < 0) {
      if (requested) checked(queryResult, 'Source asynchronous capability');
      return { supported: false, requested, mode: false };
    }
    const pointer = readPointer(result);
    try {
      if (requested) checked(koffi.call(method(pointer, 3), setAsyncType, pointer, 1), 'Enabling isolated async source');
      const enabled = Buffer.alloc(4);
      checked(koffi.call(method(pointer, 4), getAsyncType, pointer, enabled), 'Reading source async mode');
      return { supported: true, requested, mode: Boolean(enabled.readUInt32LE(0)) };
    } finally { api.releaseDataObject(pointer); }
  }

  checked(api.oleInitialize(null), 'Base OLE initialization');
  try {
    for (const { releaseSourceImmediately, asynchronous, rawDelayed = false } of [{ releaseSourceImmediately: false, asynchronous: false }, { releaseSourceImmediately: true, asynchronous: false }, { releaseSourceImmediately: true, asynchronous: true }, { releaseSourceImmediately: true, asynchronous: false, rawDelayed: true }]) {
      const nonce = crypto.randomBytes(16).toString('hex'), source = path.join(sourceDirectory, `_EyeCopy_drop_${nonce}.txt`);
      const targetDirectory = path.join(directory, rawDelayed ? 'raw-delayed-source-object' : asynchronous ? 'async-released-source-object' : releaseSourceImmediately ? 'released-source-object' : 'retained-source-object');
      const payload = Buffer.from(`EyeCopy transfer ticket v1\n${nonce}\n`, 'ascii');
      assert.equal(payload.length, 60); fs.writeFileSync(source, payload); fs.mkdirSync(targetDirectory);
      const targetFile = path.join(targetDirectory, path.basename(source));
      let data = 0n, item = 0n, target = 0n, sourceReleased = false;
      const adapter = createLocalTicketDrag({ getBindings: () => ({ ...api,
        createDataObject(...args) { const result = api.createDataObject(...args); data = result.dataObject; return result; },
        dragDrop() { throw new Error('Shell copy diagnostic must never invoke a system mouse drag.'); },
      }) });
      const prepared = adapter.prepare(source);
      try {
        const before = rawDelayed ? { formatsNotQueried: true, nativeSourceReadable: api.readLocalFile(source).size === payload.length } : verifyFormats(data, source);
        const asyncSource = rawDelayed ? { formatsNotQueried: true } : configureAsync(data, asynchronous);
        if (rawDelayed) {
          // Unlike GetData warming, this matches a prepared source retained
          // while the user holds/moves the gesture and Node releases temporaries.
          for (let index = 0; index < 48; index++) Buffer.alloc(1024 * 1024, index);
          if (typeof global.gc === 'function') global.gc();
          await new Promise(resolve => setTimeout(resolve, 1000));
          if (typeof global.gc === 'function') global.gc();
        }
        let result = Buffer.alloc(pointerSize);
        checked(createItem(targetDirectory, null, shellItemIID, result), 'Creating scoped target'); item = readPointer(result);
        result = Buffer.alloc(pointerSize);
        checked(koffi.call(method(item, 3), bindType, item, null, handler, targetIID, result), 'Binding real folder drop target'); target = readPointer(result);
        const effect = Buffer.alloc(4); effect.writeUInt32LE(1);
        const enterResult = checked(koffi.call(method(target, 3), enterType, target, data, 1, { x: 0, y: 0 }, effect), 'Shell DragEnter');
        assert.equal(effect.readUInt32LE(0) & 7, 1);
        effect.writeUInt32LE(1);
        const droppedAt = Date.now();
        const dropResult = checked(koffi.call(method(target, 6), dropType, target, data, 0, { x: 0, y: 0 }, effect), 'Shell Drop');
        assert.equal(effect.readUInt32LE(0) & 7, 1);
        const fileExistsOnDropReturn = fs.existsSync(targetFile);
        const after = verifyFormats(data, source);
        if (releaseSourceImmediately) { adapter.release(prepared); sourceReleased = true; }
        while (!fs.existsSync(targetFile) && Date.now() - droppedAt < 5000) await new Promise(resolve => setTimeout(resolve, 15));
        assert.ok(fs.existsSync(targetFile), 'Scoped Windows folder must create the ticket');
        assert.deepEqual(fs.readFileSync(targetFile), payload);
        assert.deepEqual(api.readLocalFile(targetFile).contents, payload);
        assert.deepEqual(api.readLocalFile(source).contents, payload);
        if (!sourceReleased) verifyFormats(data, source);
        cases.push({ releaseSourceImmediately, rawDelayed, heapGCExposed: typeof global.gc === 'function', asynchronousRequested: asynchronous, asyncSource, fileExistsOnDropReturn, asynchronousCopyExercised: !fileExistsOnDropReturn, enterResult, dropResult, effect: effect.readUInt32LE(0), originalSourceReadableAfterCopy: true, byteExactCopiedTicket: true, bytes: payload.length, before, after, elapsedMs: Date.now() - droppedAt });
      } finally {
        if (!sourceReleased) adapter.release(prepared);
        if (target) api.releaseDataObject(target);
        if (item) api.releaseDataObject(item);
      }
    }
  } finally { api.oleUninitialize(); }
  const report = { passed: true, sameElectronMainThreadSource: true, actualElectronUIThread: process.argv.includes('--electron-ui-thread'), actualSHCreateDataObjectSource: true, realWindowsFolderDropTarget: true, cidaAndCFHDropVerified: true, nativeDragCalled: false, inputInjected: false, clipboardUntouched: true, phoneDataUntouched: true, cases };
  fs.writeFileSync(path.join(root, 'test-output', 'local-ticket-shell-copy-verification.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}

if (process.argv.includes('--shell-copy-probe')) {
  if (process.argv.includes('--electron-ui-thread')) {
    const { app } = require('electron');
    app.setName('EyeCopy ticket diagnostic');
    app.whenReady().then(shellCopyProbe).then(() => app.exit(0)).catch(error => { console.error(error.stack); app.exit(1); });
  } else shellCopyProbe().catch(error => { console.error(error.stack); process.exitCode = 1; });
} else {
  const test = require('node:test');
  test('real Windows target copies the exact Shell-created ticket in retained and released source lifetimes', () => {
    const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
    const result = spawnSync(require('electron'), ['--js-flags=--expose-gc', __filename, '--shell-copy-probe', '--electron-ui-thread'], { env, windowsHide: true, timeout: 20000, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const report = JSON.parse(result.stdout.trim());
    assert.equal(report.cidaAndCFHDropVerified, true);
    assert.equal(report.actualSHCreateDataObjectSource, true);
    assert.equal(report.nativeDragCalled, false);
    assert.equal(report.actualElectronUIThread, true);
    assert.equal(report.cases.length, 4);
    assert.equal(report.cases[2].asyncSource.requested, true);
    assert.equal(typeof report.cases[2].asyncSource.mode, 'boolean');
    assert.equal(report.cases[3].rawDelayed, true);
    assert.equal(report.cases[3].before.formatsNotQueried, true);
    assert.equal(report.cases[3].heapGCExposed, true);
    assert.ok(report.cases.every(value => value.byteExactCopiedTicket && value.originalSourceReadableAfterCopy));
  });
}
