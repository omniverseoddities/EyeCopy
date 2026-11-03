'use strict';
// Electron main/UI thread only. Shell owns the IDataObject, default IDropSource,
// drag image and OLE modal loop; this adapter installs no JS COM callbacks.
// https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shdodragdrop
// https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shcreatedataobject
const fs = require('node:fs');
const path = require('node:path');
const { isMainThread } = require('node:worker_threads');
const DROP = 0x00040100, CANCEL = 0x00040101, COPY = 1;
let native;

function sourceError(stage, code = null) {
  const error = new Error(`Windows cannot read the drag ticket: ${stage}${code === null ? '' : ` (Win32 ${code})`}.`);
  error.sourceStage = stage; if (code !== null) error.win32Error = code;
  return error;
}
function createNativeTicketReader(api) {
  return filePath => {
    const attributes = api.fileAttributes(filePath) >>> 0;
    if (attributes === 0xffffffff) throw sourceError('GetFileAttributesW failed', api.lastError());
    if (attributes & 0x410) throw sourceError('the source is a directory or reparse point');
    // Read only, OPEN_EXISTING. Sharing preserves Explorer's subsequent copy;
    // OPEN_REPARSE_POINT refuses a raced replacement symlink at the leaf.
    const handle = api.openFile(filePath, 0x80000000, 7, null, 3, 0x00200080, 0n);
    if (!handle || BigInt(handle) === -1n || BigInt(handle) === api.invalidHandle) throw sourceError('CreateFileW failed', api.lastError());
    try {
      const info = Buffer.alloc(52);
      if (!api.fileInformation(handle, info)) throw sourceError('GetFileInformationByHandle failed', api.lastError());
      if (info.readUInt32LE(0) & 0x410) throw sourceError('the opened source is not an ordinary file');
      const size = BigInt(info.readUInt32LE(32)) * 0x100000000n + BigInt(info.readUInt32LE(36));
      if (size > 128n) throw sourceError('the source exceeds the 128 byte ticket limit');
      const contents = Buffer.alloc(128), count = Buffer.alloc(4);
      if (!api.readFile(handle, contents, contents.length, count, null)) throw sourceError('ReadFile failed', api.lastError());
      const length = count.readUInt32LE(0);
      if (length > contents.length || BigInt(length) !== size) throw sourceError('the source size changed while reading');
      return { attributes, size: Number(size), contents: contents.subarray(0, length) };
    } finally { api.closeFile(handle); }
  };
}

function loadShellBindings() {
  if (native) return native;
  if (process.platform !== 'win32' || !isMainThread) throw new Error('Local ticket dragging requires the Windows main/UI thread.');
  const koffi = require('koffi');
  const shell = koffi.load('shell32.dll'), ole = koffi.load('ole32.dll');
  const user = koffi.load('user32.dll'), kernel = koffi.load('kernel32.dll');
  const pointerSize = koffi.sizeof('void *');
  const readPointer = buffer => pointerSize === 8 ? buffer.readBigUInt64LE(0) : BigInt(buffer.readUInt32LE(0));
  const writePointer = (buffer, value) => pointerSize === 8 ? buffer.writeBigUInt64LE(BigInt(value), 0) : buffer.writeUInt32LE(Number(value), 0);
  const parse = shell.func('__stdcall', 'SHParseDisplayName', 'int32_t', ['str16', 'void *', 'void *', 'uint32_t', 'void *']);
  const create = shell.func('__stdcall', 'SHCreateDataObject', 'int32_t', ['void *', 'uint32_t', 'void *', 'void *', 'void *', 'void *']);
  const releaseType = koffi.proto('__stdcall', 'EyeCopyTicketIUnknownRelease', 'uint32_t', ['void *']);
  const iid = Buffer.alloc(16); iid.writeUInt32LE(0x10e, 0); iid[8] = 0xc0; iid[15] = 0x46;
  const currentWindow = user.func('__stdcall', 'GetForegroundWindow', 'uintptr_t', []);
  const windowProcess = user.func('__stdcall', 'GetWindowThreadProcessId', 'uint32_t', ['uintptr_t', 'void *']);
  const cursor = user.func('__stdcall', 'GetCursorPos', 'int', ['void *']);
  const sourceReader = createNativeTicketReader({
    invalidHandle: pointerSize === 8 ? 0xffffffffffffffffn : 0xffffffffn,
    fileAttributes: kernel.func('__stdcall', 'GetFileAttributesW', 'uint32_t', ['str16']),
    openFile: kernel.func('__stdcall', 'CreateFileW', 'uintptr_t', ['str16', 'uint32_t', 'uint32_t', 'void *', 'uint32_t', 'uint32_t', 'uintptr_t']),
    fileInformation: kernel.func('__stdcall', 'GetFileInformationByHandle', 'int', ['uintptr_t', 'void *']),
    readFile: kernel.func('__stdcall', 'ReadFile', 'int', ['uintptr_t', 'void *', 'uint32_t', 'void *', 'void *']),
    closeFile: kernel.func('__stdcall', 'CloseHandle', 'int', ['uintptr_t']),
    lastError: kernel.func('__stdcall', 'GetLastError', 'uint32_t', []),
  });
  native = {
    readLocalFile: sourceReader,
    oleInitialize: ole.func('__stdcall', 'OleInitialize', 'int32_t', ['void *']),
    oleUninitialize: ole.func('__stdcall', 'OleUninitialize', 'void', []),
    freePIDL: ole.func('__stdcall', 'CoTaskMemFree', 'void', ['void *']),
    lastID: shell.func('__stdcall', 'ILFindLastID', 'void *', ['void *']),
    threadId: kernel.func('__stdcall', 'GetCurrentThreadId', 'uint32_t', []),
    keyState: user.func('__stdcall', 'GetAsyncKeyState', 'int16_t', ['int']),
    currentWindow,
    processFor(window) { const output = Buffer.alloc(4); return windowProcess(window, output) ? output.readUInt32LE(0) : 0; },
    parsePIDL(filePath) {
      const output = Buffer.alloc(pointerSize);
      const hresult = parse(filePath, null, output, 0, null);
      return { hresult, pidl: readPointer(output) };
    },
    createDataObject(parentPIDL, childPIDL) {
      const children = Buffer.alloc(pointerSize), output = Buffer.alloc(pointerSize);
      writePointer(children, childPIDL);
      const hresult = create(parentPIDL, 1, children, null, iid, output);
      return { hresult, dataObject: readPointer(output) };
    },
    releaseDataObject(pointer) {
      const vtable = koffi.decode(pointer, 'void *');
      const release = koffi.decode(vtable, 2 * pointerSize, 'void *');
      return koffi.call(release, releaseType, pointer);
    },
    dragDrop: shell.func('__stdcall', 'SHDoDragDrop', 'int32_t', ['uintptr_t', 'void *', 'void *', 'uint32_t', 'void *']),
    cursorPosition() {
      const output = Buffer.alloc(8);
      return cursor(output) ? { x: output.readInt32LE(0), y: output.readInt32LE(4) } : null;
    },
  };
  return native;
}

function failed(hresult) { return (Number(hresult) | 0) < 0; }
function nativeError(stage, hresult) {
  const error = new Error(`${stage} failed (0x${(Number(hresult) >>> 0).toString(16).padStart(8, '0')}).`);
  error.hresult = Number(hresult) | 0; return error;
}
function sourceHandle(value) {
  if (!/^\d{1,20}$/.test(String(value))) throw new Error('Invalid local ticket source window.');
  const handle = BigInt(value);
  if (handle <= 0n || handle > 0x7fffffffffffffffn) throw new Error('Invalid local ticket source window.');
  return handle;
}
function validateTicket(filePath, stat) {
  if (typeof filePath !== 'string' || filePath.includes('\0') || !/^[A-Za-z]:\\/.test(filePath)) throw new Error('Choose an existing local transfer ticket.');
  const file = path.win32.normalize(filePath);
  if (!/^_(EyeCopy)_drop_[0-9a-f]{32}\.(txt|transfer)$/.test(path.win32.basename(file))) throw new Error('Only an EyeCopy transfer ticket can use this drag adapter.');
  const metadata = stat(file, { bigint: true });
  if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 128n) throw new Error('The transfer ticket is not an ordinary small local file.');
  return { file, signature: [metadata.dev, metadata.ino, metadata.size, metadata.mtimeNs].map(String).join(':') };
}
function verifyNativeSource(api, filePath) {
  const actual = api.readLocalFile(filePath);
  const match = /^_(EyeCopy)_drop_([0-9a-f]{32})\.(txt|transfer)$/.exec(path.win32.basename(filePath));
  const expected = match[3] === 'transfer' ? Buffer.alloc(0) : Buffer.from(`${match[1]} transfer ticket v1\n${match[2]}\n`, 'ascii');
  if (!Buffer.isBuffer(actual.contents) || !actual.contents.equals(expected)) throw sourceError('the native file bytes do not match this ticket nonce');
  return Object.freeze({ nativeReadable: true, attributes: actual.attributes, size: actual.size, noncePayloadVerified: true });
}

function createLocalTicketDrag({ getBindings = loadShellBindings, parentPid = process.pid, stat = fs.lstatSync } = {}) {
  const resources = new WeakMap();
  let dragging = false;
  function release(prepared) {
    const owned = prepared && resources.get(prepared);
    if (!owned || owned.released) return false;
    if (owned.api.threadId() !== owned.thread) throw new Error('Release the local ticket on its owning UI thread.');
    if (owned.active) throw new Error('Cannot release a local ticket during the native drag loop.');
    owned.released = true;
    let cleanupError;
    const cleanup = action => { try { action(); } catch (error) { cleanupError ||= error; } };
    if (owned.dataObject) cleanup(() => owned.api.releaseDataObject(owned.dataObject));
    for (const pidl of owned.pidls) if (pidl) cleanup(() => owned.api.freePIDL(pidl));
    if (owned.oleInitialized) cleanup(() => owned.api.oleUninitialize());
    if (cleanupError) throw cleanupError;
    return true;
  }
  return {
    prepare(filePath) {
      const ticket = validateTicket(filePath, stat), api = getBindings();
      const sourceValidation = verifyNativeSource(api, ticket.file);
      const prepared = Object.freeze({ filePath: ticket.file, sourceValidation });
      const owned = { api, thread: api.threadId(), pidls: [], dataObject: null, oleInitialized: false, released: false, active: false, signature: ticket.signature };
      resources.set(prepared, owned);
      try {
        const initialized = api.oleInitialize(null);
        if (failed(initialized)) throw nativeError('Initializing the UI drag apartment', initialized);
        owned.oleInitialized = true; // S_FALSE must also be balanced.
        const parent = api.parsePIDL(path.win32.dirname(ticket.file));
        if (parent.pidl) owned.pidls.push(parent.pidl);
        if (failed(parent.hresult) || !parent.pidl) throw nativeError('Resolving the ticket folder', parent.hresult);
        const file = api.parsePIDL(ticket.file);
        if (file.pidl) owned.pidls.push(file.pidl);
        if (failed(file.hresult) || !file.pidl) throw nativeError('Resolving the local ticket', file.hresult);
        // ILFindLastID is a borrowed pointer into file.pidl, not an allocation.
        // Keep its complete absolute PIDL alive through data-object release.
        const child = api.lastID(file.pidl);
        if (!child) throw new Error('The local ticket has no Shell file identifier.');
        const object = api.createDataObject(parent.pidl, child);
        owned.dataObject = object.dataObject;
        if (failed(object.hresult) || !object.dataObject) throw nativeError('Creating the Shell ticket data', object.hresult);
        return prepared;
      } catch (error) { release(prepared); throw error; }
    },
    drag(prepared, { sourceWindow } = {}) {
      const owned = prepared && resources.get(prepared);
      if (!owned || owned.released) throw new Error('Prepare a fresh local ticket before dragging.');
      if (owned.api.threadId() !== owned.thread) throw new Error('Run local ticket dragging on its owning UI thread.');
      if (dragging) throw new Error('Another local ticket gesture is still active.');
      try {
        const source = sourceHandle(sourceWindow), api = owned.api;
        if (validateTicket(prepared.filePath, stat).signature !== owned.signature) throw new Error('The transfer ticket changed. Select the file again.');
        const sourceValidation = verifyNativeSource(api, prepared.filePath);
        if (api.processFor(source) !== parentPid || BigInt(api.currentWindow()) !== source || !(api.keyState(1) & 0x8000)) throw new Error('Keep the left mouse button held in EyeCopy when starting this drag.');
        const effect = Buffer.alloc(4);
        dragging = true; owned.active = true;
        let hresult, point;
        try {
          // COPY only; null IDropSource requests Shell's native implementation.
          // This synchronous call stays on Electron's UI/OLE apartment.
          hresult = api.dragDrop(source, owned.dataObject, null, COPY, effect);
          point = api.cursorPosition(); // Capture the real release point first.
        } finally { dragging = false; owned.active = false; }
        const dropEffect = effect.readUInt32LE(0);
        return { dropped: (Number(hresult) >>> 0) === DROP && (dropEffect & 7) === COPY, effect: dropEffect, hresult: Number(hresult) | 0, point, cancelled: (Number(hresult) >>> 0) === CANCEL, sourceValidation };
      } finally { release(prepared); }
    },
    release,
  };
}

module.exports = { createLocalTicketDrag, loadShellBindings, createNativeTicketReader };
