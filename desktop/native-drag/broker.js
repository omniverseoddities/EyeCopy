'use strict';
// Windows virtual-file drag source. No clipboard changes, temporary video, or
// Explorer window automation; Explorer chooses and writes the destination.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const readline = require('node:readline');
const { createForegroundHandoff } = require('./foreground');

async function buildHelper(dataDir) {
  if (process.platform !== 'win32') throw new Error('Direct Explorer drops require Windows.');
  const bundled = path.join(__dirname, '..', '..', 'assets', 'native', 'EyeCopyDrag.exe');
  if (fs.existsSync(bundled)) return bundled;
  const source = path.join(__dirname, 'VirtualFileDrag.cs');
  const hash = crypto.createHash('sha256').update(await fs.promises.readFile(source)).digest('hex').slice(0, 20);
  const outputDir = path.join(dataDir, 'native-drag', hash);
  const executable = path.join(outputDir, 'EyeCopyDrag.exe');
  if (fs.existsSync(executable)) return executable;
  await fs.promises.mkdir(outputDir, { recursive: true });
  const compiler = path.join(process.env.SystemRoot || 'C:\\Windows', 'Microsoft.NET', 'Framework64', 'v4.0.30319', 'csc.exe');
  if (!fs.existsSync(compiler)) throw new Error('Windows .NET Framework compiler is unavailable. Use Download to folder instead.');
  await new Promise((resolve, reject) => {
    const child = spawn(compiler, ['/nologo', '/target:exe', '/platform:x64', '/optimize+', '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll', `/out:${executable}`, source], { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
    let diagnostic = '';
    child.stdout.on('data', chunk => { diagnostic += String(chunk).slice(0, 6000); });
    child.stderr.on('data', chunk => { diagnostic += String(chunk).slice(0, 6000); });
    child.on('error', reject);
    child.on('exit', code => code === 0 ? resolve() : reject(new Error(`Explorer drop helper could not compile: ${diagnostic.slice(0, 1500)}`)));
  });
  return executable;
}

function createDragBroker({ dataDir, onEvent = () => {}, onError = () => {} }) {
  let processHandle = null, startup = null, stopped = false;
  const outstanding = new Set();
  const sourceWindows = new Map();
  const foreground = createForegroundHandoff();
  function ready() {
    if (stopped) return Promise.reject(new Error('Explorer drop helper has stopped.'));
    if (startup) return startup;
    startup = buildHelper(dataDir).then(executable => {
      foreground.ready();
      return new Promise((resolve, reject) => {
      const child = processHandle = spawn(executable, ['--parent', String(process.pid)], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
      let initialized = false;
      const timer = setTimeout(() => { child.kill(); reject(new Error('Explorer drop helper did not start.')); }, 12000);
      function protocolFailure() {
        for (const id of outstanding) onEvent({ event: 'error', id, message: 'Explorer copy source returned an invalid response. Cancelled this copy; try dragging again.' });
        outstanding.clear(); sourceWindows.clear();
        onError(new Error('Explorer drop helper returned an invalid response.'));
        child.kill();
      }
      readline.createInterface({ input: child.stdout }).on('line', line => {
        let message;
        try { message = JSON.parse(line); } catch { protocolFailure(); return; }
        if (!message || typeof message !== 'object' || typeof message.event !== 'string') { protocolFailure(); return; }
        if (message.event === 'ready') { initialized = true; clearTimeout(timer); resolve(executable); return; }
        if (message.event === 'foreground_request') {
          // This window came from our private helper process, never renderer
          // input. Native validation checks both HWND owners and live input.
          let result = { succeeded: false };
          try {
            if (!stopped && processHandle === child && outstanding.has(message.id) && sourceWindows.has(message.id)) result = foreground.activate({ sourceWindow: sourceWindows.get(message.id), hostWindow: message.window, helperPid: child.pid });
          } catch { result = { succeeded: false }; }
          onEvent({ event: 'foreground_request', id: message.id, ...result });
          try { if (child.stdin.writable) child.stdin.write(JSON.stringify({ command: 'foreground_ack', id: message.id, succeeded: Boolean(result.succeeded || result.activationPending) }) + '\n'); }
          catch { child.kill(); }
          return;
        }
        if (message.event === 'error' && !message.id && outstanding.size) { protocolFailure(); return; }
        if (['completed', 'cancelled', 'error'].includes(message.event)) { outstanding.delete(message.id); sourceWindows.delete(message.id); }
        onEvent(message);
      });
      child.stderr.on('data', () => {}); // Never print signed media URLs.
      child.stdin.on('error', error => {
        if (!stopped) {
          onError(error);
          for (const id of outstanding) onEvent({ event: 'error', id, message: 'Explorer copy source stopped accepting requests. Try dragging again.' });
          outstanding.clear(); sourceWindows.clear(); child.kill();
        }
      });
      child.on('error', error => { clearTimeout(timer); reject(error); onError(error); });
      child.on('exit', () => {
        clearTimeout(timer);
        processHandle = null; startup = null;
        if (!initialized) reject(new Error('Explorer drop helper exited during startup.'));
        if (!stopped) {
          for (const id of outstanding) onEvent({ event: 'error', id, message: 'Explorer copy source stopped. Check Explorer for a partial file and retry.' });
          outstanding.clear(); sourceWindows.clear();
        }
      });
      });
    });
    startup.catch(() => { startup = null; });
    return startup;
  }
  async function begin(files, id = crypto.randomUUID(), { sourceWindow } = {}) {
    await ready();
    if (!Array.isArray(files) || !files.length || files.length > 100) throw new Error('Choose between one and 100 files to drag.');
    if (stopped || !processHandle?.stdin.writable || processHandle.exitCode !== null) throw new Error('Explorer copy source is no longer running. Try dragging again.');
    outstanding.add(id);
    if (sourceWindow) sourceWindows.set(id, String(sourceWindow));
    try {
      processHandle.stdin.write(JSON.stringify({ command: 'begin', id, files,
        ...(sourceWindow ? { sourceWindow: String(sourceWindow), sourcePid: process.pid } : {}) }) + '\n');
    }
    catch (error) { outstanding.delete(id); sourceWindows.delete(id); throw error; }
    return id;
  }
  function cancel(id) {
    // Revoke activation permission synchronously, before the helper reader can
    // process a cancellation or a previously queued foreground request.
    outstanding.delete(id); sourceWindows.delete(id);
    if (processHandle?.stdin.writable) processHandle.stdin.write(JSON.stringify({ command: 'cancel', id }) + '\n');
  }
  function close() {
    stopped = true;
    outstanding.clear(); sourceWindows.clear();
    if (processHandle?.stdin.writable) processHandle.stdin.write('{"command":"quit"}\n');
    const child = processHandle;
    if (child) setTimeout(() => { if (child.exitCode === null) child.kill(); }, 1500).unref();
  }
  return { ready, begin, cancel, close };
}
module.exports = { buildHelper, createDragBroker };
