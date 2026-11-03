'use strict';
// Called only in Electron main for an explicit drag. No renderer-facing API,
// input injection, input-queue attachment, or arbitrary window activation.
let native;
function bindings() {
  if (native) return native;
  const koffi = require('koffi');
  const library = koffi.load('user32.dll');
  native = {
    currentWindow: library.func('__stdcall', 'GetForegroundWindow', 'uintptr_t', []),
    windowProcess: library.func('__stdcall', 'GetWindowThreadProcessId', 'uint32_t', ['uintptr_t', 'void *']),
    keyState: library.func('__stdcall', 'GetAsyncKeyState', 'int16_t', ['int']),
    captureWindow: library.func('__stdcall', 'GetCapture', 'uintptr_t', []),
    rootWindow: library.func('__stdcall', 'GetAncestor', 'uintptr_t', ['uintptr_t', 'uint32_t']),
    releaseCapture: library.func('__stdcall', 'ReleaseCapture', 'int', []),
    activate: library.func('__stdcall', 'SetForegroundWindow', 'int', ['uintptr_t']),
  };
  return native;
}
function windowHandle(value) {
  if (!/^\d{1,20}$/.test(String(value))) throw new Error('Invalid drag source window.');
  const handle = BigInt(value);
  if (handle <= 0n || handle > 0x7fffffffffffffffn) throw new Error('Invalid drag source window.');
  return handle;
}
function processFor(api, handle) {
  const buffer = Buffer.alloc(4);
  return api.windowProcess(handle, buffer) ? buffer.readUInt32LE(0) : 0;
}
function createForegroundHandoff({ getBindings = bindings, parentPid = process.pid } = {}) {
  return {
    ready() { getBindings(); },
    releaseSourceCapture({ sourceWindow }) {
      const result = { sourceForeground: false, buttonHeld: false, sourceCaptureOwned: false, captureReleased: false, attempted: false, succeeded: false };
      let source;
      try { source = windowHandle(sourceWindow); } catch { return result; }
      const api = getBindings();
      if (processFor(api, source) !== parentPid) return result;
      result.sourceForeground = BigInt(api.currentWindow()) === source;
      result.buttonHeld = (api.keyState(1) & 0x8000) !== 0;
      if (!result.sourceForeground || !result.buttonHeld) return result;
      const captured = BigInt(api.captureWindow());
      if (captured !== 0n) {
        result.sourceCaptureOwned = processFor(api, captured) === parentPid && (captured === source || BigInt(api.rootWindow(captured, 2)) === source);
        if (!result.sourceCaptureOwned) return result;
        result.attempted = true;
        result.captureReleased = Boolean(api.releaseCapture()) && BigInt(api.captureWindow()) === 0n;
        if (!result.captureReleased) return result;
      }
      result.succeeded = BigInt(api.currentWindow()) === source && (api.keyState(1) & 0x8000) !== 0 && processFor(api, source) === parentPid;
      return result;
    },
    activate({ sourceWindow, hostWindow, helperPid }) {
      const result = { sourceForeground: false, buttonHeld: false, hostOwned: false, sourceCaptureOwned: false, captureReleased: false, captureTransferred: false, attempted: false, activationRequested: false, activationPending: false, succeeded: false };
      if (!Number.isInteger(helperPid) || helperPid <= 0 || helperPid >= 0xffffffff || helperPid === parentPid) return result;
      let source, host;
      try { source = windowHandle(sourceWindow); host = windowHandle(hostWindow); } catch { return result; }
      const api = getBindings();
      if (processFor(api, source) !== parentPid) return result;
      result.sourceForeground = BigInt(api.currentWindow()) === source;
      result.buttonHeld = (api.keyState(1) & 0x8000) !== 0;
      result.hostOwned = processFor(api, host) === helperPid;
      if (!result.sourceForeground || !result.buttonHeld || !result.hostOwned) return result;
      const captured = BigInt(api.captureWindow());
      if (captured !== 0n) {
        result.sourceCaptureOwned = processFor(api, captured) === parentPid && (captured === source || BigInt(api.rootWindow(captured, 2)) === source);
        if (!result.sourceCaptureOwned) return result;
        // GetCapture is scoped to this calling parent thread. Chromium can
        // retain native capture after the DOM dragstart was prevented; release
        // only this trusted source/child before OLE takes over in our helper.
        result.captureReleased = Boolean(api.releaseCapture()) && BigInt(api.captureWindow()) === 0n;
        if (!result.captureReleased) return result;
      }
      // Recheck immediately before the one allowed mutation. The host belongs
      // to our exact spawned helper; the source still belongs to this parent.
      if (BigInt(api.currentWindow()) !== source || (api.keyState(1) & 0x8000) === 0 || processFor(api, source) !== parentPid || processFor(api, host) !== helperPid) return result;
      result.attempted = true;
      result.activationRequested = Boolean(api.activate(host));
      const observed = BigInt(api.currentWindow());
      result.succeeded = result.activationRequested && observed === host;
      // SetForegroundWindow may return before a different input queue has
      // processed activation. Do not turn an accepted request into a false
      // cancellation just because the source (or transient NULL) still shows.
      // The helper's own bounded STA pump confirms actual foreground/active
      // ownership before starting OLE; an unrelated foreground is rejected.
      result.activationPending = result.activationRequested && (observed === source || observed === 0n);
      result.captureTransferred = result.captureReleased && result.succeeded;
      return result;
    },
  };
}
module.exports = { createForegroundHandoff };
