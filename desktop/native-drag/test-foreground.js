'use strict';
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { createForegroundHandoff } = require('./foreground');

if (process.argv.includes('--electron-probe')) {
  // Electron's Node mode loads its real ABI without opening a browser window.
  // Bind/read user32 only; zero HWND is rejected before any activation call.
  const koffi = require('koffi'), library = koffi.load('user32.dll');
  const current = library.func('__stdcall', 'GetForegroundWindow', 'uintptr_t', []);
  const keys = library.func('__stdcall', 'GetAsyncKeyState', 'int16_t', ['int']);
  current(); keys(1);
  const handoff = createForegroundHandoff(); handoff.ready();
  const result = handoff.activate({ sourceWindow: '0', hostWindow: '0', helperPid: 1 });
  assert.equal(result.attempted, false);
  console.log(JSON.stringify({ passed: true, electronNativeLoad: true, nodeVersion: process.versions.node, napi: process.versions.napi, koffiVersion: koffi.version, readOnly: true }));
} else {
  const test = require('node:test');
  function fixture({ sourceOwned = true, sourceForeground = true, hostOwned = true, held = true, switchAfterCheck = false, capture = 'none', releaseSucceeds = true, activationAfter = 'host', activationAccepted = true } = {}) {
    const source = 9007199254741001n, host = 9007199254741002n, other = 9007199254741003n;
    let current = sourceForeground ? source : other, reads = 0;
    const activated = [];
    const released = []; const captureChild = 9007199254741004n;
    let captured = capture === 'source' ? source : capture === 'child' ? captureChild : capture === 'none' ? 0n : other;
    const api = {
      currentWindow() { if (switchAfterCheck && ++reads > 1) current = other; return current; },
      keyState() { return held ? -32768 : 0; },
      windowProcess(window, output) { output.writeUInt32LE(window === source && sourceOwned || window === captureChild || window === other && capture === 'other-own-window' ? 123 : window === host && hostOwned ? 456 : 789); return 99; },
      captureWindow() { return captured; },
      rootWindow(window) { return window === captureChild ? source : window; },
      releaseCapture() { released.push(captured); if (releaseSucceeds) captured = 0n; return releaseSucceeds ? 1 : 0; },
      activate(window) { activated.push(window); current = activationAfter === 'host' ? window : activationAfter === 'source' ? source : activationAfter === 'null' ? 0n : other; return activationAccepted ? 1 : 0; },
    };
    return { handoff: createForegroundHandoff({ getBindings: () => api, parentPid: 123 }), activated, released, source, host };
  }
  function activate(state) { return state.handoff.activate({ sourceWindow: state.source.toString(), hostWindow: state.host.toString(), helperPid: 456 }); }
  test('parent activates only its spawned helper host from the verified held foreground gesture', () => {
    const state = fixture(), result = activate(state);
    assert.equal(result.succeeded, true); assert.equal(result.attempted, true);
    assert.deepEqual(state.activated, [state.host]);
  });
  for (const activationAfter of ['source', 'null']) test(`accepted cross-thread activation remains pending while foreground is ${activationAfter}`, () => {
    const state = fixture({ activationAfter }), result = activate(state);
    assert.equal(result.succeeded, false); assert.equal(result.activationRequested, true);
    assert.equal(result.activationPending, true); assert.equal(result.attempted, true);
    assert.deepEqual(state.activated, [state.host]);
  });
  test('an unrelated foreground after the activation request is never acknowledged', () => {
    const result = activate(fixture({ activationAfter: 'other' }));
    assert.equal(result.succeeded, false); assert.equal(result.activationPending, false);
  });
  test('a rejected activation request is never acknowledged as pending', () => {
    const result = activate(fixture({ activationAfter: 'source', activationAccepted: false }));
    assert.equal(result.succeeded, false); assert.equal(result.activationPending, false);
  });
  for (const flag of ['sourceOwned', 'sourceForeground', 'hostOwned', 'held']) test(`refuses activation when ${flag} is false`, () => {
    const state = fixture({ [flag]: false }), result = activate(state);
    assert.equal(result.attempted, false); assert.deepEqual(state.activated, []);
  });
  test('a focus change before activation cannot steal focus back', () => {
    const state = fixture({ switchAfterCheck: true }), result = activate(state);
    assert.equal(result.attempted, false); assert.deepEqual(state.activated, []);
  });
  for (const capture of ['source', 'child']) test(`releases only trusted source ${capture} capture before transfer`, () => {
    const state = fixture({ capture }), result = activate(state);
    assert.equal(result.sourceCaptureOwned, true); assert.equal(result.captureReleased, true);
    assert.equal(result.captureTransferred, true); assert.equal(state.released.length, 1);
    assert.deepEqual(state.activated, [state.host]);
  });
  for (const capture of ['other-process', 'other-own-window']) test(`does not release capture from ${capture}`, () => {
    const state = fixture({ capture }), result = activate(state);
    assert.equal(result.sourceCaptureOwned, false); assert.equal(result.attempted, false);
    assert.deepEqual(state.released, []); assert.deepEqual(state.activated, []);
  });
  test('a failed capture release prevents starting a conflicting OLE capture', () => {
    const state = fixture({ capture: 'source', releaseSucceeds: false }), result = activate(state);
    assert.equal(result.captureReleased, false); assert.equal(result.attempted, false);
    assert.deepEqual(state.activated, []);
  });
  for (const capture of ['none', 'source', 'child']) test(`local drop preflight handles ${capture} capture without any activation`, () => {
    const state = fixture({ capture }), result = state.handoff.releaseSourceCapture({ sourceWindow: state.source.toString() });
    assert.equal(result.succeeded, true); assert.equal(result.captureReleased, capture !== 'none');
    assert.equal(state.released.length, capture === 'none' ? 0 : 1); assert.deepEqual(state.activated, []);
  });
  for (const options of [{ sourceOwned: false }, { sourceForeground: false }, { held: false }, { capture: 'other-own-window' }, { capture: 'other-process' }, { switchAfterCheck: true }]) test(`local preflight refuses unrelated or expired gesture: ${JSON.stringify(options)}`, () => {
    const state = fixture(options), result = state.handoff.releaseSourceCapture({ sourceWindow: state.source.toString() });
    assert.equal(result.succeeded, false); assert.deepEqual(state.activated, []);
    assert.deepEqual(state.released, []);
  });
  test('zero/invalid HWND and broad or parent PID cannot activate a window', () => {
    const state = fixture();
    for (const args of [{ sourceWindow: '0', hostWindow: state.host.toString(), helperPid: 456 }, { sourceWindow: state.source.toString(), hostWindow: '-1', helperPid: 456 }, { sourceWindow: state.source.toString(), hostWindow: state.host.toString(), helperPid: 0xffffffff }, { sourceWindow: state.source.toString(), hostWindow: state.host.toString(), helperPid: 123 }]) assert.equal(state.handoff.activate(args).attempted, false);
    assert.deepEqual(state.activated, []);
  });
  test('real Electron ABI loads Koffi and reads user32 without activating any app', () => {
    const result = spawnSync(require('electron'), [__filename, '--electron-probe'], { env: { ...process.env, ELECTRON_RUN_AS_NODE: '1' }, windowsHide: true, timeout: 10000, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr || result.error?.message);
    const report = JSON.parse(result.stdout.trim());
    assert.equal(report.electronNativeLoad, true); assert.equal(report.readOnly, true);
  });
}
